"""Adventure Hub server (Phase 1): FastAPI + SQLite."""
import base64
import math
from collections import Counter
import hmac
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

# Rolls for group loot that older addon versions recorded as pick-ups (see groups.py).
ROLL_LINES = ("戰利品", "Loot")

VERSION = "0.1.0"

SCHEMA = """
CREATE TABLE IF NOT EXISTS characters (
    id TEXT PRIMARY KEY, name TEXT, realm TEXT, guid TEXT, race TEXT, class TEXT,
    faction TEXT, level INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS upload_tokens (
    token TEXT PRIMARY KEY, label TEXT, character_id TEXT, created_at TEXT NOT NULL,
    bound_at TEXT, last_used_at TEXT
);
CREATE TABLE IF NOT EXISTS token_characters (
    character_id TEXT PRIMARY KEY, token TEXT NOT NULL, claimed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS character_snapshots (
    character_id TEXT PRIMARY KEY, captured_at TEXT NOT NULL, device_id TEXT,
    payload_json TEXT NOT NULL, received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY, character_id TEXT NOT NULL, device_id TEXT,
    event_type TEXT NOT NULL, timestamp TEXT NOT NULL, map_id INTEGER,
    payload_json TEXT NOT NULL, received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_char_ts ON events(character_id, timestamp);
CREATE INDEX IF NOT EXISTS ix_events_type_ts ON events(event_type, timestamp);
CREATE INDEX IF NOT EXISTS ix_events_map_ts ON events(map_id, timestamp);
CREATE TABLE IF NOT EXISTS map_images (
    map_id INTEGER PRIMARY KEY, name TEXT, captured_at TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS voice_tokens (
    token TEXT PRIMARY KEY, character_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
);
"""


