"""Things the agent reads from the game folder besides SavedVariables.

* Screenshots: every new image in <flavor>/Screenshots is resized and uploaded, so the
  server can show it next to what was happening at that moment.
* Combat log: when the player has switched the game's own combat log on (/combatlog), the
  text file it writes is read here and turned into damage and healing totals per minute.
  Addons may not read the combat log in this client, but the file is fair game.
  It also lists every kill by the group. Inside dungeons the addon is not told about kills,
  so the ones it did not record itself are taken from here.

Both keep their progress in the queue database, so nothing is sent twice.
"""
import bisect
import csv
import io
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("adventuresync")

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tga"}
AURA_EVENTS = ("SPELL_AURA_APPLIED", "SPELL_AURA_REMOVED", "SPELL_AURA_REFRESH", "SPELL_AURA_APPLIED_DOSE",
               "SPELL_AURA_REMOVED_DOSE", "SPELL_AURA_BROKEN", "SPELL_AURA_BROKEN_SPELL")
FULL_WIDTH, THUMB_WIDTH = 1920, 480


def _kv(queue):
    queue.conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    return queue.conn


def kv_get(queue, key: str, default: Any = None) -> Any:
    row = _kv(queue).execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def kv_set(queue, key: str, value: Any) -> None:
    _kv(queue).execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))
    queue.conn.commit()


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------ screenshots
def latest_character(db: Any) -> Optional[str]:
    """The character played most recently, by its newest snapshot."""
    chars = db.get("characters") if isinstance(db, dict) else None
    if not isinstance(chars, dict) or not chars:
        return None
    return str(max(chars, key=lambda c: (chars[c] or {}).get("captured_at") or 0))


def scaled_jpeg(path: Path, width: int, quality: int) -> Tuple[bytes, int, int]:
    from PIL import Image
    with Image.open(path) as img:
        img = img.convert("RGB")
        if img.width > width:
            img = img.resize((width, round(img.height * width / img.width)))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=quality)
        return out.getvalue(), img.width, img.height


# The AI screenshot key (addon) draws the position as 72 black and white squares along the
# bottom-left edge of the picture: CODE_CELL unscaled UI units each, the screen being 768 of
# those high. Read back here, so a picture carries the place of its own moment to the server.
CODE_CELL = 5
CODE_SYNC = [1, 0, 1, 1, 0, 0, 1, 0]


def read_code(path: Path) -> Optional[Dict[str, Any]]:
    """{map_id, x, y, facing} coded into an AI screenshot, or None for any other picture."""
    from PIL import Image
    with Image.open(path) as img:
        g = img.convert("L")
        w, h = g.size
        unit = h / 768
        bits = []
        for i in range(72):
            cx, cy = int((i + 0.5) * CODE_CELL * unit), int(h - 0.5 * CODE_CELL * unit)
            r = max(0, int(unit) - 1)
            px = [g.getpixel((min(w - 1, max(0, cx + dx)), min(h - 1, max(0, cy + dy))))
                  for dx in range(-r, r + 1) for dy in range(-r, r + 1)]
            bits.append(1 if sum(px) / len(px) >= 128 else 0)

    def num(a: int, n: int) -> int:
        return int("".join(map(str, bits[a:a + n])), 2)
    if bits[:8] != CODE_SYNC:
        return None
    m, x, y, f, check = num(8, 16), num(24, 17), num(41, 17), num(58, 9), num(67, 5)
    if (m + x + y + f) % 32 != check:
        return None
    place: Dict[str, Any] = {}
    if m:
        place["map_id"] = m
    if x <= 100000 and y <= 100000:
        place["x"], place["y"] = x / 1000, y / 1000
    if f < 360:
        place["facing"] = f
    return place or None


_sizes: Dict[str, int] = {}          # file -> its size when last looked at, to tell when the game is done writing it
_settled: Dict[str, int] = {}        # folder -> its modification time when everything in it had been dealt with
_retry_at: Dict[str, float] = {}     # folder -> when to try again after an upload that did not go through
RETRY_AFTER = 30.0                   # seconds: an unreachable server is not asked every second


