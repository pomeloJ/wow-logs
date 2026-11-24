"""Screenshots: pictures taken in the game, placed on the timeline.

The agent uploads each screenshot with the time it was taken. Where it was taken and what
was going on are not stored with it: they are looked up from the character's recorded
events when it is shown, so a screenshot uploaded before its events still gets a place.
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
MAX_BYTES = 8 * 1024 * 1024
MOMENTS = {"MOB_KILLED": lambda p: f"擊殺 {p.get('monster_name') or '怪物'}",
           "LEVEL_UP": lambda p: f"升到 {p.get('level')} 級",
           "QUEST_COMPLETED": lambda p: f"完成任務 {p.get('quest_name') or ''}",
           "QUEST_ACCEPTED": lambda p: f"接受任務 {p.get('quest_name') or ''}",
           "PLAYER_DIED": lambda p: "死亡", "INSTANCE_ENTERED": lambda p: f"進入副本 {p.get('name') or ''}",
           "ACHIEVEMENT_EARNED": lambda p: f"成就 {p.get('name') or ''}",
           "ENCOUNTER_END": lambda p: f"首領戰 {p.get('name') or ''}"}


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

    def path(self, shot_id: str, thumb: bool = False) -> str:
        return os.path.join(self.folder, f"{shot_id}{'.t' if thumb else ''}.jpg")

    def describe(self, conn, row) -> Dict[str, Any]:
        """A screenshot with the place and the moment it belongs to."""
        at = _dt(row["taken_at"])
        near = conn.execute(
            "SELECT event_type, timestamp, payload_json FROM events WHERE character_id=? AND timestamp>=? "
            "AND timestamp<=? AND event_type NOT IN ('CHAT_MESSAGE','PARTY_POSITIONS','GROUP_METER','GROUP_ENCOUNTER')",
            (row["character_id"], _fmt(at - timedelta(minutes=10)), _fmt(at + timedelta(minutes=10)))).fetchall()
        place: Optional[Dict[str, Any]] = None
        moment: Optional[str] = None
        best_place = best_moment = None
        for e in near:
            gap = abs((_dt(e["timestamp"]) - at).total_seconds())
            p = json.loads(e["payload_json"])
            if isinstance(p.get("map_id"), int) and isinstance(p.get("x"), (int, float)) \
                    and (best_place is None or gap < best_place):
                best_place = gap
                place = {"map_id": p["map_id"], "zone": p.get("zone_name"), "x": p["x"], "y": p.get("y")}
            if e["event_type"] in MOMENTS and gap <= 90 and (best_moment is None or gap < best_moment):
                best_moment = gap
                moment = MOMENTS[e["event_type"]](p).strip()
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
                             writer: Optional[str] = Depends(can_write)):
        check_id(shot_id)
        owned(writer, shot_id, character_id)
        if taken_at.tzinfo is None:
            raise HTTPException(400, "taken_at must include a timezone")
        store(shots.path(shot_id), await jpeg_body(request))
        with db() as conn:
            conn.execute("INSERT OR REPLACE INTO screenshots VALUES (?,?,?,?,?,?)",
                         (shot_id, character_id, _fmt(taken_at), width, height, _fmt(datetime.now(timezone.utc))))
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
            return {"total": total, "items": [shots.describe(conn, r) for r in rows]}

    def serve(shot_id: str, thumb: bool, scope: Optional[str]):
        check_id(shot_id)
        with db() as conn:
            row = conn.execute("SELECT character_id FROM screenshots WHERE id=?", (shot_id,)).fetchone()
        path = shots.path(shot_id, thumb)
        if not row or (scope is not None and row["character_id"] != scope) or not os.path.isfile(path):
            raise HTTPException(404, "no such screenshot")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/api/v1/screenshots/{shot_id}/image")
    def get_image(shot_id: str, scope: Optional[str] = Depends(can_read)):
        return serve(shot_id, False, scope)

    @app.get("/api/v1/screenshots/{shot_id}/thumb")
    def get_thumb(shot_id: str, scope: Optional[str] = Depends(can_read)):
        return serve(shot_id, True, scope)

    return shots
