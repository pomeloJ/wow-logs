"""Read-only world database API (quests, NPCs, spawns, items, maps).

The data lives in its own SQLite file built by `python -m world_importer`; this module
only reads it. Coordinates are percentages of a uiMapID map, the same frame the addon
records, so world data and recorded adventures can be compared directly.
"""
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException, Query

RANKS = {0: "normal", 1: "elite", 2: "rare_elite", 3: "boss", 4: "rare"}
ITEM_CLASS = {0: "消耗品", 1: "容器", 2: "武器", 3: "寶石", 4: "護甲", 5: "材料", 6: "彈藥", 7: "商品材料", 9: "配方",
              11: "箭袋", 12: "任務物品", 13: "鑰匙", 15: "雜項"}
ARMOR = {1: "布甲", 2: "皮甲", 3: "鎖甲", 4: "鎧甲", 6: "盾牌"}
WEAPON = {0: "單手斧", 1: "雙手斧", 2: "弓", 3: "槍械", 4: "單手錘", 5: "雙手錘", 6: "長柄武器", 7: "單手劍",
          8: "雙手劍", 10: "法杖", 13: "拳套", 15: "匕首", 16: "投擲武器", 18: "弩", 19: "魔杖", 20: "魚竿"}


# Bits of the quest `races` / `classes` masks. Forever's new races use bits above 2^32.
RACE_BIT = {1: 1, 2: 2, 3: 4, 4: 8, 5: 16, 6: 32, 7: 64, 8: 128, 95: 4294967296, 96: 8589934592}
RACE_ID = {"Human": 1, "Orc": 2, "Dwarf": 3, "NightElf": 4, "Scourge": 5, "Tauren": 6, "Gnome": 7, "Troll": 8}
CLASS_BIT = {"WARRIOR": 1, "PALADIN": 2, "HUNTER": 4, "ROGUE": 8, "PRIEST": 16, "SHAMAN": 64, "MAGE": 128,
             "WARLOCK": 256, "DRUID": 1024}


def xp_share(player_level: Optional[int], quest_level: Optional[int]) -> float:
    """Share of a quest's XP a character gets: less once the quest is 6+ levels below them."""
    if not player_level or not quest_level or quest_level <= 0:
        return 1.0
    gap = player_level - quest_level
    return 0.5 if gap >= 8 else 0.6 if gap == 7 else 0.8 if gap == 6 else 1.0


def item_kind(cls: Optional[int], sub: Optional[int]) -> Optional[str]:
    if cls == 4:
        return ARMOR.get(sub or 0, "護甲")
    if cls == 2:
        return WEAPON.get(sub or 0, "武器")
    return ITEM_CLASS.get(cls) if cls is not None else None


class World:
    def __init__(self, path: str):
        self.path = path
        self.zone_quests = None      # set by register(): (character_id, map_id) -> zone quest list

    def available(self) -> bool:
        return os.path.isfile(self.path)

    @contextmanager
    def conn(self):
        if not self.available():
            raise HTTPException(503, "world database has not been imported")
        c = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            yield c
        finally:
            c.close()

    # Helpers used by other modules; they return nothing when no world data is installed.
    def item_names(self, ids: List[int]) -> Dict[int, str]:
        if not ids or not self.available():
            return {}
        with self.conn() as c:
            rows = c.execute("SELECT item_id, COALESCE(name_zh, name) n FROM world_items WHERE item_id IN (%s)"
                             % ",".join("?" * len(ids)), ids).fetchall()
        return {r["item_id"]: r["n"] for r in rows if r["n"]}

    def npc_info(self, npc_id: int) -> Optional[Dict[str, Any]]:
        """Level range and rank of an NPC, or None when unknown."""
        if not self.available():
            return None
        with self.conn() as c:
            r = c.execute("SELECT min_level, max_level, rank FROM world_npcs WHERE npc_id=?", (npc_id,)).fetchone()
        if not r:
            return None
        lv = None if not r["min_level"] else str(r["min_level"]) if r["min_level"] == r["max_level"] \
            else f"{r['min_level']}–{r['max_level']}"
        return {"level": lv, "rank": RANKS.get(r["rank"], "normal")}

    def turn_in(self, quest_ids: List[int]) -> Dict[int, str]:
        """{quest id: 'NPC name（zone）'} for where each quest is handed in."""
        if not quest_ids or not self.available():
            return {}
        out: Dict[int, str] = {}
        with self.conn() as c:
            for qid in quest_ids:
                row = c.execute("SELECT data FROM world_quests WHERE quest_id=?", (qid,)).fetchone()
                fin = (json.loads(row["data"]).get("finishers") or {}) if row and row["data"] else {}
                for kind, ids in (("npc", fin.get("npcs")), ("object", fin.get("objects"))):
                    if ids:
                        e = _entity(c, kind, ids[0], spawn_limit=1)
                        if e and e["name"]:
                            zone = e["spawns"][0]["zone"] if e["spawns"] else None
                            out[qid] = e["name"] + (f"（{zone}）" if zone else "")
                            break
        return out


