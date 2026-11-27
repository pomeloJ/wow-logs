"""Screenshots: pictures taken in the game, placed on the timeline.

The agent uploads each screenshot with the time it was taken. Where it was taken and what
was going on are not stored with it: they are looked up from the character's recorded
events when it is shown, so a screenshot uploaded before its events still gets a place.

Uploading them is the player's choice, off until they turn it on in the sync program, and so
is how many are kept (the newest 50 unless they choose otherwise; older ones are deleted).
Both are kept here, one choice per token, so every character and every computer that uploads
with the same token follows them. The newest screenshot is what a tool (an AI voice assistant) reads
to see what the player sees right now.
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
MAX_BYTES = 8 * 1024 * 1024
MOMENTS = {"MOB_KILLED": lambda p: f"擊殺 {p.get('monster_name') or '怪物'}",
           "LEVEL_UP": lambda p: f"升到 {p.get('level')} 級",
           "QUEST_COMPLETED": lambda p: f"完成任務 {p.get('quest_name') or ''}",
           "QUEST_ACCEPTED": lambda p: f"接受任務 {p.get('quest_name') or ''}",
           "PLAYER_DIED": lambda p: "死亡", "INSTANCE_ENTERED": lambda p: f"進入副本 {p.get('name') or ''}",
           "ACHIEVEMENT_EARNED": lambda p: f"成就 {p.get('name') or ''}",
           "ENCOUNTER_END": lambda p: f"首領戰 {p.get('name') or ''}"}


KEEP_DEFAULT, KEEP_MAX = 50, 500


class SyncSettingsIn(BaseModel):
    upload_screenshots: Optional[bool] = None                       # left out: unchanged
    keep_screenshots: Optional[int] = Field(None, ge=1, le=KEEP_MAX)


def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dt(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class Shots:
    def __init__(self, db, folder: str):
        self.db, self.folder = db, folder
        os.makedirs(folder, exist_ok=True)
        with db() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS screenshots (id TEXT PRIMARY KEY, character_id TEXT NOT NULL, "
                         "taken_at TEXT NOT NULL, width INTEGER, height INTEGER, uploaded_at TEXT NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_shots_char ON screenshots(character_id, taken_at)")
            # where the picture itself says it was taken (the addon's AI screenshot codes it in)
            have = {r[1] for r in conn.execute("PRAGMA table_info(screenshots)")}
            for col, kind in (("map_id", "INTEGER"), ("x", "REAL"), ("y", "REAL"), ("facing", "INTEGER")):
                if col not in have:
                    conn.execute(f"ALTER TABLE screenshots ADD COLUMN {col} {kind}")
            # what the sync program does, per token ("owner" for the server's own): one choice
            # for every character and computer that uploads with it
            conn.execute("CREATE TABLE IF NOT EXISTS sync_settings (token TEXT PRIMARY KEY, "
                         "upload_screenshots INTEGER NOT NULL, updated_at TEXT NOT NULL)")
            if "keep_screenshots" not in {r[1] for r in conn.execute("PRAGMA table_info(sync_settings)")}:
                conn.execute(f"ALTER TABLE sync_settings ADD COLUMN keep_screenshots INTEGER NOT NULL DEFAULT {KEEP_DEFAULT}")

    def path(self, shot_id: str, thumb: bool = False) -> str:
        return os.path.join(self.folder, f"{shot_id}{'.t' if thumb else ''}.jpg")

    def describe(self, conn, row) -> Dict[str, Any]:
        """A screenshot with the place and the moment it belongs to."""
        at = _dt(row["taken_at"])
        near = conn.execute(
            "SELECT event_type, timestamp, payload_json FROM events WHERE character_id=? AND timestamp>=? "
            "AND timestamp<=? AND event_type NOT IN ('CHAT_MESSAGE','PARTY_POSITIONS','GROUP_METER','GROUP_ENCOUNTER','THREAT')",
            (row["character_id"], _fmt(at - timedelta(minutes=10)), _fmt(at + timedelta(minutes=10)))).fetchall()
        place: Optional[Dict[str, Any]] = None
        moment: Optional[str] = None
        best_place = best_moment = None
        zones: Dict[int, str] = {}
        for e in near:
            gap = abs((_dt(e["timestamp"]) - at).total_seconds())
            p = json.loads(e["payload_json"])
            if isinstance(p.get("map_id"), int) and p.get("zone_name"):
                zones.setdefault(p["map_id"], p["zone_name"])
            # an AI screenshot's own record is the exact moment; anything else only the nearest
            exact = e["event_type"] == "AI_SCREENSHOT" and gap <= 5
            if isinstance(p.get("map_id"), int) and isinstance(p.get("x"), (int, float)) \
                    and (best_place is None or exact or gap < best_place) and best_place != -1:
                best_place = -1 if exact else gap
                place = {"map_id": p["map_id"], "zone": p.get("zone_name"), "x": p["x"], "y": p.get("y"),
                         "facing": p.get("facing") if exact else None, "source": "ai_screenshot" if exact else "events",
                         "seconds_off": round(gap)}
            if e["event_type"] in MOMENTS and gap <= 90 and (best_moment is None or gap < best_moment):
                best_moment = gap
                moment = MOMENTS[e["event_type"]](p).strip()
        keys = row.keys()
        if "map_id" in keys and row["map_id"] is not None:
            # the picture's own code: the very moment it was taken, before any saving
            place = {"map_id": row["map_id"], "zone": zones.get(row["map_id"]), "x": row["x"], "y": row["y"],
                     "facing": row["facing"], "source": "screenshot", "seconds_off": 0}
        return {"id": row["id"], "taken_at": row["taken_at"], "width": row["width"], "height": row["height"],
                "place": place, "moment": moment}

    def between(self, conn, character_id: str, start: str, end: str) -> List[Dict[str, Any]]:
        rows = conn.execute("SELECT * FROM screenshots WHERE character_id=? AND taken_at>=? AND taken_at<=? "
                            "ORDER BY taken_at", (character_id, start, end)).fetchall()
        return [self.describe(conn, r) for r in rows]


def register(app, db, can_read, can_write, claim, folder: str) -> Shots:
    shots = Shots(db, folder)

    def owned(writer: Optional[str], shot_id: str, character_id: Optional[str] = None) -> None:
        """A personal token may only touch its own character's screenshots."""
        with db() as conn:
            row = conn.execute("SELECT character_id FROM screenshots WHERE id=?", (shot_id,)).fetchone()
        if writer is None:
            return
        if row is None and character_id is None:
            raise HTTPException(404, "no such screenshot")
        if row is not None and character_id is not None and row["character_id"] != character_id:
            raise HTTPException(409, "this screenshot belongs to another character")
        claim(writer, character_id or row["character_id"])

    def check_id(shot_id: str) -> None:
        if not ID.match(shot_id):
            raise HTTPException(400, "bad screenshot id")

    async def jpeg_body(request: Request) -> bytes:
        body = await request.body()
        if len(body) > MAX_BYTES:
            raise HTTPException(413, "image too large")
        if body[:3] != b"\xff\xd8\xff":
            raise HTTPException(400, "body must be a JPEG image")
        return body

    def store(path: str, body: bytes) -> None:
        with open(path + ".tmp", "wb") as f:
            f.write(body)
        os.replace(path + ".tmp", path)

    @app.put("/api/v1/screenshots/{shot_id}")
    async def put_screenshot(shot_id: str, request: Request, character_id: str, taken_at: datetime,
                             width: Optional[int] = None, height: Optional[int] = None,
                             map_id: Optional[int] = None, x: Optional[float] = Query(None, ge=0, le=100),
                             y: Optional[float] = Query(None, ge=0, le=100), facing: Optional[int] = Query(None, ge=0, lt=360),
                             writer: Optional[str] = Depends(can_write)):
        """Store a screenshot. map_id, x, y and facing: where the picture itself says it was taken
        (read by the sync program from the AI screenshot's code), left out for other pictures."""
        check_id(shot_id)
        owned(writer, shot_id, character_id)
        if taken_at.tzinfo is None:
            raise HTTPException(400, "taken_at must include a timezone")
        store(shots.path(shot_id), await jpeg_body(request))
        with db() as conn:
            conn.execute("INSERT OR REPLACE INTO screenshots (id, character_id, taken_at, width, height, uploaded_at, "
                         "map_id, x, y, facing) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (shot_id, character_id, _fmt(taken_at), width, height, _fmt(datetime.now(timezone.utc)),
                          map_id, x, y, facing))
        prune(writer)
        return {"success": True, "id": shot_id}

    @app.put("/api/v1/screenshots/{shot_id}/thumb")
    async def put_thumb(shot_id: str, request: Request, writer: Optional[str] = Depends(can_write)):
        check_id(shot_id)
        owned(writer, shot_id)
        store(shots.path(shot_id, True), await jpeg_body(request))
        return {"success": True, "id": shot_id}

    @app.delete("/api/v1/screenshots/{shot_id}")
    def delete_screenshot(shot_id: str, writer: Optional[str] = Depends(can_write)):
        check_id(shot_id)
        owned(writer, shot_id)
        with db() as conn:
            gone = conn.execute("DELETE FROM screenshots WHERE id=?", (shot_id,)).rowcount
        for thumb in (False, True):
            if os.path.isfile(shots.path(shot_id, thumb)):
                os.remove(shots.path(shot_id, thumb))
        if not gone:
            raise HTTPException(404, "no such screenshot")
        return {"success": True}

    @app.get("/api/v1/screenshots")
    def list_screenshots(character_id: Optional[str] = None, limit: int = Query(60, ge=1, le=300),
                         before: Optional[datetime] = None, scope: Optional[str] = Depends(can_read)):
        """Newest first. Each has the place and moment it was taken, when known."""
        if scope is not None:
            character_id = scope
        where, args = ["1=1"], []
        if character_id:
            where.append("character_id=?")
            args.append(character_id)
        if before:
            if before.tzinfo is None:
                raise HTTPException(400, "before must include a timezone")
            where.append("taken_at<?")
            args.append(_fmt(before))
        with db() as conn:
            rows = conn.execute("SELECT * FROM screenshots WHERE " + " AND ".join(where)
                                + " ORDER BY taken_at DESC LIMIT ?", args + [limit]).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM screenshots WHERE " + " AND ".join(where[:2] if character_id else where[:1]),
                                 args[:1] if character_id else []).fetchone()[0]
            out = [shots.describe(conn, r) for r in rows]
        if getattr(scope, "sections", None) is not None and "location" not in scope.sections:
            for s in out:                       # a sharing token that hides where the character goes
                s["place"] = None
        return {"total": total, "items": out}

    def serve(shot_id: str, thumb: bool, scope: Optional[str]):
        check_id(shot_id)
        with db() as conn:
            row = conn.execute("SELECT character_id FROM screenshots WHERE id=?", (shot_id,)).fetchone()
        path = shots.path(shot_id, thumb)
        if not row or (scope is not None and row["character_id"] not in scope.all) or not os.path.isfile(path):
            raise HTTPException(404, "no such screenshot")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    def newest(character_id: str, scope: Optional[str], max_age: Optional[int]):
        if scope is not None and character_id not in scope.all:
            raise HTTPException(404, "no such character")
        with db() as conn:
            row = conn.execute("SELECT * FROM screenshots WHERE character_id=? ORDER BY taken_at DESC LIMIT 1",
                               (character_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "no screenshots for this character (the player has to turn on screenshot upload in the sync program)")
            age = max(0, int((datetime.now(timezone.utc) - _dt(row["taken_at"])).total_seconds()))
            if max_age is not None and age > max_age:
                raise HTTPException(404, f"the newest screenshot is {age} seconds old, more than max_age")
            return row, age, shots.describe(conn, row)

    @app.get("/api/v1/characters/{character_id}/screenshots/latest")
    def latest_screenshot(character_id: str, max_age: Optional[int] = Query(None, ge=0, description="seconds; older than this counts as none"),
                          scope: Optional[str] = Depends(can_read)):
        """The character's newest screenshot: what the player saw a moment ago.

        When it was taken, `age_seconds` (how long ago), where and what was going on (`place`,
        `moment`), and `image`, the address of the picture itself
        (JPEG, 1920 wide; `thumb` is a small one). With `max_age`, a screenshot older than that
        is answered with 404, so an assistant never describes a scene that is long gone.

        `place.source` says how sure the place is: `screenshot` is read from the picture itself
        (an AI screenshot, F12, codes the position into it: the same as its panel shows, from the
        moment it was taken), `ai_screenshot` from the addon's record of that key press, `events`
        the nearest recorded position, `seconds_off` away (the addon's records reach the server
        only after saving, so this one can be well behind). `facing` is degrees clockwise from north.

        Screenshots arrive a few seconds after the player takes one (Print Screen in the game),
        and only once the player has turned on screenshot upload in the sync program."""
        row, age, out = newest(character_id, scope, max_age)
        out.update(age_seconds=age, image=f"/api/v1/screenshots/{row['id']}/image", thumb=f"/api/v1/screenshots/{row['id']}/thumb")
        if getattr(scope, "sections", None) is not None and "location" not in scope.sections:
            out["place"] = None
        return out

    @app.get("/api/v1/characters/{character_id}/screenshots/latest/image",
             responses={200: {"content": {"image/jpeg": {}}, "description": "the picture"}})
    def latest_screenshot_image(character_id: str, max_age: Optional[int] = Query(None, ge=0, description="seconds; older than this counts as none"),
                                scope: Optional[str] = Depends(can_read)):
        """The character's newest screenshot itself, as JPEG: for a tool that looks at the picture
        straight away. `X-Taken-At` and `X-Age-Seconds` say when it was taken. Same `max_age` as above."""
        row, age, _ = newest(character_id, scope, max_age)
        path = shots.path(row["id"])
        if not os.path.isfile(path):
            raise HTTPException(404, "no such screenshot")
        return FileResponse(path, media_type="image/jpeg", headers={
            "Cache-Control": "no-store", "X-Taken-At": row["taken_at"], "X-Age-Seconds": str(age)})

    def scope_of(writer: Optional[str]):
        """Which characters' screenshots a token's choices are about: its own characters for a
        person's token; for the server's own, the characters that belong to nobody."""
        if writer:
            return "character_id IN (SELECT character_id FROM token_characters WHERE token=?)", (writer,)
        return "character_id NOT IN (SELECT character_id FROM token_characters)", ()

    def settings_of(writer: Optional[str]) -> Dict[str, Any]:
        where, args = scope_of(writer)
        with db() as conn:
            row = conn.execute("SELECT upload_screenshots, keep_screenshots, updated_at FROM sync_settings WHERE token=?",
                               (writer or "owner",)).fetchone()
            stored = conn.execute(f"SELECT COUNT(*) FROM screenshots WHERE {where}", args).fetchone()[0]
        return {"upload_screenshots": bool(row and row["upload_screenshots"]),
                "keep_screenshots": row["keep_screenshots"] if row else KEEP_DEFAULT,
                "screenshots_stored": stored, "updated_at": row["updated_at"] if row else None}

    def prune(writer: Optional[str]) -> int:
        """Delete this token's oldest screenshots beyond the number it keeps. Returns how many."""
        keep = settings_of(writer)["keep_screenshots"]
        where, args = scope_of(writer)
        with db() as conn:
            gone = [r["id"] for r in conn.execute(f"SELECT id FROM screenshots WHERE {where} ORDER BY taken_at DESC "
                                                  "LIMIT -1 OFFSET ?", (*args, keep))]
            conn.executemany("DELETE FROM screenshots WHERE id=?", [(i,) for i in gone])
        for shot_id in gone:
            for thumb in (False, True):
                if os.path.isfile(shots.path(shot_id, thumb)):
                    os.remove(shots.path(shot_id, thumb))
        return len(gone)

    @app.get("/api/v1/sync-settings", include_in_schema=False)
    def get_sync_settings(writer: Optional[str] = Depends(can_write)):
        """What the sync program should do for this token's characters, and how many of their
        screenshots are here. Asked for by the sync program; uploading is off until the player
        turns it on, and the newest 50 screenshots are kept unless they choose another number."""
        return settings_of(writer)

    @app.put("/api/v1/sync-settings", include_in_schema=False)
    def put_sync_settings(body: SyncSettingsIn, writer: Optional[str] = Depends(can_write)):
        """Change one choice or both; one left out stays as it was. Keeping fewer screenshots
        deletes the oldest at once."""
        held = settings_of(writer)
        on = held["upload_screenshots"] if body.upload_screenshots is None else body.upload_screenshots
        keep = held["keep_screenshots"] if body.keep_screenshots is None else body.keep_screenshots
        with db() as conn:
            conn.execute("INSERT OR REPLACE INTO sync_settings (token, upload_screenshots, keep_screenshots, updated_at) "
                         "VALUES (?,?,?,?)", (writer or "owner", int(on), keep, _fmt(datetime.now(timezone.utc))))
        prune(writer)
        return settings_of(writer)

    @app.get("/api/v1/screenshots/{shot_id}/image")
    def get_image(shot_id: str, scope: Optional[str] = Depends(can_read)):
        """A screenshot at full size, as JPEG."""
        return serve(shot_id, False, scope)

    @app.get("/api/v1/screenshots/{shot_id}/thumb")
    def get_thumb(shot_id: str, scope: Optional[str] = Depends(can_read)):
        """A screenshot's small preview, as JPEG."""
        return serve(shot_id, True, scope)

    return shots