def iso(dt: datetime) -> str:
    """Normalise to UTC, second resolution, so strings sort chronologically."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return iso(datetime.now(timezone.utc))


def _aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return v


class EventIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=200)
    character_id: str = Field(min_length=1, max_length=200)
    event_type: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z0-9_]+$")
    timestamp: datetime
    payload: Dict[str, Any] = Field(default_factory=dict)

    _tz = field_validator("timestamp")(_aware)


class BatchIn(BaseModel):
    schema_version: int
    device_id: str = Field(min_length=1, max_length=100)
    events: List[EventIn] = Field(min_length=1, max_length=1000)

    @field_validator("schema_version")
    @classmethod
    def _version(cls, v: int) -> int:
        if v != 1:
            raise ValueError("unsupported schema_version")
        return v


class SnapshotIn(BaseModel):
    captured_at: datetime
    device_id: str = Field(min_length=1, max_length=100)
    data: Dict[str, Any]

    _tz = field_validator("captured_at")(_aware)


class Scope(str):
    """The character a limited token is looking at. `all` holds every character it may see."""
    all: frozenset = frozenset()


def create_app(
    db_path: Optional[str] = None,
    write_token: Optional[str] = None,
    read_token: Optional[str] = None,
    world_db: Optional[str] = None,
) -> FastAPI:
    db_path = db_path or os.environ.get("AH_DB_PATH", "data/adventure.db")
    write_token = write_token or os.environ.get("AH_API_TOKEN", "")
    read_token = read_token or os.environ.get("AH_READ_TOKEN", "")
    if not write_token:
        raise RuntimeError("AH_API_TOKEN is not set")

    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

    @contextmanager
    def db():
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:  # one transaction: commit on success, rollback on error
                yield conn
        finally:
            conn.close()

    with db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        # tokens used to belong to one character each: carry those over to the new table
        conn.execute("INSERT OR IGNORE INTO token_characters SELECT character_id, token, COALESCE(bound_at, created_at) "
                     "FROM upload_tokens WHERE character_id IS NOT NULL")
        conn.execute("UPDATE upload_tokens SET character_id=NULL, bound_at=NULL WHERE character_id IS NOT NULL")

    def _token(authorization: Optional[str]) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "missing bearer token")
        return authorization[7:]

    def _eq(a: str, b: str) -> bool:
        return bool(b) and hmac.compare_digest(a.encode(), b.encode())

    def can_write(authorization: Optional[str] = Header(None)) -> Optional[str]:
        """Returns None for the owner's token, which may write anything, or the personal token
        that was used: one person's, good for all of that person's characters (see `claim`)."""
        t = _token(authorization)
        if _eq(t, write_token):
            return None
        with db() as conn:
            if conn.execute("SELECT 1 FROM upload_tokens WHERE token=?", (t,)).fetchone():
                return t
        raise HTTPException(403, "invalid token")

    def owner_only(authorization: Optional[str] = Header(None)) -> None:
        if not _eq(_token(authorization), write_token):
            raise HTTPException(403, "only the server's own token may do this")

    def claim(writer: Optional[str], character_id: str) -> None:
        """A character belongs to the first personal token that uploads it. That token may go
        on to upload any number of characters; another person's token is refused for this one,
        so two people's data can never end up mixed under one character."""
        if writer is None:
            return
        with db() as conn:
            if not conn.execute("SELECT 1 FROM upload_tokens WHERE token=?", (writer,)).fetchone():
                raise HTTPException(403, "invalid token")
            row = conn.execute("SELECT token FROM token_characters WHERE character_id=?", (character_id,)).fetchone()
            if row is None:
                conn.execute("INSERT INTO token_characters VALUES (?,?,?)", (character_id, writer, now_iso()))
            elif row["token"] != writer:
                raise HTTPException(409, "this character belongs to another token")
            conn.execute("UPDATE upload_tokens SET last_used_at=? WHERE token=?", (now_iso(), writer))

    def can_read(request: Request, authorization: Optional[str] = Header(None)) -> Optional[Scope]:
        """Returns None for full read access, or what a limited token may see.

        A character's short token (the one used for /voice) shows that one character. A personal
        token shows every character it has uploaded: the result is the one being asked about
        (so the checks written for a single character keep working) and `.all` holds them all.
        Neither ever shows chat messages.
        """
        t = _token(authorization)
        if _eq(t, write_token) or _eq(t, read_token):
            return None
        with db() as conn:
            row = conn.execute("SELECT character_id FROM voice_tokens WHERE token=?", (t,)).fetchone()
            mine = [row["character_id"]] if row else [r["character_id"] for r in conn.execute(
                "SELECT character_id FROM token_characters WHERE token=? ORDER BY claimed_at, character_id", (t,))]
        if not mine:
            raise HTTPException(403, "invalid token")
        # Asked about a character that is not theirs, the answer is about one that is (or a
        # refusal, where the endpoint checks): never about someone else's.
        asked = request.path_params.get("character_id") or request.query_params.get("character_id")
        scope = Scope(asked if asked in mine else mine[0])
        scope.all = frozenset(mine)
        return scope

    def whose(character_id: Optional[str] = None, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
        """What a token is: the owner's, a personal one (with the characters it holds), or
        read-only. With character_id, also whether it may upload for that character."""
        t = _token(authorization)
        if _eq(t, write_token):
            return {"kind": "owner", "characters": [], "can_upload": True}
        if _eq(t, read_token):
            return {"kind": "read", "characters": [], "can_upload": False}
        with db() as conn:
            if conn.execute("SELECT 1 FROM upload_tokens WHERE token=?", (t,)).fetchone():
                mine = [{"id": r["character_id"], "name": r["name"]} for r in conn.execute(
                    "SELECT o.character_id, c.name FROM token_characters o LEFT JOIN characters c ON c.id=o.character_id "
                    "WHERE o.token=? ORDER BY o.claimed_at", (t,))]
                taken = conn.execute("SELECT token FROM token_characters WHERE character_id=?", (character_id,)).fetchone() \
                    if character_id else None
                return {"kind": "personal", "characters": mine, "can_upload": taken is None or taken["token"] == t}
            row = conn.execute("SELECT v.character_id, c.name FROM voice_tokens v LEFT JOIN characters c "
                               "ON c.id=v.character_id WHERE v.token=?", (t,)).fetchone()
        if row is None:
            raise HTTPException(403, "invalid token")
        return {"kind": "read", "characters": [{"id": row["character_id"], "name": row["name"]}], "can_upload": False}

    def allow(scope: Optional[str], character_id: Optional[str]) -> None:
        if scope is not None and character_id != scope:
            raise HTTPException(403, "this link only covers one character")

    app = FastAPI(title="WoW Adventure Hub", version=VERSION)

    @app.middleware("http")
    async def guard_headers(request: Request, call_next):
        """Share links carry their token in the address: never pass it on to other sites, never
        let a page be framed elsewhere, and never let a browser guess a file's type."""
        response = await call_next(request)
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    @app.get("/", include_in_schema=False)
    def dashboard():
        # Static page with no data in it; it calls the API with the viewer's token.
        return FileResponse(os.path.join(os.path.dirname(__file__), "dashboard.html"),
                            media_type="text/html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/v1/token")
    def token_info(info: Dict[str, Any] = Depends(whose)):
        """Lets the sync program check a token before it uploads with it."""
        return info

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok", "version": VERSION}

    def touch_device(conn, device_id: str) -> None:
        n = now_iso()
        conn.execute(
            "INSERT INTO devices(device_id, first_seen_at, last_seen_at) VALUES(?,?,?) "
            "ON CONFLICT(device_id) DO UPDATE SET last_seen_at=excluded.last_seen_at",
            (device_id, n, n),
        )

    @app.post("/api/v1/events/batch")
    def post_events(batch: BatchIn, writer: Optional[str] = Depends(can_write)):
        for character_id in sorted({e.character_id for e in batch.events}):
            claim(writer, character_id)
        received = now_iso()
        accepted = 0
        with db() as conn:
            touch_device(conn, batch.device_id)
            for e in batch.events:
                map_id = e.payload.get("map_id")
                cur = conn.execute(
                    "INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?,?)",
                    (
                        e.event_id, e.character_id, batch.device_id, e.event_type,
                        iso(e.timestamp), map_id if isinstance(map_id, int) else None,
                        json.dumps(e.payload, ensure_ascii=False), received,
                    ),
                )
                accepted += cur.rowcount
        return {
            "success": True, "accepted": accepted,
            "duplicates": len(batch.events) - accepted, "rejected": 0,
            "server_time": received,
        }

    @app.put("/api/v1/characters/{character_id}/snapshot")
    def put_snapshot(character_id: str, snap: SnapshotIn, writer: Optional[str] = Depends(can_write)):
        claim(writer, character_id)
        captured = iso(snap.captured_at)
        received = now_iso()
        d = snap.data
        with db() as conn:
            touch_device(conn, snap.device_id)
            row = conn.execute(
                "SELECT captured_at FROM character_snapshots WHERE character_id=?",
                (character_id,),
            ).fetchone()
            # Conflict policy: strictly newer wins; same or older time keeps the stored one.
            if row and row["captured_at"] >= captured:
                return {"success": True, "applied": False, "captured_at": row["captured_at"]}
            conn.execute(
                "INSERT OR REPLACE INTO character_snapshots VALUES(?,?,?,?,?)",
                (character_id, captured, snap.device_id,
                 json.dumps(d, ensure_ascii=False), received),
            )
            lvl = d.get("level")
            conn.execute(
                "INSERT INTO characters VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "name=excluded.name, realm=excluded.realm, guid=excluded.guid, race=excluded.race, "
                "class=excluded.class, faction=excluded.faction, level=excluded.level, "
                "updated_at=excluded.updated_at",
                (character_id, d.get("name"), d.get("realm"), d.get("guid"), d.get("race"),
                 d.get("class"), d.get("faction"), lvl if isinstance(lvl, int) else None,
                 received, received),
            )
        return {"success": True, "applied": True, "captured_at": captured}

    @app.get("/api/v1/characters")
    def list_characters(scope: Optional[str] = Depends(can_read)):
        with db() as conn:
            rows = conn.execute("SELECT * FROM characters ORDER BY id").fetchall()
        return {"items": [dict(r) for r in rows if scope is None or r["id"] in scope.all]}

    @app.get("/api/v1/characters/{character_id}")
    def get_character(character_id: str, scope: Optional[str] = Depends(can_read)):
        allow(scope, character_id)
        with db() as conn:
            row = conn.execute("SELECT * FROM characters WHERE id=?", (character_id,)).fetchone()
        if not row:
            raise HTTPException(404, "character not found")
        return dict(row)

    @app.get("/api/v1/characters/{character_id}/snapshot")
    def get_snapshot(character_id: str, scope: Optional[str] = Depends(can_read)):
        allow(scope, character_id)
        with db() as conn:
            row = conn.execute(
                "SELECT * FROM character_snapshots WHERE character_id=?", (character_id,)
            ).fetchone()
        if not row:
            raise HTTPException(404, "snapshot not found")
        return {
            "character_id": row["character_id"], "captured_at": row["captured_at"],
            "received_at": row["received_at"], "device_id": row["device_id"],
            "data": json.loads(row["payload_json"]),
        }

    def query_events(character_id, event_type, start_time, end_time, map_id, limit, cursor,
                     exclude_type=None, scope=None):
        where, args = [], []
        if scope is not None:   # share link: one character, no chat
            character_id = scope
            where.append("event_type<>'CHAT_MESSAGE'")
        if exclude_type:                               # one type, or several separated by commas
            kinds = [k for k in exclude_type.split(",") if k]
            where.append("event_type NOT IN (%s)" % ",".join("?" * len(kinds)))
            args += kinds
        for col, val in (("character_id", character_id), ("event_type", event_type),
                         ("map_id", map_id)):
            if val is not None:
                where.append(f"{col}=?")
                args.append(val)
        if start_time:
            where.append("timestamp>=?")
            args.append(iso(_aware_q(start_time)))
        if end_time:
            where.append("timestamp<=?")
            args.append(iso(_aware_q(end_time)))
        if cursor:
            try:
                ts, eid = base64.urlsafe_b64decode(cursor.encode()).decode().split("|", 1)
            except Exception:
                raise HTTPException(400, "invalid cursor")
            where.append("(timestamp<? OR (timestamp=? AND event_id<?))")
            args += [ts, ts, eid]
        sql = "SELECT * FROM events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY timestamp DESC, event_id DESC LIMIT ?"
        with db() as conn:
            rows = conn.execute(sql, args + [limit + 1]).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        items = [
            {"event_id": r["event_id"], "character_id": r["character_id"],
             "device_id": r["device_id"], "event_type": r["event_type"],
             "timestamp": r["timestamp"], "received_at": r["received_at"],
             "payload": json.loads(r["payload_json"])}
            for r in rows
        ]
        nxt = None
        if more:
            last = rows[-1]
            nxt = base64.urlsafe_b64encode(
                f"{last['timestamp']}|{last['event_id']}".encode()).decode()
        return {"items": items, "next_cursor": nxt}

    def _aware_q(v: datetime) -> datetime:
        if v.tzinfo is None:
            raise HTTPException(400, "time filters must include a timezone")
        return v

    @app.get("/api/v1/events")
    def get_events(
        character_id: Optional[str] = None, event_type: Optional[str] = None,
        start_time: Optional[datetime] = None, end_time: Optional[datetime] = None,
        map_id: Optional[int] = None, limit: int = Query(100, ge=1, le=1000),
        cursor: Optional[str] = None, exclude_type: Optional[str] = None,
        scope: Optional[str] = Depends(can_read),
    ):
        return query_events(character_id, event_type, start_time, end_time, map_id, limit, cursor,
                            exclude_type, scope)

    @app.get("/api/v1/locations")
    def get_locations(
        character_id: Optional[str] = None,
        start_time: Optional[datetime] = None, end_time: Optional[datetime] = None,
        map_id: Optional[int] = None, limit: int = Query(100, ge=1, le=1000),
        cursor: Optional[str] = None, scope: Optional[str] = Depends(can_read),
    ):
        return query_events(character_id, "LOCATION_SAMPLE", start_time, end_time,
                            map_id, limit, cursor, None, scope)

    maps_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)), "maps")
    os.makedirs(maps_dir, exist_ok=True)

    @app.put("/api/v1/maps/{map_id}/image", dependencies=[Depends(can_write)])
    async def put_map_image(map_id: int, request: Request, name: Optional[str] = None,
                            captured_at: Optional[datetime] = None):
        """Body is a JPEG covering the whole map (top-left = 0,0; bottom-right = 100,100)."""
        body = await request.body()
        if len(body) > 8 * 1024 * 1024:
            raise HTTPException(413, "image too large")
        if body[:3] != b"\xff\xd8\xff":
            raise HTTPException(400, "body must be a JPEG image")
        tmp = os.path.join(maps_dir, f"{map_id}.jpg.tmp")
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, os.path.join(maps_dir, f"{map_id}.jpg"))
        updated = now_iso()
        with db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO map_images VALUES(?,?,?,?)",
                (map_id, name, iso(_aware_q(captured_at)) if captured_at else None, updated))
        return {"success": True, "map_id": map_id, "updated_at": updated}

    @app.get("/api/v1/maps", dependencies=[Depends(can_read)])
    def list_maps():
        with db() as conn:
            rows = conn.execute("SELECT * FROM map_images ORDER BY map_id").fetchall()
        return {"items": [dict(r) for r in rows]}

    @app.get("/api/v1/maps/{map_id}/image", dependencies=[Depends(can_read)])
    def get_map_image(map_id: int):
        path = os.path.join(maps_dir, f"{map_id}.jpg")
        if not os.path.isfile(path):
            raise HTTPException(404, "no image for this map")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/v1/stats/activity")
    def activity_api(character_id: Optional[str] = None, start_time: Optional[datetime] = None,
                     end_time: Optional[datetime] = None, scope: Optional[str] = Depends(can_read)):
        return activity(scope if scope is not None else character_id, start_time, end_time)

    def activity(character_id: Optional[str] = None, start_time: Optional[datetime] = None,
                 end_time: Optional[datetime] = None):
        """Aggregates for a time range: totals, top monsters/items and the XP timeline."""
        where, args = ["1=1"], []
        if character_id:
            where.append("character_id=?")
            args.append(character_id)
        if start_time:
            where.append("timestamp>=?")
            args.append(iso(_aware_q(start_time)))
        if end_time:
            where.append("timestamp<=?")
            args.append(iso(_aware_q(end_time)))
        with db() as conn:
            rows = conn.execute(
                "SELECT event_type, timestamp, payload_json FROM events WHERE "
                + " AND ".join(where) + " ORDER BY timestamp, event_id", args).fetchall()

        def num(v):
            return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0

        counts: Counter = Counter()
        monsters: Dict[Any, Dict[str, Any]] = {}
        items: Dict[Any, Dict[str, Any]] = {}
        xp_points: List[List[Any]] = []
        xp = money_in = money_out = combat = active = looted = loot_value = 0
        prev = first = last = None
        for r in rows:
            kind = r["event_type"]
            counts[kind] += 1
            ts = datetime.strptime(r["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            first = first or r["timestamp"]
            last = r["timestamp"]
            # Time between events at most 5 minutes apart counts as time spent playing.
            if prev is not None and 0 < (ts - prev).total_seconds() <= 300:
                active += (ts - prev).total_seconds()
            prev = ts
            if kind not in ("MOB_KILLED", "ITEM_LOOTED", "XP_GAINED", "MONEY_CHANGED", "COMBAT"):
                continue
            p = json.loads(r["payload_json"])
            if kind == "MOB_KILLED":
                key = p.get("monster_id") or p.get("monster_name")
                m = monsters.setdefault(key, {"id": p.get("monster_id"), "name": None, "count": 0})
                m["name"] = m["name"] or p.get("monster_name")
                m["count"] += 1
            elif kind == "ITEM_LOOTED":
                if p.get("how") in ("created", "received") or p.get("item_name") in ROLL_LINES:
                    continue                      # crafted, bought or handed over: not loot
                key = p.get("item_id") or p.get("item_name")
                it = items.setdefault(key, {"id": p.get("item_id"), "name": None, "count": 0, "value": 0})
                it["name"] = it["name"] or p.get("item_name")
                n = num(p.get("count")) or 1
                it["count"] += n
                looted += n
                worth = num(p.get("sell_price")) * n      # what a vendor would pay; 0 when unknown
                it["value"] += worth
                loot_value += worth
            elif kind == "XP_GAINED":
                xp += num(p.get("amount"))
                xp_points.append([r["timestamp"], num(p.get("amount"))])
            elif kind == "MONEY_CHANGED":
                delta = num(p.get("delta"))
                money_in += max(delta, 0)
                money_out += max(-delta, 0)
            else:
                combat += num(p.get("duration"))
        if len(xp_points) > 2000:   # merge neighbours so the chart payload stays small
            step = math.ceil(len(xp_points) / 2000)
            xp_points = [[chunk[-1][0], sum(c[1] for c in chunk)]
                         for chunk in (xp_points[i:i + step] for i in range(0, len(xp_points), step))]

        def top(d):
            return sorted(d.values(), key=lambda v: (-v["count"], str(v["name"])))[:15]

        return {
            "first_event_at": first, "last_event_at": last, "counts": dict(counts),
            "kills": counts["MOB_KILLED"], "fights": counts["COMBAT"], "combat_seconds": combat,
            "active_seconds": int(active), "xp": xp, "items_looted": looted, "loot_value": loot_value,
            "money_in": money_in, "money_out": money_out,
            "top_monsters": top(monsters), "top_items": top(items), "xp_points": xp_points,
        }

    @app.get("/api/v1/stats/summary")
    def stats(scope: Optional[str] = Depends(can_read)):
        w, a = ("", []) if scope is None else \
            (" WHERE character_id=? AND event_type<>'CHAT_MESSAGE'", [scope])
        with db() as conn:
            ev = conn.execute("SELECT COUNT(*) c, MAX(received_at) r, MAX(timestamp) t FROM events" + w, a).fetchone()
            ch = conn.execute("SELECT COUNT(*) c FROM characters").fetchone()
            by = conn.execute("SELECT event_type, COUNT(*) c FROM events" + w + " GROUP BY event_type", a).fetchall()
        return {
            "characters": ch["c"] if scope is None else 1, "events": ev["c"], "last_event_at": ev["t"],
            "last_received_at": ev["r"], "by_type": {r["event_type"]: r["c"] for r in by},
            "server_time": now_iso(),
        }

    from . import voice, world as world_api
    world = world_api.World(world_db or os.environ.get("AH_WORLD_DB")
                            or os.path.join(os.path.dirname(os.path.abspath(db_path)), "world.db"))
    world_api.register(app, world, db, can_read)
    voice.register(app, db, can_write, activity, world, claim)
    from . import combat, groups, setup, shots as shots_api
    setup.register(app, owner_only, os.path.join(os.path.dirname(os.path.abspath(db_path)), "builds"))
    combat.register(app, db, can_write, claim)
    from . import admin
    admin.register(app, db)
    shots = shots_api.register(app, db, can_read, can_write, claim,
                               os.path.join(os.path.dirname(os.path.abspath(db_path)), "shots"))
    groups.register(app, db, can_read, activity, maps_dir, world, shots)
    from . import items
    items.register(app, db, can_read, world)
    return app
