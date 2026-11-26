"""AdventureSync core: config, discovery, local queue, ingest, upload."""
import json
import logging
import sqlite3
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

from . import luaparser

log = logging.getLogger("adventuresync")
SV_NAME = "AdventureCollector.lua"
DB_GLOBAL = "AdventureCollectorDB"


# ---------------------------------------------------------------- config
@dataclass
class Config:
    url: str
    api_token: str
    timeout: float = 10
    device_id: str = "device"
    wow_path: str = ""
    sync_interval: int = 30
    batch_size: int = 100
    db_path: str = "~/.adventuresync/queue.db"
    log_level: str = "INFO"
    # api_token is one person's token and covers every character on this computer. A character
    # can be given a different one here (character id -> token): two people sharing a computer.
    tokens: Dict[str, str] = field(default_factory=dict)


def load_config(path: Path) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    server, client = raw.get("server", {}), raw.get("client", {})
    tokens = {str(k): str(v).strip() for k, v in (raw.get("characters") or {}).items() if str(v).strip()}
    if not server.get("url"):
        raise ValueError("config: [server] url is required")
    if not server.get("api_token") and not tokens:
        raise ValueError("config: [server] api_token is required (or a token under [characters])")
    cfg = Config(
        tokens=tokens,
        url=str(server["url"]).rstrip("/"),
        api_token=str(server.get("api_token") or ""),
        timeout=float(server.get("timeout", 10)),
        device_id=str(client.get("device_id", "device")),
        wow_path=str(client.get("wow_path", "")),
        sync_interval=int(client.get("sync_interval", 30)),
        batch_size=int(client.get("batch_size", 100)),
        db_path=str(client.get("db_path", "~/.adventuresync/queue.db")),
        log_level=str(raw.get("logging", {}).get("level", "INFO")),
    )
    if not 1 <= cfg.batch_size <= 1000:
        raise ValueError("config: batch_size must be 1..1000")
    return cfg


def _toml(value: str) -> str:
    """A TOML string. Paths go in single quotes so their backslashes stay as they are."""
    if "\\" in value and "'" not in value and "\n" not in value:
        return "'" + value + "'"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_config(path: Path, cfg: Config) -> None:
    """Write the settings file. It holds the token, so only its owner may read it."""
    text = "\n".join([
        "[server]", f"url = {_toml(cfg.url)}", f"api_token = {_toml(cfg.api_token)}", f"timeout = {cfg.timeout:g}", "",
        "[client]", f"device_id = {_toml(cfg.device_id)}", "# Leave empty to find the game automatically.",
        f"wow_path = {_toml(cfg.wow_path)}", f"sync_interval = {cfg.sync_interval}", f"batch_size = {cfg.batch_size}",
        f"db_path = {_toml(cfg.db_path)}", "", "[logging]", f"level = {_toml(cfg.log_level)}", "",
        "# One token per character (character id = token). Others use api_token above.",
        "[characters]", *[f"{_toml(k)} = {_toml(v)}" for k, v in sorted(cfg.tokens.items()) if v], ""])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        tmp.chmod(0o600)
    except OSError:
        pass                                          # not every system has these permissions
    tmp.replace(path)


# ------------------------------------------------------------- discovery
DEFAULT_WOW_PATHS = [
    "/Applications/World of Warcraft",
    "C:/Program Files (x86)/World of Warcraft",
    "C:/Program Files/World of Warcraft",
]
# Where people put the game on a drive of their own choosing (Windows: tried on every fixed drive).
WINDOWS_FOLDERS = ["World of Warcraft", "Games/World of Warcraft", "Program Files (x86)/World of Warcraft",
                   "Program Files/World of Warcraft"]


def _windows_drives() -> List[str]:
    """Letters of the fixed drives. Removable and network drives are left alone: asking an
    empty card reader or an unreachable share can hang or pop up a dialog."""
    import ctypes
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    return [c for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
            if mask >> i & 1 and ctypes.windll.kernel32.GetDriveTypeW(f"{c}:\\") == 3]