def _name(row: sqlite3.Row) -> Optional[str]:
    return row["name_zh"] or row["name"]


def _zone(c, area_id: Optional[int]) -> Optional[str]:
    if area_id is None:
        return None
    r = c.execute("SELECT name, name_zh FROM world_maps WHERE area_id=?", (area_id,)).fetchone()
    return _name(r) if r else None


def _spawns(c, kind: str, eid: int, limit: int) -> List[Dict[str, Any]]:
    rows = c.execute("SELECT area_id, ui_map_id, x, y, respawn FROM world_spawns "
                     "WHERE kind=? AND entity_id=? ORDER BY id LIMIT ?", (kind, eid, limit)).fetchall()
    zones: Dict[int, Optional[str]] = {}
    out = []
    for r in rows:
        if r["area_id"] not in zones:
            zones[r["area_id"]] = _zone(c, r["area_id"])
        out.append({"map_id": r["ui_map_id"], "area_id": r["area_id"], "zone": zones[r["area_id"]],
                    "x": r["x"], "y": r["y"], "respawn": r["respawn"]})
    return out


def _entity(c, kind: str, eid: int, spawn_limit: int = 200) -> Optional[Dict[str, Any]]:
    if kind == "npc":
        r = c.execute("SELECT * FROM world_npcs WHERE npc_id=?", (eid,)).fetchone()
    else:
        r = c.execute("SELECT * FROM world_objects WHERE object_id=?", (eid,)).fetchone()
    if not r:
        return None
    out = {"kind": kind, "id": eid, "name": _name(r), "name_en": r["name"],
           "quest_starts": json.loads(r["quest_starts"] or "[]"),
           "quest_ends": json.loads(r["quest_ends"] or "[]"),
           "spawn_count": c.execute("SELECT COUNT(*) FROM world_spawns WHERE kind=? AND entity_id=?",
                                    (kind, eid)).fetchone()[0],
           "spawns": _spawns(c, kind, eid, spawn_limit)}
    if kind == "npc":
        out.update({"title": r["sub_name_zh"] or r["sub_name"], "min_level": r["min_level"],
                    "max_level": r["max_level"], "rank": RANKS.get(r["rank"], "normal"),
                    "reaction": r["reaction"], "respawn": r["respawn"]})
    return out


def _brief(c, kind: str, eid: int) -> Dict[str, Any]:
    e = _entity(c, kind, eid, spawn_limit=1)
    if not e:
        return {"kind": kind, "id": eid, "name": None}
    return {"kind": kind, "id": eid, "name": e["name"], "location": e["spawns"][0] if e["spawns"] else None}


def _zones(c, kind: str, eid: int) -> List[Dict[str, Any]]:
    """Maps an NPC or object appears on, with how many spawn points each, busiest first."""
    out = []
    for r in c.execute("SELECT ui_map_id, MIN(area_id) a, COUNT(*) n FROM world_spawns WHERE kind=? AND "
                       "entity_id=? AND ui_map_id IS NOT NULL GROUP BY ui_map_id ORDER BY n DESC", (kind, eid)):
        top = c.execute("SELECT name, name_zh FROM world_maps WHERE ui_map_id=? AND parent_area_id IS NULL "
                        "ORDER BY area_id LIMIT 1", (r["ui_map_id"],)).fetchone()
        out.append({"map_id": r["ui_map_id"], "zone": _name(top) if top else _zone(c, r["a"]), "spawns": r["n"]})
    return out


def _source(c, kind: str, eid: int) -> Dict[str, Any]:
    """An NPC or object as a place to get something: name, level, and where it is found."""
    out = _brief(c, kind, eid)
    out["zones"] = _zones(c, kind, eid)
    if kind == "npc":
        r = c.execute("SELECT min_level, max_level, rank FROM world_npcs WHERE npc_id=?", (eid,)).fetchone()
        if r:
            out.update(min_level=r["min_level"], max_level=r["max_level"], rank=RANKS.get(r["rank"], "normal"))
    return out


def _item_brief(c, iid: int) -> Dict[str, Any]:
    r = c.execute("SELECT name, name_zh FROM world_items WHERE item_id=?", (iid,)).fetchone()
    return {"id": iid, "name": _name(r) if r else None}