def process_screenshots(game_dir: Path, db: Any, queue, uploader) -> int:
    """Upload screenshots not sent yet. Returns how many were uploaded.

    Only while the player has screenshot upload turned on (a setting kept on the server, see
    Uploader.settings), and only screenshots taken after this program first saw it on: the
    ones taken before, or while it was off, are noted as such and never sent.

    Meant to be called every second or so: a file is taken as soon as the game has finished
    writing it, which is when its size stays the same from one look to the next."""
    folder = game_dir / "Screenshots"
    character = latest_character(db)
    key = str(folder)
    try:
        stamp = folder.stat().st_mtime_ns
    except OSError:
        return 0
    # Cheap when nothing happened: a folder's time changes when a file is added to it, so an
    # unchanged one holds nothing new. One look at the folder, however many pictures are in it.
    if not character or _settled.get(key) == stamp or time.time() < _retry_at.get(key, 0):
        return 0
    done = kv_get(queue, "screenshots", {})
    new = [f for f in sorted(folder.iterdir()) if f.suffix.lower() in IMAGE_SUFFIXES and f.name not in done]
    if not new:
        _settled[key] = stamp
        return 0
    if uploader is None or not uploader.screenshots_on(character):
        for f in new:
            done[f.name] = "off"
        kv_set(queue, "screenshots", done)
        kv_set(queue, "screenshots_since", None)    # turned on again later: from that moment
        _settled[key] = stamp
        return 0
    since = kv_get(queue, "screenshots_since")
    if since is None:                               # just turned on: what is in the folder now stays here
        since = time.time()
        kv_set(queue, "screenshots_since", since)
    # Shots taken by /adventure mapshot are map backgrounds, not pictures for the album.
    map_times = []
    for e in (db.get("events") or []) if isinstance(db, dict) and isinstance(db.get("events"), list) else []:
        if isinstance(e, dict) and e.get("event_type") == "MAP_SCREENSHOT":
            try:
                map_times.append(float(e["payload"]["local_ts"]))
            except (KeyError, TypeError, ValueError):
                pass
    sent = 0
    for f in new:
        st = f.stat()
        if st.st_mtime < since - 2:
            done[f.name] = "off"
            continue
        age = time.time() - st.st_mtime
        last, _sizes[str(f)] = _sizes.get(str(f)), st.st_size
        if age < 3 and not (age >= 1 and last == st.st_size):     # the game may still be writing it
            continue
        if any(-5 <= st.st_mtime - t <= 30 for t in map_times):
            done[f.name] = "map"
            continue
        try:
            place = read_code(f)
        except Exception:
            place = None
        try:
            full, w, h = scaled_jpeg(f, FULL_WIDTH, 85)
            thumb, _, _ = scaled_jpeg(f, THUMB_WIDTH, 80)
        except Exception as e:
            if age < 60:                            # perhaps not finished after all: look again shortly
                continue
            done[f.name] = f"unreadable: {e}"
            log.warning("could not read screenshot %s: %s", f.name, e)
            continue
        _sizes.pop(str(f), None)
        shot_id = re.sub(r"[^A-Za-z0-9_-]", "_", f.stem)[:80]
        state, detail = uploader.upload_screenshot(shot_id, character, iso(st.st_mtime), w, h, full, thumb,
                                                   **({"place": place} if place else {}))
        if state == "ok":
            done[f.name] = "uploaded"
            sent += 1
            log.info("uploaded screenshot %s", f.name)
        elif state == "invalid":
            done[f.name] = f"rejected: {detail}"
        elif state == "skip":                       # no token for this character yet: try again later
            _retry_at[key] = time.time() + RETRY_AFTER
            break
        else:
            log.warning("screenshot upload will be retried: %s", detail)
            _retry_at[key] = time.time() + RETRY_AFTER
            break                                   # server unreachable: try the rest later too
    kv_set(queue, "screenshots", done)
    if all(f.name in done for f in new):           # nothing left waiting: rest until the folder changes
        _settled[key] = stamp
    return sent


# ------------------------------------------------------------- combat log
# Unit flag bits in the log: who a unit belongs to, and what kind of unit it is.
MINE, PARTY, RAID = 0x1, 0x2, 0x4
PLAYER, PET, GUARDIAN = 0x400, 0x1000, 0x2000
DAMAGE = {"SWING_DAMAGE": 9, "RANGE_DAMAGE": 12, "SPELL_DAMAGE": 12, "SPELL_PERIODIC_DAMAGE": 12,
          "DAMAGE_SHIELD": 12}
HEAL = {"SPELL_HEAL": 12, "SPELL_PERIODIC_HEAL": 12}
MISSED = {"SWING_MISSED": 9, "RANGE_MISSED": 12, "SPELL_MISSED": 12, "SPELL_PERIODIC_MISSED": 12,
          "DAMAGE_SHIELD_MISSED": 12}