def _windows_registry() -> List[str]:
    """The folder the game's own installer wrote down, when it did."""
    import winreg
    found = []
    for key in (r"SOFTWARE\WOW6432Node\Blizzard Entertainment\World of Warcraft",
                r"SOFTWARE\Blizzard Entertainment\World of Warcraft"):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
                value = str(winreg.QueryValueEx(k, "InstallPath")[0]).rstrip("\\/")
        except OSError:
            continue
        # It names one game version's folder (…/World of Warcraft/_retail_): the others sit beside it.
        found += [str(Path(value).parent), value] if Path(value).name.startswith("_") else [value]
    return found


def wow_roots(wow_path: str = "") -> List[Path]:
    """Folders to look for the game in: the chosen one, or else the usual places."""
    if wow_path:
        return [Path(wow_path).expanduser()]
    places = list(DEFAULT_WOW_PATHS)
    if sys.platform.startswith("win"):
        try:
            places = _windows_registry() + places + [f"{d}:/{f}" for d in _windows_drives() for f in WINDOWS_FOLDERS]
        except Exception as e:                        # looking further is a convenience, never a reason to fail
            log.debug("could not look for the game beyond the usual folders: %s", e)
    roots, seen = [], set()
    for place in places:
        # The same folder written two ways (D:\Games\x from the registry, D:/Games/x from the list)
        # is one place. Compared as text, so it gives the same answer on every system.
        key = place.replace("\\", "/").rstrip("/").lower()
        if key not in seen:
            seen.add(key)
            roots.append(Path(place))
    return roots


def find_saved_variables(wow_path: str) -> List[Path]:
    """All AdventureCollector.lua files under <wow>/_*_/WTF/Account/*/SavedVariables."""
    found: List[Path] = []
    for root in wow_roots(wow_path):
        if not root.is_dir():
            continue
        bases = [root] if (root / "WTF").is_dir() else             [d for d in root.iterdir() if d.is_dir() and (d / "WTF").is_dir()]
        for base in bases:
            found += [f for f in sorted((base / "WTF" / "Account").glob(f"*/SavedVariables/{SV_NAME}")) if f not in found]
    return found


def read_stable(path: Path, settle: float = 0.5, attempts: int = 5) -> Dict[str, Any]:
    """Read and parse only once the file stopped changing; retry on partial writes."""
    last: Optional[Exception] = None
    for _ in range(attempts):
        try:
            before = path.stat()
            time.sleep(settle)
            data = path.read_bytes()
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) \
                    or len(data) != after.st_size:
                last = RuntimeError("file is still being written")
                continue
            return luaparser.loads(data.decode("utf-8", errors="replace"))
        except (OSError, luaparser.LuaParseError) as e:
            last = e
            time.sleep(settle)
    raise RuntimeError(f"could not read {path}: {last}")


# ------------------------------------------------------------ normalise
def epoch_to_iso(ts: Any) -> str:
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not 946684800 <= ts < 4102444800:
        raise ValueError(f"bad epoch timestamp {ts!r}")
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _obj(v: Any) -> Dict[str, Any]:
    """An empty Lua table parses as []; payloads are always objects."""
    if isinstance(v, dict):
        return {str(k): val for k, val in v.items()}
    if v in (None, []):
        return {}
    raise ValueError("expected a table with named keys")