def _quest_brief(c, qid: int) -> Dict[str, Any]:
    r = c.execute("SELECT name, name_zh, level FROM world_quests WHERE quest_id=?", (qid,)).fetchone()
    return {"id": qid, "name": _name(r) if r else None, "level": r["level"] if r else None}


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def register(app, world: World, db, can_read) -> None:
    dep = [Depends(can_read)]

    def search(c, table: str, key: str, q: Optional[str], extra: List[str], args: List[Any],
               limit: int, offset: int, order: str):
        where = list(extra)
        if q:
            if q.isdigit():
                where.append(f"({key}=? OR name LIKE ? ESCAPE '\\' OR name_zh LIKE ? ESCAPE '\\')")
                args += [int(q), _like(q), _like(q)]
            else:
                where.append("(name LIKE ? ESCAPE '\\' OR name_zh LIKE ? ESCAPE '\\')")
                args += [_like(q), _like(q)]
        sql = f"FROM {table}" + (" WHERE " + " AND ".join(where) if where else "")
        total = c.execute("SELECT COUNT(*) " + sql, args).fetchone()[0]
        rows = c.execute(f"SELECT * {sql} ORDER BY {order} LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
        return total, rows

    @app.get("/api/v1/world/sources", dependencies=dep)
    def sources():
        """Which data is installed: source, version, game build and row counts."""
        if not world.available():
            return {"available": False, "items": []}
        with world.conn() as c:
            rows = c.execute("SELECT * FROM world_data_sources ORDER BY id").fetchall()
        return {"available": True, "items": [
            {**dict(r), "counts": json.loads(r["counts"] or "{}"),
             "upstream": json.loads(r["upstream"] or "[]")} for r in rows]}

    @app.get("/api/v1/world/maps", dependencies=dep)
    def maps(q: Optional[str] = None, zones_only: bool = True):
        """Zones and their uiMapID. zones_only=false also lists subzones."""
        with world.conn() as c:
            extra = ["ui_map_id IS NOT NULL"] + (["parent_area_id IS NULL"] if zones_only else [])
            _, rows = search(c, "world_maps", "area_id", q, extra, [], 5000, 0, "ui_map_id, area_id")
        return {"items": [{"area_id": r["area_id"], "map_id": r["ui_map_id"], "name": _name(r),
                           "name_en": r["name"], "parent_area_id": r["parent_area_id"]} for r in rows]}

    @app.get("/api/v1/world/quests", dependencies=dep)
    def quests(q: Optional[str] = None, map_id: Optional[int] = None,
               min_level: Optional[int] = None, max_level: Optional[int] = None,
               include_hidden: bool = False, limit: int = Query(50, ge=1, le=500),
               offset: int = Query(0, ge=0)):
        """Search the quest database by name (`q`), zone (`map_id`) or level. Static game data, not a character's progress."""
        extra, args = [], []
        if not include_hidden:
            extra.append("hidden=0")
        for cond, val in (("ui_map_id=?", map_id), ("level>=?", min_level), ("level<=?", max_level)):
            if val is not None:
                extra.append(cond)
                args.append(val)
        with world.conn() as c:
            total, rows = search(c, "world_quests", "quest_id", q, extra, args, limit, offset,
                                 "level, quest_id")
        return {"total": total, "items": [
            {"id": r["quest_id"], "name": _name(r), "name_en": r["name"], "level": r["level"],
             "min_level": r["min_level"], "map_id": r["ui_map_id"], "xp": r["xp"],
             "xp_confirmed": bool(r["xp_confirmed"])} for r in rows]}

    @app.get("/api/v1/world/quests/{quest_id}")
    def quest(quest_id: int, character_id: Optional[str] = None, scope: Optional[str] = Depends(can_read)):
        """One quest: objectives, where it starts and ends, experience, rewards, what it leads to.

        With `character_id` the answer also says where that character stands with it (`status`)."""
        if scope is not None and character_id not in (None, scope):
            raise HTTPException(403, "this link only covers one character")
        with world.conn() as c:
            r = c.execute("SELECT * FROM world_quests WHERE quest_id=?", (quest_id,)).fetchone()
            if not r:
                raise HTTPException(404, "quest not found")
            d = json.loads(r["data"] or "{}")
            zh = json.loads(r["text_zh"] or "{}")
            txt = d.get("text") or {}

            def people(group: Dict[str, Any]) -> List[Dict[str, Any]]:
                out = [_brief(c, "npc", i) for i in group.get("npcs") or []]
                out += [_brief(c, "object", i) for i in group.get("objects") or []]
                out += [{"kind": "item", **_item_brief(c, i)} for i in group.get("items") or []]
                return out

            obj = d.get("objectives") or {}
            objectives = [_brief(c, "npc", o["id"]) for o in obj.get("npcs") or [] if "id" in o]
            objectives += [_brief(c, "object", o["id"]) for o in obj.get("objects") or [] if "id" in o]
            for o in obj.get("items") or []:
                if "id" not in o:
                    continue
                it = {"kind": "item", **_item_brief(c, o["id"])}
                drops = c.execute(
                    "SELECT source_kind, source_entity, chance FROM world_drops WHERE item_id=? "
                    "ORDER BY chance DESC LIMIT 5", (o["id"],)).fetchall()
                it["dropped_by"] = [{**_source(c, s["source_kind"], s["source_entity"]),
                                     "chance": s["chance"] or None} for s in drops]
                objectives.append(it)
            rewards = d.get("rewards") or {}
            pre = d.get("prerequisites") or {}
            out = {
                "id": quest_id, "name": _name(r), "name_en": r["name"], "level": r["level"],
                "min_level": r["min_level"], "map_id": r["ui_map_id"], "zone": _zone(c, r["area_id"]),
                "hidden": bool(r["hidden"]),
                "description": zh.get("description") or txt.get("description"),
                "objective_text": zh.get("objectives") or txt.get("objectives") or [],
                "starters": people(d.get("starters") or {}), "finishers": people(d.get("finishers") or {}),
                "objectives": objectives,
                "prerequisites": [_quest_brief(c, i) for i in (pre.get("all") or []) + (pre.get("any") or [])],
                "next_quest": _quest_brief(c, r["next_quest"]) if r["next_quest"] else None,
                "rewards": {"xp": r["xp"], "xp_confirmed": bool(r["xp_confirmed"]),
                            "choice": bool(rewards.get("choice")),
                            "items": [_item_brief(c, i) for i in rewards.get("items") or []]},
            }
        if character_id:
            out["status"] = quest_status(character_id, quest_id)
        return out

    @app.get("/api/v1/world/quest-pins")
    def quest_pins(character_id: str, scope: Optional[str] = Depends(can_read)):
        """Map pins for the quests in a character's log: where each is handed in, and where
        the things an unfinished quest needs can be found (kill targets, objects, droppers)."""
        if scope is not None and character_id != scope:
            raise HTTPException(403, "this link only covers one character")
        with db() as conn:
            snap = conn.execute("SELECT payload_json FROM character_snapshots WHERE character_id=?",
                                (character_id,)).fetchone()
        log = (json.loads(snap["payload_json"]).get("quests") or []) if snap else []
        pins: List[Dict[str, Any]] = []
        with world.conn() as c:
            for q in log:
                qid = q.get("quest_id")
                row = c.execute("SELECT data FROM world_quests WHERE quest_id=?", (qid,)).fetchone() \
                    if isinstance(qid, int) else None
                if not row or not row["data"]:
                    continue
                d = json.loads(row["data"])
                base = {"quest_id": qid, "title": q.get("title"), "complete": bool(q.get("complete"))}

                def add(role: str, kind: str, eid: int, limit: int, note: Optional[str] = None) -> None:
                    e = _entity(c, kind, eid, spawn_limit=limit)
                    if not e or not e["name"]:
                        return
                    for sp in e["spawns"]:
                        if sp["map_id"] is not None:
                            pins.append({**base, "role": role, "name": e["name"], "note": note,
                                         "map_id": sp["map_id"], "x": sp["x"], "y": sp["y"]})

                fin = d.get("finishers") or {}
                for kind, ids in (("npc", fin.get("npcs")), ("object", fin.get("objects"))):
                    for eid in (ids or [])[:2]:
                        add("turn_in", kind, eid, 1)
                if base["complete"]:
                    continue
                obj = d.get("objectives") or {}
                for kind, key in (("npc", "npcs"), ("object", "objects")):
                    for o in (obj.get(key) or [])[:4]:
                        if "id" in o:
                            add("target", kind, o["id"], 40)
                for o in (obj.get("items") or [])[:4]:
                    if "id" not in o:
                        continue
                    item = _item_brief(c, o["id"])["name"]
                    for s in c.execute("SELECT source_kind, source_entity FROM world_drops WHERE item_id=? "
                                       "ORDER BY chance DESC LIMIT 2", (o["id"],)).fetchall():
                        add("target", s["source_kind"], s["source_entity"], 40, item)
        return {"items": pins}

    def progress(character_id: str) -> Dict[str, Any]:
        """What is known about a character's quests: the log, everything turned in, and whether
        the full completed list is available (it comes from the game, via a recent addon)."""
        with db() as conn:
            snap = conn.execute("SELECT payload_json FROM character_snapshots WHERE character_id=?",
                                (character_id,)).fetchone()
            rows = conn.execute("SELECT payload_json FROM events WHERE character_id=? AND "
                                "event_type='QUEST_COMPLETED'", (character_id,)).fetchall()
        d = json.loads(snap["payload_json"]) if snap else {}
        full = isinstance(d.get("quests_done"), list)
        done = {q for q in (d.get("quests_done") or []) if isinstance(q, int)}
        done |= {json.loads(e["payload_json"]).get("quest_id") for e in rows}
        log = {q.get("quest_id"): q for q in d.get("quests") or [] if isinstance(q.get("quest_id"), int)}
        race_id = d.get("race_id") if isinstance(d.get("race_id"), int) else RACE_ID.get(d.get("race"))
        return {"data": d, "done": done, "log": log, "full": full, "level": d.get("level"),
                "race_bit": RACE_BIT.get(race_id), "class_bit": CLASS_BIT.get(d.get("class"))}

    @app.get("/api/v1/characters/{character_id}/quests")
    def character_quests(character_id: str, scope: Optional[str] = Depends(can_read)):
        """The quests in a character's log as structured data, joined with the world database:
        progress per objective, where to hand each one in, and what it pays at this level.
        Meant for tools (an MCP server, scripts) that should not have to parse the snapshot."""
        if scope is not None and character_id != scope:
            raise HTTPException(403, "this link only covers one character")
        with db() as conn:
            snap = conn.execute("SELECT captured_at, payload_json FROM character_snapshots "
                                "WHERE character_id=?", (character_id,)).fetchone()
        if not snap:
            raise HTTPException(404, {"error": "character_not_synced", "message": "this character has no data on the server yet: it shows up once it is played with the sync program running", "character_id": character_id})
        d = json.loads(snap["payload_json"])
        items = []
        for q in d.get("quests") or []:
            objectives = []
            for o in q.get("objectives") or []:
                m = re.search(r"(\d+)\s*/\s*(\d+)", o.get("text") or "")
                objectives.append({"text": o.get("text"), "finished": bool(o.get("finished")),
                                   "current": int(m.group(1)) if m else None,
                                   "required": int(m.group(2)) if m else None})
            items.append({"quest_id": q.get("quest_id"), "title": q.get("title"), "level": q.get("level"),
                          "category": q.get("category"), "complete": bool(q.get("complete")),
                          "objectives": objectives, "world": None})
        if world.available():
            with world.conn() as c:
                for it in items:
                    r = c.execute("SELECT * FROM world_quests WHERE quest_id=?", (it["quest_id"],)).fetchone() \
                        if isinstance(it["quest_id"], int) else None
                    if not r:
                        continue
                    w = json.loads(r["data"] or "{}")
                    zh = json.loads(r["text_zh"] or "{}")

                    def first(group: Dict[str, Any]) -> Optional[Dict[str, Any]]:
                        for kind, key in (("npc", "npcs"), ("object", "objects")):
                            if group.get(key):
                                return _brief(c, kind, group[key][0])
                        return None

                    rewards = w.get("rewards") or {}
                    share = xp_share(d.get("level"), rewards.get("xpLevel") or r["level"])
                    it["world"] = {
                        "zone": _zone(c, r["area_id"]), "map_id": r["ui_map_id"],
                        "objective_text": zh.get("objectives") or (w.get("text") or {}).get("objectives") or [],
                        "turn_in": first(w.get("finishers") or {}), "start": first(w.get("starters") or {}),
                        "xp": r["xp"], "xp_now": int(round((r["xp"] or 0) * share)),
                        "xp_confirmed": bool(r["xp_confirmed"]),
                        "reward_items": [_item_brief(c, i) for i in (rewards.get("items") or [])[:8]],
                        "reward_is_choice": bool(rewards.get("choice")),
                        "next_quest": _quest_brief(c, r["next_quest"]) if r["next_quest"] else None,
                    }
        done = d.get("quests_done")
        return {"character_id": character_id, "captured_at": snap["captured_at"], "level": d.get("level"),
                "count": len(items), "ready_to_turn_in": sum(i["complete"] for i in items),
                "completed_total": len(done) if isinstance(done, list) else None, "items": items}

    def quest_status(character_id: str, quest_id: int) -> str:
        """in_log / completed / not_done / unknown. Without the game's completed list a quest
        with no record is 'unknown', not 'not done': it may predate the addon."""
        p = progress(character_id)
        if quest_id in p["log"]:
            return "in_log"
        if quest_id in p["done"]:
            return "completed"
        return "not_done" if p["full"] else "unknown"

    def zone_rows(c, p: Dict[str, Any], map_id: Optional[int] = None):
        """Quests this character could ever do (right side, class, not hidden, not seasonal or
        repeatable), each with its state. Yields (row, data, state)."""
        sql = "SELECT * FROM world_quests WHERE hidden=0 AND ui_map_id IS NOT NULL AND name IS NOT NULL"
        args: List[Any] = []
        if map_id is not None:
            sql += " AND ui_map_id=?"
            args.append(map_id)
        for r in c.execute(sql, args):
            d = json.loads(r["data"] or "{}")
            if d.get("event") or (d.get("specialFlags") or 0) & 1:
                continue
            races, classes = d.get("races") or 0, d.get("classes") or 0
            # masks go above 2^32, so test bits with arithmetic on Python ints (no 32-bit limit)
            if races and p["race_bit"] and not races & p["race_bit"]:
                continue
            if classes and p["class_bit"] and not classes & p["class_bit"]:
                continue
            qid = r["quest_id"]
            if qid in p["log"]:
                state = "in_log"
            elif qid in p["done"]:
                state = "completed"
            elif any(x in p["done"] or x in p["log"] for x in d.get("exclusiveTo") or []):
                continue                          # took the other branch; this one is closed for good
            else:
                pre = d.get("prerequisites") or {}
                need_all, need_any = pre.get("all") or [], pre.get("any") or []
                if any(x not in p["done"] for x in need_all) or (need_any and not any(x in p["done"] for x in need_any)):
                    state = "locked"
                elif r["min_level"] and p["level"] and p["level"] < r["min_level"]:
                    state = "too_low"
                else:
                    state = "available"
            yield r, d, state

    world.zone_quests = lambda character_id, map_id: zone_list(character_id, map_id)

    @app.get("/api/v1/world/quest-zones")
    def quest_zones(character_id: str, scope: Optional[str] = Depends(can_read)):
        """Per zone: how many of the quests open to this character are done, in the log or left."""
        if scope is not None and character_id != scope:
            raise HTTPException(403, "this link only covers one character")
        p = progress(character_id)
        zones: Dict[int, Dict[str, Any]] = {}
        with world.conn() as c:
            for r, _, state in zone_rows(c, p):
                z = zones.setdefault(r["ui_map_id"], {"map_id": r["ui_map_id"], "total": 0, "completed": 0,
                                                      "in_log": 0, "available": 0, "too_low": 0, "locked": 0,
                                                      "levels": []})
                z["total"] += 1
                z[state] += 1
                if r["level"] and r["level"] > 0:
                    z["levels"].append(r["level"])
            for z in zones.values():
                top = c.execute("SELECT name, name_zh FROM world_maps WHERE ui_map_id=? AND parent_area_id IS NULL "
                                "ORDER BY area_id LIMIT 1", (z["map_id"],)).fetchone()
                z["zone"] = _name(top) if top else None
                lv = sorted(z.pop("levels"))
                # the middle 80% of quest levels: a few outliers should not stretch the range
                z["level_min"], z["level_max"] = (lv[len(lv) // 10], lv[-1 - len(lv) // 10]) if lv else (None, None)
        items = sorted((z for z in zones.values() if z["zone"]), key=lambda z: (z["level_min"] or 0, z["map_id"]))
        return {"known": p["full"], "level": p["level"], "items": items}

    @app.get("/api/v1/world/zone-quests")
    def zone_quests(character_id: str, map_id: int, scope: Optional[str] = Depends(can_read)):
        """The quests of one zone for this character, the ones worth doing now first."""
        if scope is not None and character_id != scope:
            raise HTTPException(403, "this link only covers one character")
        return zone_list(character_id, map_id)

    def zone_list(character_id: str, map_id: int) -> Dict[str, Any]:
        p = progress(character_id)
        out = []
        with world.conn() as c:
            for r, d, state in zone_rows(c, p, map_id):
                share = xp_share(p["level"], (d.get("rewards") or {}).get("xpLevel") or r["level"])
                starters = d.get("starters") or {}
                start = None
                for kind, key in (("npc", "npcs"), ("object", "objects")):
                    if starters.get(key):
                        start = _brief(c, kind, starters[key][0])
                        break
                pre = d.get("prerequisites") or {}
                missing = [x for x in (pre.get("all") or []) if x not in p["done"]]
                if not missing and pre.get("any") and not any(x in p["done"] for x in pre["any"]):
                    missing = pre["any"][:1]
                out.append({
                    "id": r["quest_id"], "name": _name(r), "level": r["level"], "min_level": r["min_level"],
                    "state": state, "xp": r["xp"], "xp_confirmed": bool(r["xp_confirmed"]),
                    "xp_now": int(round((r["xp"] or 0) * share)), "xp_share": share,
                    "start": start, "needs": [_quest_brief(c, x) for x in missing[:2]] if state == "locked" else [],
                    "rewards": [_item_brief(c, i) for i in ((d.get("rewards") or {}).get("items") or [])[:6]],
                    "next_quest": r["next_quest"],
                })
            top = c.execute("SELECT name, name_zh FROM world_maps WHERE ui_map_id=? AND parent_area_id IS NULL "
                            "ORDER BY area_id LIMIT 1", (map_id,)).fetchone()
        order = {"available": 0, "in_log": 1, "too_low": 2, "locked": 3, "completed": 4}
        out.sort(key=lambda q: (order[q["state"]], -q["xp_now"], q["level"] or 0, q["id"]))
        return {"map_id": map_id, "zone": _name(top) if top else None, "known": p["full"], "level": p["level"],
                "items": out}

    @app.get("/api/v1/world/npcs", dependencies=dep)
    def npcs(q: Optional[str] = None, map_id: Optional[int] = None,
             limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
        """Search NPCs and monsters by name (`q`) or by the zone they appear in (`map_id`)."""
        extra, args = [], []
        if map_id is not None:
            extra.append("npc_id IN (SELECT entity_id FROM world_spawns WHERE kind='npc' AND ui_map_id=?)")
            args.append(map_id)
        with world.conn() as c:
            total, rows = search(c, "world_npcs", "npc_id", q, extra, args, limit, offset,
                                 "min_level, npc_id")
        return {"total": total, "items": [
            {"id": r["npc_id"], "name": _name(r), "name_en": r["name"],
             "title": r["sub_name_zh"] or r["sub_name"], "min_level": r["min_level"],
             "max_level": r["max_level"], "rank": RANKS.get(r["rank"], "normal")} for r in rows]}

    @app.get("/api/v1/world/npcs/{npc_id}", dependencies=dep)
    def npc(npc_id: int, drop_limit: int = Query(30, ge=0, le=500)):
        """One NPC or monster: level, rank, where it spawns, what it drops and sells, which quests it starts and ends."""
        with world.conn() as c:
            e = _entity(c, "npc", npc_id)
            if not e:
                raise HTTPException(404, "npc not found")
            e["quest_starts"] = [_quest_brief(c, i) for i in e["quest_starts"]]
            e["quest_ends"] = [_quest_brief(c, i) for i in e["quest_ends"]]
            rows = c.execute(
                "SELECT d.item_id, d.chance, i.name, i.name_zh FROM world_drops d LEFT JOIN world_items i "
                "USING (item_id) WHERE d.source_kind='npc' AND d.source_entity=? "
                "ORDER BY d.chance DESC, d.item_id LIMIT ?", (npc_id, drop_limit)).fetchall()
            # chance 0 means "drops, chance unknown", not "never"
            e["drops"] = [{"id": r["item_id"], "name": _name(r), "chance": r["chance"] or None} for r in rows]
            rows = c.execute(
                "SELECT v.item_id, i.name, i.name_zh FROM world_vendors v LEFT JOIN world_items i "
                "USING (item_id) WHERE v.npc_id=? ORDER BY v.item_id LIMIT 200", (npc_id,)).fetchall()
            e["sells"] = [{"id": r["item_id"], "name": _name(r)} for r in rows]
        return e

    @app.get("/api/v1/world/names", dependencies=dep)
    def names(items: str = Query("", max_length=4000)):
        """Names for a comma-separated list of item ids (for data that only recorded ids)."""
        ids = [int(x) for x in items.split(",") if x.strip().isdigit()][:500]
        needed: Dict[str, List[int]] = {}
        if ids:
            with world.conn() as c:
                for r in c.execute("SELECT item_id, quest_id FROM world_quest_items WHERE item_id IN (%s)"
                                   % ",".join("?" * len(ids)), ids):
                    needed.setdefault(str(r["item_id"]), []).append(r["quest_id"])
        # needed_by: quests that ask for the item, so a bag can flag what to keep
        return {"items": {str(k): v for k, v in world.item_names(ids).items()}, "needed_by": needed}

    @app.get("/api/v1/world/items", dependencies=dep)
    def items(q: Optional[str] = None, limit: int = Query(50, ge=1, le=500),
              offset: int = Query(0, ge=0)):
        """Search items by name (`q`)."""
        with world.conn() as c:
            total, rows = search(c, "world_items", "item_id", q, [], [], limit, offset, "item_id")
        return {"total": total, "items": [
            {"id": r["item_id"], "name": _name(r), "name_en": r["name"], "level": r["level"],
             "required_level": r["required_level"]} for r in rows]}

    @app.get("/api/v1/world/items/{item_id}", dependencies=dep)
    def item(item_id: int, limit: int = Query(20, ge=1, le=200)):
        """One item: kind, level, who drops or sells it, which quests need it, what it is crafted from and into."""
        with world.conn() as c:
            r = c.execute("SELECT * FROM world_items WHERE item_id=?", (item_id,)).fetchone()
            if not r:
                raise HTTPException(404, "item not found")
            drops = c.execute("SELECT source_kind, source_entity, chance FROM world_drops WHERE item_id=? "
                              "ORDER BY chance DESC LIMIT ?", (item_id, limit)).fetchall()
            vendors = c.execute("SELECT npc_id FROM world_vendors WHERE item_id=? LIMIT ?",
                                (item_id, limit)).fetchall()

            def recipe(rc: sqlite3.Row) -> Dict[str, Any]:
                made = _item_brief(c, rc["item_id"]) if rc["item_id"] else {"id": None, "name": None}
                return {"spell_id": rc["spell_id"], "name": made["name"] or rc["name"], "item_id": rc["item_id"],
                        "makes": rc["makes"], "skill": rc["skill_zh"] or rc["skill"], "learn": rc["learn"],
                        "levels": json.loads(rc["levels"] or "[]"), "source": rc["source"],
                        "reagents": [{**_item_brief(c, i), "count": n}
                                     for i, n in json.loads(rc["reagents"] or "[]")]}

            made_by = c.execute("SELECT * FROM world_recipes WHERE item_id=? ORDER BY learn LIMIT 5",
                                (item_id,)).fetchall()
            used_in = c.execute(
                "SELECT r.*, g.amount FROM world_recipe_reagents g JOIN world_recipes r USING (spell_id) "
                "WHERE g.item_id=? ORDER BY r.learn, r.spell_id LIMIT ?", (item_id, limit)).fetchall()
            used_total = c.execute("SELECT COUNT(*) FROM world_recipe_reagents WHERE item_id=?",
                                   (item_id,)).fetchone()[0]
            quests = c.execute("SELECT quest_id FROM world_quest_items WHERE item_id=? LIMIT ?",
                               (item_id, limit)).fetchall()
            return {
                "kind": item_kind(r["class"], r["sub_class"]),
                "needed_by": [_quest_brief(c, q["quest_id"]) for q in quests],
                "crafted_by": [recipe(rc) for rc in made_by],
                "reagent_for": [{**recipe(rc), "uses": rc["amount"]} for rc in used_in],
                "reagent_for_total": used_total,
                "id": item_id, "name": _name(r), "name_en": r["name"], "level": r["level"],
                "required_level": r["required_level"],
                "dropped_by": [{**_source(c, s["source_kind"], s["source_entity"]),
                                "chance": s["chance"] or None} for s in drops],
                "dropped_by_total": c.execute("SELECT COUNT(*) FROM world_drops WHERE item_id=?",
                                              (item_id,)).fetchone()[0],
                "sold_by": [_source(c, "npc", v["npc_id"]) for v in vendors],
                "reward_of": [_quest_brief(c, i) for i in json.loads(r["reward_of"] or "[]")],
                "starts_quest": _quest_brief(c, r["start_quest"]) if r["start_quest"] else None,
            }

    @app.get("/api/v1/world/spawns", dependencies=dep)
    def spawns(kind: str = Query(..., pattern="^(npc|object)$"), id: int = Query(...),
               limit: int = Query(400, ge=1, le=2000)):
        """Every known spawn point of one NPC or object, for drawing it on a map."""
        with world.conn() as c:
            e = _entity(c, kind, id, spawn_limit=limit)
            if not e:
                raise HTTPException(404, "not found")
            return {"kind": kind, "id": id, "name": e["name"], "spawn_count": e["spawn_count"],
                    "zones": _zones(c, kind, id),
                    "spawns": [{"map_id": sp["map_id"], "x": sp["x"], "y": sp["y"]}
                               for sp in e["spawns"] if sp["map_id"] is not None]}

    def quests_of(c, ids_json: Optional[str]) -> List[Dict[str, Any]]:
        """Obtainable quests among a list of ids, lowest level first."""
        ids = json.loads(ids_json or "[]")
        if not ids:
            return []
        rows = c.execute("SELECT quest_id, name, name_zh, level, min_level FROM world_quests WHERE hidden=0 "
                         "AND quest_id IN (%s) ORDER BY level, quest_id" % ",".join("?" * len(ids)), ids)
        return [{"id": q["quest_id"], "name": _name(q), "level": q["level"], "min_level": q["min_level"]}
                for q in rows]

    @app.get("/api/v1/world/nearby", dependencies=dep)
    def nearby(map_id: int, x: float = Query(..., ge=0, le=100), y: float = Query(..., ge=0, le=100),
               radius: float = Query(5, gt=0, le=50), kind: Optional[str] = Query(None, pattern="^(npc|object)$"),
               quest_only: bool = False, limit: int = Query(30, ge=1, le=200)):
        """What spawns within `radius` map-percent of a point, nearest first, one row per NPC/object."""
        where, args = ["ui_map_id=?", "x BETWEEN ? AND ?", "y BETWEEN ? AND ?"], \
            [map_id, x - radius, x + radius, y - radius, y + radius]
        if kind:
            where.append("kind=?")
            args.append(kind)
        with world.conn() as c:
            rows = c.execute("SELECT kind, entity_id, x, y FROM world_spawns WHERE " + " AND ".join(where),
                             args).fetchall()
            best: Dict[Any, Dict[str, Any]] = {}
            for r in rows:
                dist = ((r["x"] - x) ** 2 + (r["y"] - y) ** 2) ** 0.5
                if dist > radius:
                    continue
                key = (r["kind"], r["entity_id"])
                b = best.get(key)
                if b is None:
                    b = best[key] = {"kind": r["kind"], "id": r["entity_id"], "distance": dist,
                                     "x": r["x"], "y": r["y"], "spawns_in_range": 0}
                elif dist < b["distance"]:
                    b.update(distance=dist, x=r["x"], y=r["y"])
                b["spawns_in_range"] += 1
            out = []
            for b in sorted(best.values(), key=lambda v: v["distance"]):
                if b["kind"] == "npc":
                    r = c.execute("SELECT name, name_zh, min_level, max_level, rank, quest_starts, quest_ends "
                                  "FROM world_npcs WHERE npc_id=?", (b["id"],)).fetchone()
                else:
                    r = c.execute("SELECT name, name_zh, quest_starts, quest_ends FROM world_objects "
                                  "WHERE object_id=?", (b["id"],)).fetchone()
                if not r or not _name(r):
                    continue
                starts, ends = quests_of(c, r["quest_starts"]), quests_of(c, r["quest_ends"])
                if quest_only and not (starts or ends):
                    continue
                b.update(name=_name(r), distance=round(b["distance"], 2), quest_starts=starts, quest_ends=ends)
                if b["kind"] == "npc":
                    b.update(min_level=r["min_level"], max_level=r["max_level"],
                             rank=RANKS.get(r["rank"], "normal"))
                out.append(b)
                if len(out) >= limit:
                    break
        return {"map_id": map_id, "x": x, "y": y, "radius": radius, "items": out}
