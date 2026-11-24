"""Importer for NorgannonDB (https://github.com/zmarotrix/NorgannonDB, GPL-3.0).

Its data files are Lua, but pure data: `R(id, "name", [[field=value,...]])` records and
`NorgannonDB.<table> = {...}` assignments. They are read with the same non-executing
parser the agent uses for SavedVariables.
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))
from adventuresync.luaparser import LuaParseError, _Parser  # noqa: E402

URL = "https://github.com/zmarotrix/NorgannonDB"
_ASSIGN = re.compile(r"NorgannonDB\.(\w+) = ")


def _pairs(v: Any) -> Iterator[Tuple[Any, Any]]:
    """Iterate a parsed Lua table whether it came out as a dict or as a 1..n list."""
    if isinstance(v, dict):
        return iter(v.items())
    if isinstance(v, list):
        return iter(enumerate(v, 1))
    return iter(())


def _list(v: Any) -> list:
    return v if isinstance(v, list) else [x for _, x in sorted(_pairs(v))] if v else []


def records(text: str) -> Iterator[Tuple[int, Optional[str], Dict[str, Any]]]:
    """Yield (id, name, fields) for every `R(id,"name",[[...]])` line."""
    for line in text.splitlines():
        if not line.startswith("R("):
            continue
        p = _Parser(line)
        p.i = 2
        rid = p.number()
        p.expect(",")
        name = p.value()   # a string, or nil for records whose name is not known yet
        fields: Dict[str, Any] = {}
        if p.peek() == ",":
            start, end = line.find("[[", p.i), line.rfind("]])")
            if start < 0 or end < start:
                raise LuaParseError(f"malformed record {rid}")
            body = _Parser("{" + line[start + 2:end] + "}").table()
            fields = body if isinstance(body, dict) else {}
        yield rid, name, fields


def assignments(text: str) -> Dict[str, Any]:
    """Return {table name: value} for every `NorgannonDB.<name> = {...}` in the text."""
    out: Dict[str, Any] = {}
    for m in _ASSIGN.finditer(text):
        p = _Parser(text)
        p.i = m.end()
        out[m.group(1)] = p.value()
    return out


def locale(text: str) -> Tuple[Dict[str, Any], Dict[int, Tuple[str, Dict[str, Any]]]]:
    """Parse a locale file: the `local L = {...}` table and the localized quest records."""
    m = re.search(r"local L = ", text)
    table: Dict[str, Any] = {}
    if m:
        p = _Parser(text)
        p.i = m.end()
        table = p.value()
    quests = {rid: (name, f) for rid, name, f in records(text)}
    return table, quests


def _js(v: Any) -> Optional[str]:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":")) if v else None


def _jsonable(v: Any) -> Any:
    """Lua tables with numeric keys become JSON objects with string keys."""
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    return v


def import_into(conn, src: Path, log=print) -> Dict[str, int]:
    read = lambda rel: (src / rel).read_text(encoding="utf-8")  # noqa: E731
    info = assignments(read("data/info.lua")).get("info") or {}
    toc = read("NorgannonDB.toc")
    ver = re.search(r"## Version: *(\S+)", toc)
    world = assignments(read("data/world.lua"))
    zones = world.get("zones") or {}
    area_to_ui = dict(_pairs((world.get("maps") or {}).get("areaToUiMap")))
    zh, zh_quests = ({}, {})
    if (src / "locales/zhTW.lua").exists():
        zh, zh_quests = locale(read("locales/zhTW.lua"))
    zh_of = lambda kind: dict(_pairs(zh.get(kind)))  # noqa: E731

    cur = conn.cursor()
    cur.execute(
        "INSERT INTO world_data_sources (name, version, flavor, game_build, interface, generated,"
        " license, url, upstream, imported_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("NorgannonDB", ver.group(1) if ver else None, info.get("flavor"), info.get("build"),
         info.get("interface"), info.get("generated"), "GPL-3.0", URL,
         _js(_jsonable(info.get("sources"))), datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
    sid = cur.lastrowid

    def ui_map(area: Any) -> Optional[int]:
        seen = set()
        while isinstance(area, int) and area not in seen:
            seen.add(area)
            z = zones.get(area) or {}
            if area in area_to_ui or z.get("uiMapId"):
                return area_to_ui.get(area) or z.get("uiMapId")
            area = z.get("parent")
        return None

    counts = {"maps": 0, "quests": 0, "npcs": 0, "objects": 0, "items": 0, "spawns": 0,
              "drops": 0, "vendors": 0, "recipes": 0}
    zone_zh = zh_of("zones")
    for area, z in _pairs(zones):
        cur.execute("INSERT INTO world_maps VALUES (?,?,?,?,?,?)",
                    (area, ui_map(area), z.get("parent"), z.get("name"), zone_zh.get(area), sid))
        counts["maps"] += 1

    def spawns(kind: str, eid: int, f: Dict[str, Any]) -> None:
        rows = []
        for area, pts in _pairs(f.get("spawns")):
            um = ui_map(area)
            for pt in _list(pts):
                pt = _list(pt)
                if len(pt) < 2 or pt[0] is None or pt[0] < 0:  # {-1,-1}: inside an instance
                    continue
                rows.append((kind, eid, area, um, pt[0], pt[1],
                             (pt[2] if len(pt) > 2 else None) or f.get("respawn"), sid))
        cur.executemany("INSERT INTO world_spawns (kind, entity_id, area_id, ui_map_id, x, y,"
                        " respawn, source_id) VALUES (?,?,?,?,?,?,?,?)", rows)
        counts["spawns"] += len(rows)

    def drops(kind: str, eid: int, f: Dict[str, Any]) -> None:
        rows = [(kind, eid, item, chance or 0) for item, chance in _pairs(f.get("drops"))
                if isinstance(item, int)]
        cur.executemany("INSERT OR REPLACE INTO world_drops VALUES (?,?,?,?)", rows)
        counts["drops"] += len(rows)

    log("quests ...")
    for qid, name, f in records(read("data/quests.lua")):
        zos = f.get("zoneOrSort")
        area = zos if isinstance(zos, int) and zos > 0 else None
        rewards = f.get("rewards") or {}
        zq = zh_quests.get(qid)
        keep = {k: f[k] for k in ("starters", "finishers", "objectives", "prerequisites", "rewards",
                                  "text", "parentQuest", "breadcrumbFor", "requiredItems",
                                  "sourceItem", "races", "classes", "flags", "hiddenReason",
                                  "startNote", "leadIn", "exclusiveTo", "event", "specialFlags") if k in f}
        cur.execute(
            "INSERT INTO world_quests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (qid, name, zq[0] if zq else None, f.get("level"), f.get("minLevel"), area, ui_map(area),
             1 if f.get("hidden") is True else 0, rewards.get("xp"),
             1 if rewards.get("xpConfirmed") else 0, f.get("nextQuestInChain"),
             _js(_jsonable(keep)), _js(_jsonable(zq[1])) if zq else None, sid))
        counts["quests"] += 1
        if f.get("hidden") is not True:            # which quests want an item collected
            needed = {o.get("id") for o in _list((f.get("objectives") or {}).get("items"))
                      if isinstance(o, dict)}
            cur.executemany("INSERT OR IGNORE INTO world_quest_items VALUES (?,?)",
                            [(i, qid) for i in needed if isinstance(i, int)])

    log("npcs ...")
    npc_zh = zh_of("npcs")
    for nid, name, f in records(read("data/npcs.lua")):
        z = npc_zh.get(nid) or {}
        cur.execute(
            "INSERT INTO world_npcs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (nid, name, z.get("name"), f.get("subName"), z.get("subName"), f.get("minLevel"),
             f.get("maxLevel"), f.get("rank") or 0, f.get("reaction"), f.get("zone"),
             f.get("respawn"), _js(f.get("questStarts")), _js(f.get("questEnds")), sid))
        spawns("npc", nid, f)
        drops("npc", nid, f)
        rows = [(nid, item, n) for item, n in _pairs(f.get("sells")) if isinstance(item, int)]
        cur.executemany("INSERT OR REPLACE INTO world_vendors VALUES (?,?,?)", rows)
        counts["vendors"] += len(rows)
        counts["npcs"] += 1

    log("objects ...")
    obj_zh = zh_of("objects")
    for oid, name, f in records(read("data/objects.lua")):
        cur.execute("INSERT INTO world_objects VALUES (?,?,?,?,?,?,?)",
                    (oid, name, obj_zh.get(oid), f.get("zone"), _js(f.get("questStarts")),
                     _js(f.get("questEnds")), sid))
        spawns("object", oid, f)
        drops("object", oid, f)
        counts["objects"] += 1

    log("items ...")
    item_zh = zh_of("items")
    for iid, name, f in records(read("data/items.lua")):
        cur.execute("INSERT INTO world_items VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (iid, name, item_zh.get(iid), f.get("level"), f.get("requiredLevel"),
                     f.get("class"), f.get("subClass"), f.get("startQuest"),
                     _js(f.get("rewardOf")), sid))
        counts["items"] += 1

    if (src / "data/recipes.lua").exists():
        log("recipes ...")
        skills = dict(_pairs(world.get("professions")))
        skills_zh = zh_of("professions")
        for spell, f in _pairs(assignments(read("data/recipes.lua")).get("recipes")):
            if not isinstance(f, dict):
                continue
            reagents = [r for r in (_list(x) for x in _list(f.get("reagents"))) if len(r) >= 2]
            how = "auto" if f.get("auto") else "trainer" if f.get("trainers") else \
                "recipe" if f.get("items") else "quest" if f.get("quests") else None
            cur.execute("INSERT INTO world_recipes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (spell, f.get("name"), f.get("skill"), skills.get(f.get("skill")),
                         skills_zh.get(f.get("skill")), f.get("item"), f.get("count") or 1, f.get("learn"),
                         _js(_list(f.get("levels"))), _js([[r[0], r[1]] for r in reagents]), how, sid))
            cur.executemany("INSERT OR IGNORE INTO world_recipe_reagents VALUES (?,?,?)",
                            [(r[0], spell, r[1]) for r in reagents])
            counts["recipes"] += 1

    cur.execute("UPDATE world_data_sources SET counts = ? WHERE id = ?", (_js(counts), sid))
    conn.commit()
    return counts