def normalize_event(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("event is not a table")
    for key in ("event_id", "character_id", "event_type"):
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise ValueError(f"event missing {key}")
    return {
        "event_id": raw["event_id"], "character_id": raw["character_id"],
        "event_type": raw["event_type"], "timestamp": epoch_to_iso(raw.get("ts")),
        "payload": _obj(raw.get("payload")),
    }


# ----------------------------------------------------------------- queue
class Queue:
    def __init__(self, path: str):
        p = Path(path).expanduser()
        if str(path) != ":memory:":
            p.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path) if str(path) == ":memory:" else str(p))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS pending_events (
                event_id TEXT PRIMARY KEY, character_id TEXT NOT NULL,
                payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                retry_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                uploaded_at TEXT, last_error TEXT);
            CREATE INDEX IF NOT EXISTS ix_pending_status ON pending_events(status);
            CREATE TABLE IF NOT EXISTS map_shots (
                event_id TEXT PRIMARY KEY, status TEXT NOT NULL, detail TEXT);
            CREATE TABLE IF NOT EXISTS snapshots (
                character_id TEXT PRIMARY KEY, captured_at TEXT NOT NULL,
                payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                last_error TEXT);
        """)
        self.conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def add_events(self, events: List[Dict[str, Any]]) -> int:
        before = self.conn.total_changes
        self.conn.executemany(
            "INSERT OR IGNORE INTO pending_events(event_id, character_id, payload_json, created_at) "
            "VALUES(?,?,?,?)",
            [(e["event_id"], e["character_id"], json.dumps(e, ensure_ascii=False), self._now())
             for e in events])
        self.conn.commit()
        return self.conn.total_changes - before

    def add_snapshot(self, character_id: str, captured_at: str, data: Dict[str, Any]) -> bool:
        row = self.conn.execute(
            "SELECT captured_at FROM snapshots WHERE character_id=?", (character_id,)).fetchone()
        if row and row["captured_at"] >= captured_at:
            return False
        self.conn.execute(
            "INSERT OR REPLACE INTO snapshots(character_id, captured_at, payload_json, status) "
            "VALUES(?,?,?, 'pending')",
            (character_id, captured_at, json.dumps(data, ensure_ascii=False)))
        self.conn.commit()
        return True

    def pending_events(self, limit: int, character_id: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT payload_json FROM pending_events WHERE status='pending' AND (? IS NULL OR character_id=?) "
            "ORDER BY rowid LIMIT ?", (character_id, character_id, limit)).fetchall()
        return [json.loads(r["payload_json"]) for r in rows]

    def pending_characters(self) -> List[str]:
        """Characters with events waiting, oldest waiting first."""
        return [r[0] for r in self.conn.execute(
            "SELECT character_id FROM pending_events WHERE status='pending' GROUP BY character_id ORDER BY MIN(rowid)")]

    def pending_snapshots(self) -> List[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM snapshots WHERE status='pending'").fetchall()

    def mark_events(self, ids: List[str], status: str, error: Optional[str] = None) -> None:
        self.conn.executemany(
            "UPDATE pending_events SET status=?, uploaded_at=?, last_error=? WHERE event_id=?",
            [(status, self._now() if status == "uploaded" else None, error, i) for i in ids])
        self.conn.commit()

    def note_retry(self, ids: List[str], error: str) -> None:
        self.conn.executemany(
            "UPDATE pending_events SET retry_count=retry_count+1, last_error=? WHERE event_id=?",
            [(error, i) for i in ids])
        self.conn.commit()

    def mark_snapshot(self, character_id: str, captured_at: str, status: str,
                      error: Optional[str] = None) -> None:
        self.conn.execute(
            "UPDATE snapshots SET status=?, last_error=? WHERE character_id=? AND captured_at=?",
            (status, error, character_id, captured_at))
        self.conn.commit()

    def map_shot_done(self, event_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM map_shots WHERE event_id=?", (event_id,)).fetchone() is not None

    def mark_map_shot(self, event_id: str, status: str, detail: str = "") -> None:
        self.conn.execute("INSERT OR REPLACE INTO map_shots VALUES(?,?,?)", (event_id, status, detail))
        self.conn.commit()

    def counts(self) -> Dict[str, int]:
        out = {"pending": 0, "uploaded": 0, "rejected": 0}
        for r in self.conn.execute("SELECT status, COUNT(*) c FROM pending_events GROUP BY status"):
            out[r["status"]] = r["c"]
        out["pending_snapshots"] = self.conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE status='pending'").fetchone()[0]
        return out


# ---------------------------------------------------------------- ingest
def ingest(parsed: Dict[str, Any], queue: Queue) -> Tuple[int, int, int]:
    """Queue everything in a parsed SavedVariables file.

    Returns (new_events, new_snapshots, invalid_events)."""
    db = parsed.get(DB_GLOBAL)
    if not isinstance(db, dict):
        return 0, 0, 0
    events, invalid = [], 0
    raw_events = db.get("events") or []
    if isinstance(raw_events, dict):  # sparse array after trimming
        raw_events = [raw_events[k] for k in sorted(k for k in raw_events if isinstance(k, int))]
    for raw in raw_events:
        try:
            events.append(normalize_event(raw))
        except ValueError as e:
            invalid += 1
            log.warning("skipping invalid event: %s", e)
    new_events = queue.add_events(events)
    new_snaps = 0
    chars = db.get("characters")
    if isinstance(chars, dict):
        for cid, snap in chars.items():
            try:
                captured = epoch_to_iso(snap.get("captured_at"))
                if queue.add_snapshot(str(cid), captured, _obj(snap.get("data"))):
                    new_snaps += 1
            except (ValueError, AttributeError) as e:
                log.warning("skipping invalid snapshot for %s: %s", cid, e)
    return new_events, new_snaps, invalid


# ---------------------------------------------------------------- upload
USER_AGENT = "AdventureSync/0.1"


def _outcome(r) -> Tuple[str, str]:
    """-> ('ok'|'invalid'|'retry', detail) for a response. Only 'invalid' means the data is bad.

    The server always answers errors with JSON that has a "detail". Anything else was written
    by something in between (a reverse proxy, a firewall, bot protection such as Cloudflare's),
    which says nothing about the data: it is kept and sent again later."""
    if r.status_code == 200:
        return "ok", ""
    try:
        ours = isinstance(r.json(), dict) and "detail" in r.json()
    except ValueError:
        ours = False
    if not ours:
        return "retry", (f"HTTP {r.status_code} did not come from the Adventure Hub server: something in front of it "
                         f"(a proxy, firewall or bot protection) answered instead. If it blocks automated clients, "
                         f"allow the User-Agent \"{USER_AGENT}\" or the paths under /api/ there.")
    detail = f"HTTP {r.status_code}: {r.text[:200]}"
    if r.status_code == 409:
        # The character belongs to someone else's token. The data is fine: it waits here, like
        # that of a character with no token, until the right token is filled in.
        return "skip", "this character belongs to another token"
    return ("invalid" if r.status_code in (400, 413, 422) else "retry"), detail


class Uploader:
    def __init__(self, cfg: Config, queue: Queue, client: Optional[httpx.Client] = None):
        self.cfg, self.queue = cfg, queue
        # A name of its own, so a proxy or firewall in front of the server can be told to let it through.
        self.client = client or httpx.Client(base_url=cfg.url, timeout=cfg.timeout,
                                             headers={"User-Agent": USER_AGENT})
        self.headers = {"Authorization": f"Bearer {cfg.api_token}"} if cfg.api_token else {}
        self._settings: Dict[str, Tuple[float, Dict[str, Any]]] = {}     # token -> (when asked, answer)

    # ---- what the player chose, kept on the server per token (sync-settings)
    SETTINGS_EVERY = 60.0           # seconds between asking again: a change made elsewhere arrives within a minute

    def _token_of(self, character_id: Optional[str]) -> str:
        return self.cfg.tokens.get(character_id or "") or self.cfg.api_token or next(iter(self.cfg.tokens.values()), "")

    def settings(self, character_id: Optional[str] = None, fresh: bool = False) -> Dict[str, Any]:
        """The server's settings for the token this character uploads with. Off when the server
        cannot be asked and never answered before, or is from before these settings existed."""
        token = self._token_of(character_id)
        if not token:
            return {"upload_screenshots": False}
        held = self._settings.get(token)
        if held and not fresh and time.monotonic() - held[0] < self.SETTINGS_EVERY:
            return held[1]
        try:
            r = self.client.get("/api/v1/sync-settings", headers={"Authorization": f"Bearer {token}"})
            answer = r.json() if r.status_code == 200 else {"upload_screenshots": False}
        except (httpx.HTTPError, ValueError):
            answer = held[1] if held else {"upload_screenshots": False}       # keep the last answer
        self._settings[token] = (time.monotonic(), answer)
        return answer

    def screenshots_on(self, character_id: Optional[str] = None) -> bool:
        return bool(self.settings(character_id).get("upload_screenshots"))

    def set_screenshots(self, on: bool) -> List[str]:
        """Turn screenshot upload on or off for every token this computer uses."""
        return self.set_settings(upload_screenshots=bool(on))

    def set_settings(self, **changes: Any) -> List[str]:
        """Change settings (upload_screenshots, keep_screenshots) for every token this computer
        uses. Returns the problems, one per token that could not be changed (empty: all done)."""
        problems = []
        for token in dict.fromkeys(t for t in [self.cfg.api_token, *self.cfg.tokens.values()] if t):
            try:
                r = self.client.put("/api/v1/sync-settings", json=changes,
                                    headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as e:
                problems.append(f"連不上伺服器（{type(e).__name__}）")
                continue
            if r.status_code == 200:
                self._settings[token] = (time.monotonic(), r.json())
            else:
                problems.append("伺服器還不支援這個設定，請先更新伺服器" if r.status_code in (404, 405) else _outcome(r)[1])
        return problems

    def auth(self, character_id: Optional[str]) -> Optional[Dict[str, str]]:
        """The header to upload one character's data with: its own token, else the general one.
        None when there is neither: that character's data stays here until a token is given."""
        token = self.cfg.tokens.get(character_id or "") or self.cfg.api_token
        return {"Authorization": f"Bearer {token}"} if token else None

    def _any_auth(self) -> Dict[str, str]:
        """For things that belong to no character (a map image): the general token, or any."""
        return self.headers or next(({"Authorization": f"Bearer {t}"} for t in self.cfg.tokens.values()), {})

    def test_connection(self) -> Dict[str, Any]:
        """Is the server there, and what does it make of each token? `tokens` lists them:
        character_id is None for the general one; `characters` are the ones the token holds."""
        r = self.client.get("/api/v1/health")
        if r.status_code != 200:
            raise RuntimeError(_outcome(r)[1])
        health, checked = r.json(), []
        for character_id, token in [(None, self.cfg.api_token)] + sorted(self.cfg.tokens.items()):
            if not token:
                continue
            auth = {"Authorization": f"Bearer {token}"}
            who = self.client.get("/api/v1/token", headers=auth, params={"character_id": character_id} if character_id else None)
            info = who.json() if who.status_code == 200 else {}
            if who.status_code == 404:                  # a server from before this question existed
                who = self.client.get("/api/v1/stats/summary", headers=auth)
            checked.append({"character_id": character_id, "status": who.status_code, "kind": info.get("kind"),
                            "characters": info.get("characters") or [],
                            "ok": who.status_code == 200 and info.get("kind") != "read" and info.get("can_upload", True)})
        general = next((c for c in checked if c["character_id"] is None), None)
        return {"health": health, "tokens": checked, "token_ok": bool(checked) and all(c["ok"] for c in checked),
                "auth_status": general["status"] if general else (checked[0]["status"] if checked else 0)}

    def _send(self, method: str, path: str, body: Dict[str, Any], character_id: Optional[str] = None) -> Tuple[str, str]:
        """-> ('ok'|'invalid'|'retry'|'skip', detail). Only 'invalid' means the data itself is
        bad; 'skip' means there is no token to send this character's data with."""
        headers = self.auth(character_id)
        if headers is None:
            return "skip", "no token for this character"
        try:
            r = self.client.request(method, path, json=body, headers=headers)
        except httpx.HTTPError as e:
            return "retry", f"network: {type(e).__name__}"
        return _outcome(r)

    def upload_map(self, map_id: int, name: Optional[str], captured_at: str,
                   jpeg: bytes) -> Tuple[str, str]:
        params = {"captured_at": captured_at}
        if name:
            params["name"] = name
        try:
            r = self.client.put(f"/api/v1/maps/{int(map_id)}/image", content=jpeg, params=params,
                                headers={**self._any_auth(), "Content-Type": "image/jpeg"})
        except httpx.HTTPError as e:
            return "retry", f"network: {type(e).__name__}"
        return _outcome(r)

    def upload_screenshot(self, shot_id: str, character_id: str, taken_at: str, width: int, height: int,
                          full: bytes, thumb: bytes) -> Tuple[str, str]:
        """Send a screenshot and its thumbnail. Same result convention as _send."""
        auth = self.auth(character_id)
        if auth is None:
            return "skip", "no token for this character"
        jpeg = {**auth, "Content-Type": "image/jpeg"}
        try:
            r = self.client.put(f"/api/v1/screenshots/{shot_id}", content=full, headers=jpeg, params={
                "character_id": character_id, "taken_at": taken_at, "width": width, "height": height})
            if r.status_code == 200:
                r = self.client.put(f"/api/v1/screenshots/{shot_id}/thumb", content=thumb, headers=jpeg)
        except httpx.HTTPError as e:
            return "retry", f"network: {type(e).__name__}"
        return _outcome(r)

    def upload_hits(self, character_id: str, source: str, rows: List[List[Any]]) -> Tuple[str, str]:
        """Send rows read from a combat log file, a few thousand at a time."""
        for i in range(0, len(rows), 2000):
            state, detail = self._send("POST", "/api/v1/combat/hits", {
                "character_id": character_id, "source": source, "rows": rows[i:i + 2000]}, character_id)
            if state != "ok":
                return state, detail
        return "ok", ""

    def _send_events(self, events: List[Dict[str, Any]], character_id: Optional[str] = None) -> Tuple[str, str]:
        return self._send("POST", "/api/v1/events/batch", {
            "schema_version": 1, "device_id": self.cfg.device_id, "events": events}, character_id)

    def sync_once(self) -> Dict[str, Any]:
        """Send what is waiting, one character at a time, each with its own token. A character
        with no token is left waiting and reported in `waiting`; it never holds the others up."""
        result: Dict[str, Any] = {"uploaded": 0, "rejected": 0, "snapshots": 0, "error": None, "waiting": []}
        for row in self.queue.pending_snapshots():
            state, detail = self._send(
                "PUT", f"/api/v1/characters/{quote(row['character_id'], safe='')}/snapshot",
                {"captured_at": row["captured_at"], "device_id": self.cfg.device_id,
                 "data": json.loads(row["payload_json"])}, row["character_id"])
            if state == "skip":
                if row["character_id"] not in result["waiting"]:
                    result["waiting"].append(row["character_id"])
                continue
            if state == "retry":
                result["error"] = detail
                return result
            self.queue.mark_snapshot(row["character_id"], row["captured_at"],
                                     "uploaded" if state == "ok" else "rejected", detail or None)
            result["snapshots"] += state == "ok"
        for character in self.queue.pending_characters():
            if self.auth(character) is None:
                if character not in result["waiting"]:
                    result["waiting"].append(character)
                continue
            while True:
                batch = self.queue.pending_events(self.cfg.batch_size, character)
                if not batch:
                    break
                ids = [e["event_id"] for e in batch]
                state, detail = self._send_events(batch, character)
                if state == "skip":
                    if character not in result["waiting"]:
                        result["waiting"].append(character)
                    break
                if state == "ok":
                    self.queue.mark_events(ids, "uploaded")
                    result["uploaded"] += len(ids)
                elif state == "retry":
                    self.queue.note_retry(ids, detail)
                    result["error"] = detail
                    return result
                else:
                    # The server rejects a whole batch for one bad event; isolate it so
                    # a single invalid event can never block the queue.
                    for e in batch:
                        one, d = self._send_events([e], character) if len(batch) > 1 else (state, detail)
                        if one == "ok":
                            self.queue.mark_events([e["event_id"]], "uploaded")
                            result["uploaded"] += 1
                        elif one == "invalid":
                            self.queue.mark_events([e["event_id"]], "rejected", d)
                            result["rejected"] += 1
                            log.warning("event %s rejected: %s", e["event_id"], d)
                        else:
                            self.queue.note_retry([e["event_id"]], d)
                            result["error"] = d
                            return result
        return result


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tga"}


def find_screenshot(folder: Path, local_ts: float) -> Optional[Path]:
    """The screenshot written closest to (and not long after) the addon's timestamp."""
    best, best_diff = None, None
    if not folder.is_dir():
        return None
    for f in folder.iterdir():
        if f.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        diff = f.stat().st_mtime - local_ts
        if -5 <= diff <= 30 and (best_diff is None or abs(diff) < abs(best_diff)):
            best, best_diff = f, diff
    return best


def map_box(p: Dict[str, Any], w: int, h: int) -> Tuple[int, int, int, int]:
    """Pixel box of the map inside a w x h screenshot.

    The addon reports edges in UI units measured from the bottom-left of the
    screen plus the screen width in the same units. Pixels are square, so the
    width alone gives the scale; the height is taken from the image itself."""
    ppu = w / float(p["screen_w_u"])
    box = (round(float(p["left_u"]) * ppu), round(h - float(p["top_u"]) * ppu),
           round(float(p["right_u"]) * ppu), round(h - float(p["bottom_u"]) * ppu))
    if not (-3 <= box[0] < box[2] <= w + 3 and -3 <= box[1] < box[3] <= h + 3):
        raise ValueError(f"map area {box} is outside the {w}x{h} screenshot")
    return (max(box[0], 0), max(box[1], 0), min(box[2], w), min(box[3], h))


def crop_map(path: Path, payload: Optional[Dict[str, Any]] = None) -> bytes:
    """Crop the map area described by a MAP_SCREENSHOT payload (None = whole image) to JPEG."""
    import io
    from PIL import Image
    with Image.open(path) as img:
        img = img.convert("RGB")
        w, h = img.size
        box = map_box(payload, w, h) if payload else (0, 0, w, h)
        if box[2] - box[0] < 50 or box[3] - box[1] < 50:
            raise ValueError("map area is too small")
        img = img.crop(box)
        if img.width > 1600:
            img = img.resize((1600, round(img.height * 1600 / img.width)))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=85)
        return out.getvalue()