KINDS = {"SWING": "swing", "RANGE": "range", "SPELL_PERIODIC": "dot", "SPELL": "spell", "DAMAGE_SHIELD": "shield"}
STAMP = re.compile(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\s+(\d{1,2}):(\d{2}):(\d{2})(\.\d+)?(?:[+-]\d+(?:\.\d+)?)?\s\s+(.*)$")
NPC = re.compile(r"^(?:Creature|Vehicle)-\d+-\d+-\d+-\d+-(\d+)-[0-9A-Fa-f]+$")
KEEP = 400                                          # pets and names remembered between scans
GUIDISH = re.compile(r"^(Player|Creature|Pet|Vehicle|GameObject|Item)-|^0{8,}$")


def parse_line(line: str, default_year: int) -> Optional[Tuple[float, List[str]]]:
    """-> (local epoch seconds, fields) for one combat log line, or None."""
    m = STAMP.match(line.rstrip("\r\n"))
    if not m:
        return None
    month, day, year, hh, mm, ss, frac, rest = m.groups()
    y = int(year) if year else default_year
    if y < 100:
        y += 2000
    try:
        epoch = time.mktime((y, int(month), int(day), int(hh), int(mm), int(ss), 0, 0, -1)) + float(frac or 0)
        fields = next(csv.reader([rest]))
    except (ValueError, OverflowError, StopIteration, csv.Error):
        return None
    return epoch, fields


def _is_float(v: str) -> bool:
    return bool(re.match(r"^-?\d+\.\d+$", v))


def amount_index(fields: List[str], start: int) -> Optional[int]:
    """Where the amount sits. With "advanced combat logging" on, a block describing the unit
    comes first; it ends with position x, position y, map id, facing, level."""
    if start >= len(fields):
        return None
    if not GUIDISH.match(fields[start]):
        return start
    for i in range(start + 8, min(start + 22, len(fields) - 5)):
        if _is_float(fields[i]) and _is_float(fields[i + 1]) and fields[i + 2].lstrip("-").isdigit() \
                and _is_float(fields[i + 3]) and fields[i + 4].lstrip("-").isdigit():
            return i + 5
    return None


def _num(v: str) -> Optional[float]:
    try:
        return float(v)
    except ValueError:
        return None


def _trim(table: Dict[str, Any]) -> Dict[str, Any]:
    return dict(list(table.items())[-KEEP:]) if len(table) > KEEP else table


class Meter:
    """Damage and healing by the player's group, summed per minute, and the group's kills."""

    def __init__(self, state: Optional[Dict[str, Any]] = None):
        state = state or {}
        self.me: Optional[str] = state.get("me")
        self.pets: Dict[str, str] = state.get("pets") or {}          # summoned unit -> counted under
        self.owners: Dict[str, str] = state.get("owners") or {}      # pet -> its owner's id
        self.names: Dict[str, str] = state.get("names") or {}        # group member's id -> name
        self.zone: Optional[str] = state.get("zone")
        # kills not sent yet: [time, unit id, npc id, name, killer ("" = the player), zone]
        self.kills: List[List[Any]] = state.get("kills") or []
        self.bucket: Optional[int] = state.get("bucket")
        self.damage: Dict[str, float] = state.get("damage") or {}
        self.healing: Dict[str, float] = state.get("healing") or {}
        self.taken: Dict[str, float] = state.get("taken") or {}      # damage the group's players took
        self.spells: Dict[str, Dict[str, float]] = state.get("spells") or {}   # damage by ability
        self.best: Dict[str, List[Any]] = state.get("best") or {}    # hardest hit: amount, ability, target, crit
        self.fight: float = state.get("fight") or 0                  # seconds of fighting this minute
        self.hits: Dict[str, List[int]] = state.get("hits") or {}    # hits landed, and how many were critical
        self.over: Dict[str, float] = state.get("over") or {}        # healing that went past full health
        self.hurt: Dict[str, float] = state.get("hurt") or {}        # "enemy｜ability" -> damage to the group
        self.cc: Dict[str, List[int]] = state.get("cc") or {}        # dispels, interrupts
        self.last: Optional[float] = state.get("last")               # when damage was last dealt or taken
        self.enc: Optional[Dict[str, Any]] = state.get("enc")        # the boss fight going on
        self.enc_done: List[List[Any]] = state.get("enc_done") or []
        # minutes already closed: "character:minute" -> how many parts. The game writes the file
        # in chunks, so a minute closed for quiet can go on in the next chunk (seen in the game:
        # 18:15 sent with 53 of its 157 damage, the rest dropped as the same minute again).
        self.closed: Dict[str, int] = state.get("closed") or {}
        # One row per hit, miss and heal of this pass, for the server's own statistics (not kept
        # between scans). pos is where the line being read starts in the file.
        self.rows: Optional[List[List[Any]]] = None
        self.cuts: List[Tuple[int, str]] = []                        # (row index, character) where whose log it is changed
        self.pos = 0
        self.starts: Dict[Any, float] = {}                           # casts under way: (caster, ability) -> began
        # finished minutes not sent yet (kept while it is still unknown whose log this is)
        self.done: List[Tuple[int, Dict[str, float], Dict[str, float]]] = [tuple(x) for x in state.get("done") or []]

    def state(self) -> Dict[str, Any]:
        return {"me": self.me, "pets": _trim(self.pets), "owners": _trim(self.owners),
                "names": _trim(self.names), "zone": self.zone, "kills": self.kills,
                "bucket": self.bucket, "damage": self.damage, "healing": self.healing,
                "taken": self.taken, "spells": self.spells, "best": self.best, "fight": self.fight,
                "hits": self.hits, "over": self.over, "hurt": self.hurt, "cc": self.cc,
                "last": self.last, "enc": self.enc, "enc_done": self.enc_done, "closed": self.closed}

    def named(self, who: str) -> str:
        """The name for a pet's owner that was not known yet when the pet acted."""
        if not who.startswith("@"):
            return who
        return "" if who[1:] == self.me else self.names.get(who[1:]) or "隊友的寵物"

    def _named(self, table: Dict[str, float]) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for who, amount in table.items():
            out[self.named(who)] = out.get(self.named(who), 0) + amount
        return out

    def close(self) -> None:
        if self.bucket is not None and (self.damage or self.healing or self.taken or self.cc):
            spells: Dict[str, Dict[str, float]] = {}
            for who, table in self.spells.items():
                mine = spells.setdefault(self.named(who), {})
                for spell, amount in table.items():
                    mine[spell] = mine.get(spell, 0) + amount
            best: Dict[str, List[Any]] = {}
            for who, hit in self.best.items():
                if hit[0] > best.get(self.named(who), [0])[0]:
                    best[self.named(who)] = hit
            pairs = lambda table: {n: [sum(v[0] for k, v in table.items() if self.named(k) == n),   # noqa: E731
                                       sum(v[1] for k, v in table.items() if self.named(k) == n)]
                                   for n in {self.named(k) for k in table}}
            more = {"taken": self._named(self.taken), "spells": spells, "best": best, "fight": round(self.fight),
                    "hits": pairs(self.hits), "over": self._named(self.over), "hurt": self.hurt,
                    "cc": pairs(self.cc), "me": self.me}
            key = f"{self.me}:{self.bucket}"
            more["part"] = self.closed[key] = self.closed.get(key, 0) + 1
            if len(self.closed) > 120:              # an hour or two back is plenty
                for k in sorted(self.closed, key=lambda k: int(k.rsplit(":", 1)[1]))[:-120]:
                    del self.closed[k]
            self.done.append((self.bucket, self._named(self.damage), self._named(self.healing), more))
        self.bucket, self.damage, self.healing = None, {}, {}
        self.taken, self.spells, self.best, self.fight = {}, {}, {}, 0
        self.hits, self.over, self.hurt, self.cc = {}, {}, {}, {}

    def _row(self, epoch: float, f: List[str], way: str, who: str, pet: bool, enemy: int, start: int,
             idx: Optional[int], miss: Optional[str] = None, healed: Optional[float] = None) -> None:
        """Keep one hit, miss or heal as a row. enemy is where the other unit's id sits in the line."""
        if self.rows is None:
            return
        event = f[0]
        kind = next(v for k, v in KINDS.items() if event.startswith(k)) if way != "heal" else \
            ("hot" if "PERIODIC" in event else "heal")
        spell_id = int(f[9]) if start == 12 and f[9].isdigit() else 0
        spell = f[10] if start == 12 else "近戰"
        npc = NPC.match(f[enemy])
        school = f[11] if start == 12 else "0x1"
        row: List[Any] = [self.pos, round(epoch * 1000), way, who, int(pet), f[2] if way != "in" else f[6],
                          f[enemy + 1] if f[enemy + 1] != "nil" else None, int(npc.group(1)) if npc else None,
                          spell_id, spell, int(school, 16) if school.startswith("0x") else None, kind]
        g = lambda i: (_num(f[i]) if i < len(f) else None)                     # noqa: E731
        flag = lambda i: int(i < len(f) and f[i] == "1")                       # noqa: E731
        if miss is not None:
            row += [0, None, None, None, None, None, 0, 0, 0, miss]
        elif way == "heal":
            row += [healed, g(idx), None, None, None, None, flag(idx + 4 if len(f) - idx >= 5 else idx + 3), 0, 0, None]
        else:
            based = len(f) - idx >= 10               # newer logs give the amount before mitigation too
            o = idx + (1 if based else 0)
            row += [g(idx), g(idx + 1) if based else None, g(o + 1), g(o + 3), g(o + 4), g(o + 5),
                    flag(o + 6), flag(o + 7), flag(o + 8), None]
        # the unit a line describes: its health, armour and level when it is the target, its
        # attack power when it is the attacker
        unit: List[Any] = [None] * 5
        if idx is not None and miss is None and idx - start >= 17 and GUIDISH.match(f[start]):
            hp, top, ap, armor, level = (_num(f[start + 2]), _num(f[start + 3]), _num(f[start + 4]),
                                         _num(f[start + 6]), _num(f[idx - 1]))
            if f[start] == f[5]:
                unit[:4] = [hp, top, armor, level]
            elif f[start] == f[1]:
                unit[4] = ap
        self.rows.append(row + unit + [f[enemy] if GUIDISH.match(f[enemy]) else None])

    def _cast(self, epoch: float, f: List[str], who: str, pet: bool) -> None:
        """A finished cast as a row: what it was aimed at and what it cost."""
        if self.rows is None or len(f) < 26 or not GUIDISH.match(f[12]):
            return
        began = self.starts.pop((f[1], f[9]), None)
        took = round((epoch - began) * 1000) if began is not None and 0 < epoch - began <= 15 else None    # cast time, ms
        npc = NPC.match(f[5])
        self.rows.append([self.pos, round(epoch * 1000), "cast", who, int(pet), f[2], f[6] if f[6] != "nil" else None,
                          int(npc.group(1)) if npc else None, int(f[9]) if f[9].isdigit() else 0, f[10],
                          int(f[11], 16) if f[11].startswith("0x") else None, f[22],     # kind of power spent
                          _num(f[25]), took, *[None] * 13, f[5] if npc else None])

    def _note(self, epoch: float, f: List[str], way: str, who: str, pet: bool, kind: Optional[str] = None,
              other: Optional[str] = None, stacks: Optional[int] = None) -> None:
        """An aura gained or lost (APPLIED, REFRESH, REMOVED, or APPLIED_DOSE / REMOVED_DOSE with
        the stacks it has now in `amount`), or a death, as a row."""
        if self.rows is None:
            return
        aura = way == "aura"
        npc = NPC.match(f[5])
        self.rows.append([self.pos, round(epoch * 1000), way, who, int(pet), f[2] if aura and f[2] != "nil" else f[6],
                          other, int(npc.group(1)) if npc else None,
                          int(f[9]) if aura and f[9].isdigit() else None, f[10] if aura else None, None, kind,
                          stacks, *[None] * 8, f[12] if aura and len(f) > 12 else None, *[None] * 5, f[5]])

    def _became(self, guid: str) -> None:
        """The log is someone else's from here on. The game keeps writing the same file when
        the player logs out and comes back as another character (seen in the game: a second
        character's play was filed under the first), so what was gathered so far is closed
        as the earlier character's, and the rows from here on are marked as the new one's."""
        if self.me is not None:
            self.close()
            self.enc = None
            self.last = None
        self.me = guid
        if self.rows is not None:
            self.cuts.append((len(self.rows), guid))

    def _roll(self, epoch: float) -> None:
        """Start a new minute when the clock has moved on."""
        minute = int(epoch // 60)
        if self.bucket is not None and minute != self.bucket:
            self.close()
        self.bucket = minute

    def _who(self, guid: str, name: str, flags: int) -> Optional[str]:
        """The name a source is counted under ("" = the player), or None when not ours."""
        if not flags & (MINE | PARTY | RAID):
            return None
        if flags & PLAYER:
            if flags & MINE:
                if guid != self.me and guid.startswith("Player-"):
                    self._became(guid)
                return ""
            self.names[guid] = name.split("-")[0]
            return self.names[guid]
        if flags & (PET | GUARDIAN):
            if flags & MINE:
                return ""
            owner = self.owners.get(guid)
            if owner and owner == self.me:
                return ""
            if owner:
                return self.names.get(owner) or "@" + owner   # named later, once the owner acts
            if guid in self.pets:
                return self.pets[guid]
            return f"{name}（寵物）"
        return None

    def feed(self, epoch: float, f: List[str]) -> None:
        event = f[0]
        if event == "ZONE_CHANGE" and len(f) >= 3:
            self.zone = f[2]
            return
        if event == "ENCOUNTER_START" and len(f) >= 3:
            self.enc = {"name": f[2], "start": epoch, "damage": {}, "healing": {}, "taken": {}}
            return
        if event == "ENCOUNTER_END" and len(f) >= 6:
            if self.enc and self.enc["name"] == f[2]:
                e = self.enc
                self.enc_done.append([e["start"], epoch, e["name"], f[5] == "1", round((_num(f[6]) or 0) / 1000)
                                      if len(f) > 6 else round(epoch - e["start"]),
                                      self._named(e["damage"]), self._named(e["healing"]), self._named(e["taken"]), self.me])
            self.enc = None
            return
        if len(f) < 9:
            return
        try:
            flags = int(f[3], 16)
        except ValueError:
            return
        # Lines that describe a unit name its owner right after it: that is how a pet which
        # was already out when logging started is known to be someone's.
        for i in range(9, min(len(f) - 1, 14)):
            if f[i + 1].startswith("Player-") and f[i].startswith(("Pet-", "Creature-")):
                self.owners[f[i]] = f[i + 1]
                break
        if event == "PARTY_KILL":
            who = self._who(f[1], f[2], flags)
            npc = NPC.match(f[5])
            if who is not None and npc:
                self.kills.append([epoch, f[5], int(npc.group(1)), f[6] if f[6] != "nil" else None, who, self.zone, self.me])
            return
        if event == "SPELL_SUMMON":                # remember whose pet or totem this is
            owner = self._who(f[1], f[2], flags)
            if owner is not None:
                self.pets[f[5]] = owner
            return
        if event in ("SPELL_DISPEL", "SPELL_INTERRUPT", "SPELL_STOLEN"):
            who = self._who(f[1], f[2], flags)
            if who is not None:
                self._roll(epoch)
                self.cc.setdefault(who, [0, 0])[1 if event == "SPELL_INTERRUPT" else 0] += 1
            return
        if event == "SPELL_CAST_START" and len(f) > 9:
            if self.rows is not None and flags & (MINE | PARTY | RAID):
                self.starts[(f[1], f[9])] = epoch
                if len(self.starts) > 200:
                    self.starts.pop(next(iter(self.starts)))
            return
        if event in AURA_EVENTS and self.rows is not None and len(f) > 12:
            try:
                dest = int(f[7], 16)
            except ValueError:
                dest = 0
            kind, stacks = event[11:], None
            if kind in ("BROKEN", "BROKEN_SPELL"):          # broken by damage or a spell: it ends there
                if kind == "BROKEN_SPELL":                  # ..., the breaking spell's id, name, school, BUFF|DEBUFF
                    f = f[:12] + f[-1:]
                kind = "REMOVED"
            elif kind.endswith("_DOSE"):                    # ..., BUFF|DEBUFF, how many stacks now
                stacks = int(f[13]) if len(f) > 13 and f[13].isdigit() else None
                f = f[:13]
            if dest & PLAYER and dest & (MINE | PARTY | RAID):         # something on a member: filed under them
                self._note(epoch, f, "aura", self._who(f[5], f[6], dest) or "", False, kind, stacks=stacks)
            elif not dest & (MINE | PARTY | RAID):                     # something a member put on someone else
                who = self._who(f[1], f[2], flags)
                if who is not None:
                    self._note(epoch, f, "aura", who, bool(flags & (PET | GUARDIAN)), kind, f[6] if f[6] != "nil" else None,
                               stacks=stacks)
            return
        if event == "UNIT_DIED" and self.rows is not None:
            try:
                dest = int(f[7], 16)
            except ValueError:
                dest = 0
            if dest & (MINE | PARTY | RAID) and dest & (PLAYER | PET):
                owner = self._who(f[5], f[6], dest)
                if owner is not None:
                    self._note(epoch, f, "death", owner, not dest & PLAYER)
            return
        if event == "SPELL_CAST_SUCCESS":
            who = self._who(f[1], f[2], flags)
            if who is not None:
                self._cast(epoch, f, who, bool(flags & (PET | GUARDIAN)))
            return
        if event in MISSED and self.rows is not None:
            who, at = self._who(f[1], f[2], flags), MISSED[event]
            try:
                dest = int(f[7], 16)
            except ValueError:
                dest = 0
            if at < len(f):
                if who is not None:
                    self._row(epoch, f, "out", who, bool(flags & (PET | GUARDIAN)), 5, at, None, miss=f[at])
                elif dest & (PLAYER | PET | GUARDIAN) and dest & (MINE | PARTY | RAID):
                    self._row(epoch, f, "in", self._who(f[5], f[6], dest) or "", not dest & PLAYER, 1, at, None, miss=f[at])
            return
        start = DAMAGE.get(event) or HEAL.get(event)
        if start is None:
            return
        who = self._who(f[1], f[2], flags)
        victim = None
        if event in DAMAGE:
            try:
                dest = int(f[7], 16)
            except ValueError:
                dest = 0
            if dest & PLAYER and dest & (MINE | PARTY | RAID):
                victim = self._who(f[5], f[6], dest)
            elif who is None and dest & (PET | GUARDIAN) and dest & (MINE | PARTY | RAID) and self.rows is not None:
                # a hit on one of the group's pets: kept as a row, but not counted as damage the players took
                idx = amount_index(f, start)
                owner = self._who(f[5], f[6], dest)
                if idx is not None and owner is not None and (_num(f[idx]) or 0) > 0:
                    self._row(epoch, f, "in", owner, True, 1, start, idx)
                return
        if who is None and victim is None:
            return
        idx = amount_index(f, start)
        amount = _num(f[idx]) if idx is not None and idx < len(f) else None
        if amount is None or amount <= 0:
            return
        if event in HEAL and who is None:
            return
        if event in HEAL:
            # healing that landed: the overheal follows, after a "base amount" in newer logs
            over = _num(f[idx + 2]) if len(f) - idx >= 5 else _num(f[idx + 1]) if idx + 1 < len(f) else 0
            over = max(0, min(over or 0, amount))
            amount -= over
            if who is not None:
                self._row(epoch, f, "heal", who, bool(flags & (PET | GUARDIAN)), 5, start, idx, healed=amount)
            if amount <= 0:
                self._roll(epoch)
                self.over[who] = self.over.get(who, 0) + over
                return
        self._roll(epoch)
        enc = self.enc
        if event in HEAL:
            if over:
                self.over[who] = self.over.get(who, 0) + over
            self.healing[who] = self.healing.get(who, 0) + amount
            if enc:
                enc["healing"][who] = enc["healing"].get(who, 0) + amount
            return
        # fighting time: seconds between hits, as long as they are no more than five apart
        gap = epoch - self.last if self.last is not None else None
        self.fight += gap if gap is not None and 0 <= gap <= 5 else 1
        self.last = epoch
        spell = ("寵物近戰" if flags & (PET | GUARDIAN) else "近戰") if event == "SWING_DAMAGE" else f[10]
        if who is not None:
            self._row(epoch, f, "out", who, bool(flags & (PET | GUARDIAN)), 5, start, idx)
        elif victim is not None:
            self._row(epoch, f, "in", victim, False, 1, start, idx)
        if victim is not None:
            if who is None:                         # what hurt the group, by enemy and ability
                key = f"{f[2]}｜{spell if event != 'SWING_DAMAGE' else '近戰'}"
                self.hurt[key] = self.hurt.get(key, 0) + amount
            self.taken[victim] = self.taken.get(victim, 0) + amount
            if enc:
                enc["taken"][victim] = enc["taken"].get(victim, 0) + amount
        if who is None:
            return
        self.damage[who] = self.damage.get(who, 0) + amount
        if enc:
            enc["damage"][who] = enc["damage"].get(who, 0) + amount
        mine = self.spells.setdefault(who, {})
        mine[spell] = mine.get(spell, 0) + amount
        at = idx + 7 if len(f) - idx >= 10 else idx + 6
        crit = at < len(f) and f[at] == "1"
        if event != "DAMAGE_SHIELD" and event != "SPELL_PERIODIC_DAMAGE":      # those never crit
            count = self.hits.setdefault(who, [0, 0])
            count[0] += 1
            count[1] += int(crit)
        if amount > self.best.get(who, [0])[0]:
            self.best[who] = [amount, spell, f[6], crit]


def recorded_kills(queue) -> Dict[int, List[float]]:
    """Kills the addon reported itself: npc id -> times. The game shows addons scrambled unit
    ids, so a kill in the log is matched to the addon's by kind of monster and time."""
    rows = queue.conn.execute("SELECT payload_json FROM pending_events WHERE payload_json LIKE '%MOB_KILLED%' "
                              "AND event_id NOT LIKE 'combatlog:%'")
    out: Dict[int, List[float]] = {}
    for (text,) in rows:
        try:
            e = json.loads(text)
            at = datetime.strptime(e["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
            out.setdefault(int(e["payload"]["monster_id"]), []).append(at)
        except (ValueError, KeyError, TypeError):
            pass
    return out


SEEN = "combatseen"         # kv: times (5-second steps) the player's own doings were read from the log
# kv: the newest moment any line read from the log speaks of. The game keeps what it logs in
# memory and writes it to the file in chunks: seen in the game, a fight playing alone reached
# the file four minutes later. Until the file has got past a fight, nothing in it says anything
# about that fight.
READ = "combatread"
SETTLE = 900                # a fight this old with nothing in the file: the log really was off


def note_seen(queue, rows: List[List[Any]], keep: float = 7 * 86400) -> None:
    """Remember when the log showed the player doing something (or having it done to them)."""
    mine = {int(r[1] // 5000) * 5 for r in rows if len(r) > 3 and r[3] == "" and isinstance(r[1], (int, float))}
    if not mine:
        return
    seen = set(kv_get(queue, SEEN, [])) | mine
    newest = max(seen)
    kv_set(queue, SEEN, sorted(t for t in seen if t >= newest - keep))


def combat_log_check(queue, now: Optional[float] = None, live: float = 900, recent: float = 12 * 3600,
                     settle: float = SETTLE) -> Dict[str, Any]:
    """Is the game's combat log giving us the player's fights?

    Checked against the fights the addon itself recorded (it does that with the log on or
    off): a fight with nothing from the log in its time means the log was off -- once the file
    has got past that fight, or the fight is `settle` seconds old (the game writes the file
    late, see READ). Between saves the addon has not said anything yet, so something of the
    player's read from the log in the last minutes counts as proof too. State "ok", "missing",
    "checking" (the newest fight is not in the file yet, which says nothing either way), or
    "waiting" (no fight to judge by in the last hours)."""
    now = time.time() if now is None else now
    # Fights from before this program started keeping track have nothing to be checked against.
    since = kv_get(queue, SEEN + "-since")
    if since is None:
        since = now
        kv_set(queue, SEEN + "-since", since)
    seen = kv_get(queue, SEEN, [])
    fights = []
    for (text,) in queue.conn.execute("SELECT payload_json FROM pending_events WHERE payload_json LIKE '%COMBAT%' "
                                      "ORDER BY created_at DESC LIMIT 500"):
        try:
            e = json.loads(text)
            if e.get("event_type") != "COMBAT":
                continue
            end = datetime.strptime(e["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
            if now - end <= recent and end >= since:
                fights.append((end, float((e.get("payload") or {}).get("duration") or 0)))
        except (ValueError, KeyError, TypeError):
            pass
    fights.sort()

    def covered(end: float, seconds: float) -> bool:
        k = bisect.bisect_left(seen, end - seconds - 10)
        return k < len(seen) and seen[k] <= end + 10

    read = kv_get(queue, READ)

    def settled(end: float) -> bool:
        return (read is not None and read >= end + 30) or now - end >= settle

    missed = [end for end, seconds in fights if not covered(end, seconds) and settled(end)]
    out = {"last_seen": seen[-1] if seen else None, "fights": len(fights), "missed": len(missed),
           "newest_fight": fights[-1][0] if fights else None}
    if seen and now - seen[-1] <= live:
        return {**out, "state": "ok"}
    if fights:
        if covered(*fights[-1]):
            return {**out, "state": "ok"}
        return {**out, "state": "missing" if settled(fights[-1][0]) else "checking"}
    return {**out, "state": "waiting"}


def process_combat_logs(game_dir: Path, db: Any, queue, idle_after: float = 120,
                        saved_at: Optional[float] = None, uploader=None) -> int:
    """Read what was appended to the game's combat log files. Returns events queued.

    saved_at is when the addon last saved. A kill is taken from the log only once the addon
    has saved after it and still did not report it, so no kill is counted twice."""
    folder = game_dir / "Logs"
    if not folder.is_dir():
        return 0
    fallback = latest_character(db)
    queued = 0
    for f in sorted(folder.glob("WoWCombatLog*.txt")):
        key = f"combatlog:{f.name}"
        st = f.stat()
        state = kv_get(queue, key, {})
        offset = state.get("offset", 0)
        if st.st_size < offset:                     # the file was replaced: start over
            offset, state = 0, {}
        meter = Meter(state.get("meter"))
        if uploader is not None:
            meter.rows = []
        before = meter.me                           # whose log it was when this pass began
        if st.st_size > offset:
            with open(f, "rb") as fh:
                fh.seek(offset)
                chunk = fh.read()
            end = chunk.rfind(b"\n") + 1             # leave a half-written last line for next time
            year = time.localtime(st.st_mtime).tm_year
            at = offset
            newest = None
            for raw in chunk[:end].split(b"\n"):
                parsed = parse_line(raw.decode("utf-8", errors="replace"), year)
                if parsed:
                    meter.pos = at
                    meter.feed(*parsed)
                    newest = parsed[0]
                at += len(raw) + 1
            offset += end
            if newest is not None and newest > (kv_get(queue, READ) or 0):
                kv_set(queue, READ, newest)
        if time.time() - st.st_mtime >= idle_after:  # nothing new for a while: the minute is over
            meter.close()
        guess = fallback if fallback and fallback.startswith("Player-") else None
        character = meter.me or guess
        if meter.rows:
            note_seen(queue, meter.rows)
            # Every hit goes to the server as it is, each stretch under the character whose log
            # it was. Until all of that has worked, nothing about this part of the file is
            # remembered, so it is read again on the next scan.
            for row in meter.rows:
                row[3] = meter.named(row[3])
            marks = [(0, before or (meter.cuts[0][1] if meter.cuts else None) or guess)]
            for at, owner in meter.cuts:            # the first owner becoming known is no change of owner
                if owner != marks[-1][1]:
                    marks.append((at, owner))
            stretches = [(owner, meter.rows[at:(marks[k + 1][0] if k + 1 < len(marks) else len(meter.rows))])
                         for k, (at, owner) in enumerate(marks)]
            again = False
            for owner, part in stretches:
                if not part:
                    continue
                if not owner:
                    again = True
                    break
                state_, detail = uploader.upload_hits(owner, f.name, part)
                if state_ == "skip":                # no token for this character yet: read it again later
                    again = True
                    break
                if state_ == "retry":
                    log.warning("combat log rows will be sent again later: %s", detail)
                    again = True
                    break
                if state_ != "ok":
                    log.error("the server refused combat log rows from %s: %s", f.name, detail)
            if again:
                continue
        events = []
        if character:
            whole = lambda table: {k: round(v) for k, v in table.items()}     # noqa: E731
            for minute, damage, healing, *rest in meter.done:
                more = rest[0] if rest else {}
                payload = {"source": "combatlog", "damage": whole(damage), "healing": whole(healing)}
                if more.get("taken"):
                    payload["taken"] = whole(more["taken"])
                if more.get("spells"):
                    payload["spells"] = {who: whole(t) for who, t in more["spells"].items()}
                if more.get("best"):
                    payload["best"] = {who: [round(h[0]), *h[1:]] for who, h in more["best"].items()}
                if more.get("fight"):
                    payload["fight"] = more["fight"]
                for part in ("hits", "cc"):
                    if more.get(part):
                        payload[part] = more[part]
                for part in ("over", "hurt"):
                    if more.get(part):
                        payload[part] = whole(more[part])
                owner = more.get("me") or character             # the minute is the character's who played it
                part = more.get("part") or 1                    # the rest of a minute the game wrote late
                events.append({
                    "event_id": f"combatlog:{owner}:{minute}" + (f":{part}" if part > 1 else ""), "character_id": owner,
                    "event_type": "GROUP_METER", "timestamp": iso(minute * 60 + 59), "payload": payload})
            meter.done = []
            for start, end, name, success, seconds, damage, healing, taken, *rest in meter.enc_done:
                owner = (rest[0] if rest else None) or character
                events.append({
                    "event_id": f"combatlog:enc:{owner}:{int(start)}", "character_id": owner,
                    "event_type": "GROUP_ENCOUNTER", "timestamp": iso(end),
                    "payload": {"source": "combatlog", "name": name, "success": success, "duration": seconds,
                                "damage": whole(damage), "healing": whole(healing), "taken": whole(taken)}})
            meter.enc_done = []
            due = [k for k in meter.kills if saved_at is not None and k[0] <= saved_at]
            if due:
                known = recorded_kills(queue)
                for at, guid, npc, name, who, zone, *rest in due:
                    owner = (rest[0] if rest else None) or character
                    same = [t for t in known.get(npc, []) if abs(t - at) <= 5]
                    if same:                        # the addon has this one: each of its kills matches once
                        known[npc].remove(min(same, key=lambda t: abs(t - at)))
                        continue
                    payload = {"source": "combatlog", "monster_id": npc, "monster_name": name,
                               "monster_guid": guid, "zone_name": zone, "killed_by": meter.named(who) or None}
                    events.append({"event_id": f"combatlog:kill:{guid}", "character_id": owner,
                                   "event_type": "MOB_KILLED", "timestamp": iso(at),
                                   "payload": {k: v for k, v in payload.items() if v is not None}})
                meter.kills = [k for k in meter.kills if k not in due]
        queued += queue.add_events(events) if events else 0
        kv_set(queue, key, {"offset": offset, "meter": {**meter.state(), "done": meter.done}})
    return queued