def process_map_shots(sv_path: Path, parsed: Dict[str, Any], queue: Queue,
                      uploader: "Uploader") -> int:
    """Turn MAP_SCREENSHOT events into uploaded map backgrounds. Returns how many uploaded."""
    db = parsed.get(DB_GLOBAL)
    events = db.get("events") if isinstance(db, dict) else None
    if not isinstance(events, list):
        return 0
    shots_dir = sv_path.parents[4] / "Screenshots"   # <flavor>/WTF/Account/X/SavedVariables/file
    done = 0
    for raw in events:
        if not isinstance(raw, dict) or raw.get("event_type") != "MAP_SCREENSHOT":
            continue
        eid, p = raw.get("event_id"), raw.get("payload")
        if not isinstance(eid, str) or queue.map_shot_done(eid):
            continue
        try:
            for k in ("left_u", "top_u", "right_u", "bottom_u", "screen_w_u"):
                float(p[k])
            map_id, local_ts = int(p["map_id"]), float(p["local_ts"])
            captured = epoch_to_iso(raw.get("ts"))
        except (TypeError, KeyError, ValueError) as e:
            queue.mark_map_shot(eid, "failed", f"bad event: {e}")
            continue
        shot = find_screenshot(shots_dir, local_ts)
        if shot is None:
            if time.time() - local_ts > 3600:
                queue.mark_map_shot(eid, "missing", "no screenshot file near that time")
            log.warning("map screenshot for map %s not found in %s", map_id, shots_dir)
            continue
        try:
            jpeg = crop_map(shot, p)
        except Exception as e:
            queue.mark_map_shot(eid, "failed", f"{shot.name}: {e}")
            log.warning("could not crop %s: %s", shot.name, e)
            continue
        state, detail = uploader.upload_map(map_id, p.get("map_name"), captured, jpeg)
        if state == "ok":
            queue.mark_map_shot(eid, "uploaded", shot.name)
            done += 1
            log.info("uploaded map background for map %s from %s", map_id, shot.name)
        elif state == "invalid":
            queue.mark_map_shot(eid, "failed", detail)
        else:
            log.warning("map upload will be retried: %s", detail)
    return done


_last_db: Dict[Path, Any] = {}      # newest parsed data per SavedVariables file


def scan_screenshots(queue: Queue, uploader: Optional["Uploader"]) -> int:
    """New screenshots in every game folder seen so far. Cheap when there are none: meant to be
    called every second or so, so that a screenshot reaches the server within a few seconds."""
    from . import extras
    sent = 0
    for path, db in list(_last_db.items()):
        try:
            sent += extras.process_screenshots(path.parents[4], db, queue, uploader)
        except Exception as e:      # never let a picture stop the sync
            log.warning("screenshots failed: %s", e)
    return sent


def scan_extras(path: Path, queue: Queue, uploader: Optional["Uploader"]) -> None:
    """The combat log, which changes without the SavedVariables file changing. (Screenshots
    are looked for more often, by scan_screenshots.)"""
    from . import extras
    game_dir = path.parents[4]                      # <flavor>/WTF/Account/X/SavedVariables/file
    db = _last_db.get(path)
    try:
        n = extras.process_combat_logs(game_dir, db, queue, saved_at=path.stat().st_mtime, uploader=uploader)
        if n:
            log.info("combat log: %d events queued (damage and healing per minute, kills)", n)
    except Exception as e:  # never let an extra stop the main sync
        log.warning("extras failed: %s", e)


def scan(cfg: Config, queue: Queue, seen: Dict[Path, Tuple[int, int]],
         uploader: Optional["Uploader"] = None) -> None:
    """Ingest every SavedVariables file that changed since the last scan."""
    for path in find_saved_variables(cfg.wow_path):
        try:
            st = path.stat()
            sig = (st.st_size, st.st_mtime_ns)
            if seen.get(path) == sig:
                if path in _last_db:
                    scan_extras(path, queue, uploader)
                continue
            parsed = read_stable(path)
            new_e, new_s, bad = ingest(parsed, queue)
            seen[path] = sig
            if uploader is not None:
                process_map_shots(path, parsed, queue, uploader)
            _last_db[path] = parsed.get(DB_GLOBAL)
            scan_extras(path, queue, uploader)
            log.info("read %s: %d new events, %d new snapshots, %d invalid",
                     path.name, new_e, new_s, bad)
        except Exception as e:  # keep the agent alive; try again next scan
            log.warning("scan failed for %s: %s", path, e)


def run_forever(cfg: Config, queue: Queue, uploader: Uploader, scan_every: float = 5) -> None:
    seen: Dict[Path, Tuple[int, int]] = {}
    failures, next_sync, next_scan = 0, 0.0, 0.0
    log.info("agent started; server %s", cfg.url)
    while True:
        scan_screenshots(queue, uploader)           # every second: a new screenshot goes up at once
        if time.monotonic() < next_scan:
            time.sleep(1)
            continue
        next_scan = time.monotonic() + scan_every
        scan(cfg, queue, seen, uploader)
        if time.monotonic() >= next_sync:
            res = uploader.sync_once()
            if res["error"]:
                failures += 1
                log.warning("sync failed (%s); will retry", res["error"])
            else:
                failures = 0
                if res["uploaded"] or res["snapshots"] or res["rejected"]:
                    log.info("synced: %s", res)
            delay = min(cfg.sync_interval * (2 ** min(failures, 5)), 600)
            next_sync = time.monotonic() + delay
        time.sleep(1)
