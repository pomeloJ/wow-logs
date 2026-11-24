"""Every hit, miss and heal from the game's combat log file, one row each.

The agent sends the rows as it reads the file. Nothing is added up when they arrive: the
statistics shown anywhere are worked out from these rows when asked for, so a new kind of
analysis also covers the fights already recorded.
"""
import math
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

COLUMNS = ("pos", "ts", "way", "who", "pet", "unit", "enemy", "npc_id", "spell_id", "spell", "school", "kind",
           "amount", "raw", "overkill", "resisted", "blocked", "absorbed", "crit", "glancing", "crushing", "miss",
           "t_hp", "t_maxhp", "t_armor", "t_level", "s_ap", "guid")
SCHEMA = """
CREATE TABLE IF NOT EXISTS combat_hits (
    source TEXT NOT NULL, pos INTEGER NOT NULL, character_id TEXT NOT NULL, ts INTEGER NOT NULL,
    way TEXT NOT NULL, who TEXT NOT NULL, pet INTEGER NOT NULL, unit TEXT, enemy TEXT, npc_id INTEGER,
    spell_id INTEGER, spell TEXT, school INTEGER, kind TEXT, amount REAL, raw REAL, overkill REAL,
    resisted REAL, blocked REAL, absorbed REAL, crit INTEGER, glancing INTEGER, crushing INTEGER, miss TEXT,
    t_hp REAL, t_maxhp REAL, t_armor REAL, t_level REAL, s_ap REAL, guid TEXT,
    PRIMARY KEY (character_id, source, pos));
CREATE INDEX IF NOT EXISTS ix_hits_time ON combat_hits(character_id, ts);
"""
MISS_NAMES = {"MISS": "未擊中", "DODGE": "閃躲", "PARRY": "招架", "BLOCK": "格擋", "RESIST": "抵抗", "IMMUNE": "免疫",
              "ABSORB": "吸收", "EVADE": "閃避", "DEFLECT": "偏轉", "REFLECT": "反射"}


class HitsIn(BaseModel):
    character_id: str = Field(min_length=1, max_length=200)
    source: str = Field(min_length=1, max_length=200)
    rows: List[List[Any]] = Field(min_length=1, max_length=5000)


def _spread(values: List[float]) -> Dict[str, Any]:
    """Count, smallest, largest, mean and total of a list of amounts."""
    if not values:
        return {"n": 0, "min": None, "max": None, "avg": None, "total": 0}
    return {"n": len(values), "min": round(min(values)), "max": round(max(values)),
            "avg": round(sum(values) / len(values), 1), "total": round(sum(values))}


def _sd(values: List[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return math.sqrt(sum((x - mean) ** 2 for x in values) / (n - 1))


def _middle(values: List[float]) -> Optional[float]:
    if not values:
        return None
    v, n = sorted(values), len(values)
    return round(v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2, 1)


def _out(character_id: str, start_ms: int, end_ms: int, enemy: Optional[str] = None):
    """WHERE clause and arguments for what the group dealt in a stretch of time, to one kind
    of enemy when asked."""
    where, args = "character_id=? AND ts>=? AND ts<=? AND way='out'", [character_id, start_ms, end_ms]
    if enemy:
        where, args = where + " AND enemy=?", args + [enemy]
    return where, args


def enemies(conn, character_id: str, start_ms: int, end_ms: int) -> List[Dict[str, Any]]:
    """What the group fought, by damage dealt to it."""
    where, args = _out(character_id, start_ms, end_ms)
    return [{"name": r["enemy"], "npc_id": r["npc_id"], "damage": round(r["total"] or 0), "hits": r["n"]}
            for r in conn.execute(f"SELECT enemy, MAX(npc_id) npc_id, SUM(amount) total, COUNT(*) n FROM combat_hits "
                                  f"WHERE {where} AND enemy IS NOT NULL GROUP BY enemy ORDER BY total DESC LIMIT 80", args)]


def amounts(conn, character_id: str, start_ms: int, end_ms: int, me: str, spell: str, pet: bool,
            enemy: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every landed hit of one ability, per member, normal and critical apart."""
    where, args = _out(character_id, start_ms, end_ms, enemy)
    out: Dict[str, Dict[str, Any]] = {}
    for r in conn.execute(f"SELECT who, amount, crit FROM combat_hits WHERE {where} AND spell=? AND pet=? "
                          f"AND miss IS NULL ORDER BY ts, pos", args + [spell, int(pet)]):
        mine = out.setdefault(r["who"] or me, {"who": r["who"] or me, "normal": [], "crit": []})
        mine["crit" if r["crit"] else "normal"].append(round(r["amount"] or 0))
    return sorted(out.values(), key=lambda m: -(sum(m["normal"]) + sum(m["crit"])))


def dps(conn, character_id: str, start_ms: int, end_ms: int, me: str, step: int) -> Dict[str, Any]:
    """Damage per member in slices of `step` seconds, the member's own and their pet's apart."""
    step = max(1, step)
    while (end_ms - start_ms) / 1000 / step > 1500:   # never more points than a chart can show
        step *= 2
    n = int((end_ms - start_ms) / 1000 / step) + 1
    where, args = _out(character_id, start_ms, end_ms)
    series: Dict[str, Dict[str, List[int]]] = {}
    for r in conn.execute(f"SELECT who, pet, ts, amount FROM combat_hits WHERE {where} AND miss IS NULL", args):
        mine = series.setdefault(r["who"] or me, {"own": [0] * n, "pet": [0] * n})
        mine["pet" if r["pet"] else "own"][min(n - 1, int((r["ts"] - start_ms) / 1000 / step))] += round(r["amount"] or 0)
    return {"start": start_ms, "step": step, "count": n, "series": series}


def events(conn, character_id: str, start_ms: int, end_ms: int, me: str, limit: int = 600) -> List[Dict[str, Any]]:
    """The fight line by line, oldest first."""
    rows = conn.execute(
        "SELECT ts, way, who, pet, unit, enemy, spell, kind, amount, crit, miss FROM combat_hits WHERE character_id=? "
        "AND ts>=? AND ts<=? AND way<>'cast' ORDER BY ts, pos LIMIT ?", (character_id, start_ms, end_ms, limit)).fetchall()
    return [{**dict(r), "who": r["who"] or me, "amount": round(r["amount"] or 0),
             "miss": MISS_NAMES.get(r["miss"], r["miss"]) if r["miss"] else None} for r in rows]


def csv_lines(conn, character_id: str, start_ms: int, end_ms: int, me: str):
    """All rows of a stretch of time as CSV text, a line at a time."""
    import csv
    import io
    cols = ("ts",) + COLUMNS[2:]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(cols)
    for r in conn.execute(f"SELECT {', '.join(cols)} FROM combat_hits WHERE character_id=? AND ts>=? AND ts<=? "
                          "ORDER BY ts, pos", (character_id, start_ms, end_ms)):
        row = list(r)
        row[2] = row[2] or me
        # names come from the game and from other players: text a spreadsheet would run as a
        # formula is made plain text
        writer.writerow(["'" + v if isinstance(v, str) and v[:1] in "=+-@" else v for v in row])
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate()
    yield buf.getvalue()


def skills(conn, character_id: str, start_ms: int, end_ms: int, me: str, enemy: Optional[str] = None) -> List[Dict[str, Any]]:
    """Per member and ability: how often it was used, how it landed and how hard it hit."""
    where, args = _out(character_id, start_ms, end_ms, enemy)
    rows = conn.execute(
        f"SELECT who, pet, spell, kind, amount, raw, crit, glancing, miss FROM combat_hits WHERE {where}", args).fetchall()
    groups: Dict[Any, Dict[str, Any]] = {}
    for r in rows:
        g = groups.setdefault((r["who"] or me, r["spell"], r["pet"]), {"kind": r["kind"], "normal": [], "crit": [],
                                                                      "miss": {}, "raw": 0, "raw_of": 0, "glancing": 0})
        if r["miss"]:
            g["miss"][r["miss"]] = g["miss"].get(r["miss"], 0) + 1
            continue
        g["crit" if r["crit"] else "normal"].append(r["amount"] or 0)
        g["glancing"] += r["glancing"] or 0
        if r["raw"] and not r["crit"] and not r["glancing"]:      # the logged "before" leaves the crit bonus out
            g["raw"] += r["raw"]
            g["raw_of"] += r["amount"] or 0
    out = []
    for (who, spell, pet), g in groups.items():
        landed = g["normal"] + g["crit"]
        if not landed and not g["miss"]:
            continue
        missed = sum(g["miss"].values())
        all_, total = _spread(landed), sum(landed)
        mean = total / len(landed) if landed else 0
        out.append({
            "who": who, "spell": spell, "pet": bool(pet), "kind": g["kind"],
            "can_crit": g["kind"] not in ("dot", "shield"),      # ticks and thorns never crit here
            "attempts": len(landed) + missed, "hits": len(landed), "total": all_["total"],
            "min": all_["min"], "max": all_["max"], "avg": all_["avg"], "median": _middle(landed),
            "sd": round(math.sqrt(sum((x - mean) ** 2 for x in landed) / len(landed)), 1) if landed else None,
            # where the true average probably lies, given how many hits there were
            "ci": [round(mean - 1.96 * _sd(landed) / math.sqrt(len(landed)), 1),
                   round(mean + 1.96 * _sd(landed) / math.sqrt(len(landed)), 1)] if len(landed) > 1 else None,
            "normal": _spread(g["normal"]), "crit": _spread(g["crit"]), "glancing": g["glancing"],
            "misses": [{"type": k, "name": MISS_NAMES.get(k, k), "count": v}
                       for k, v in sorted(g["miss"].items(), key=lambda kv: -kv[1])],
            "missed": missed,
            # what armour and resistances took off, where the log gives the amount before them
            "reduced": round(1 - g["raw_of"] / g["raw"], 3) if g["raw"] > 0 else None,
        })
    out.sort(key=lambda s: -s["total"])
    return out


# ------------------------------------------------------------------ studying the targets
SCHOOLS = {1: "物理", 2: "神聖", 4: "火焰", 8: "自然", 16: "冰霜", 32: "暗影", 64: "秘法"}
TYPES = {"swing": "melee", "range": "ranged", "spell": "spell"}
RESULTS = {"normal": "普通命中", "crit": "致命一擊", "glancing": "偏斜", **MISS_NAMES}
POWER = {"0": "法力", "1": "怒氣", "2": "集中", "3": "能量"}


def wilson(k: int, n: int) -> Optional[List[float]]:
    """95% interval for a share seen k times in n tries, in percent."""
    if n <= 0:
        return None
    z, p = 1.96, k / n
    d = 1 + z * z / n
    mid, half = (p + z * z / (2 * n)) / d, z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, mid - half) * 100, 1), round(min(1.0, mid + half) * 100, 1)]


def _share(k: int, n: int) -> Dict[str, Any]:
    return {"n": k, "of": n, "pct": round(k / n * 100, 1) if n else None, "ci": wilson(k, n)}


def _outcome(r) -> str:
    return r["miss"] or ("crit" if r["crit"] else "glancing" if r["glancing"] else "normal")


def _levels(values) -> Optional[str]:
    seen = sorted({int(v) for v in values if v})
    return None if not seen else str(seen[0]) if len(seen) == 1 else f"{seen[0]}–{seen[-1]}"


def defence(conn, character_id: str, start_ms: int, end_ms: int, me: str, npc_info=None) -> List[Dict[str, Any]]:
    """What each kind of enemy did with the group's attacks: how they landed, what its armour
    took off, how long it lived. Measured from the rows; nothing here is a game constant."""
    where, args = _out(character_id, start_ms, end_ms)
    found: Dict[str, Dict[str, Any]] = {}
    for r in conn.execute(f"SELECT * FROM combat_hits WHERE {where} AND enemy IS NOT NULL ORDER BY ts, pos", args):
        e = found.setdefault(r["enemy"], {"npc_id": r["npc_id"], "damage": 0, "hits": 0, "types": {}, "members": {},
                                          "armor": [], "levels": set(), "schools": {}, "units": {}, "blocked": 0,
                                          "blockable": 0})
        kind = "pet" if r["pet"] and r["kind"] == "swing" else TYPES.get(r["kind"]) if not r["pet"] else None
        result = _outcome(r)
        if kind:
            table = e["types"].setdefault(kind, {})
            table[result] = table.get(result, 0) + 1
            if kind != "pet":
                mine = e["members"].setdefault(r["who"] or me, {})
                mine[result] = mine.get(result, 0) + 1
            if kind in ("melee", "ranged"):
                e["blockable"] += 1
                e["blocked"] += 1 if (r["blocked"] or 0) > 0 or r["miss"] == "BLOCK" else 0
        if r["miss"]:
            continue
        e["damage"] += r["amount"] or 0
        e["hits"] += 1
        if r["t_armor"] is not None:
            e["armor"].append(r["t_armor"])
            e["levels"].add(r["t_level"])
        if r["raw"] and not r["glancing"] and not r["crit"] and r["school"] in SCHOOLS:
            sch = e["schools"].setdefault(r["school"], [0.0, 0.0, 0])
            sch[0] += r["raw"]
            sch[1] += r["amount"] or 0
            sch[2] += 1
        if r["guid"]:
            unit = e["units"].setdefault(r["guid"], [r["ts"], None])
            if r["overkill"] is not None and r["overkill"] >= 0:
                unit[1] = r["ts"]
    out = []
    for name, e in found.items():
        info = npc_info(e["npc_id"]) if npc_info and e["npc_id"] else None
        lived = sorted((end - first) / 1000 for first, end in e["units"].values() if end is not None)
        armor = sorted(e["armor"])

        def table(counts: Dict[str, int]) -> Dict[str, Any]:
            n = sum(counts.values())
            return {"attempts": n, "results": [{"key": k, "name": RESULTS.get(k, k), **_share(v, n)}
                                               for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]}
        out.append({
            "name": name, "npc_id": e["npc_id"], "level": _levels(e["levels"]) or (info or {}).get("level"),
            "rank": (info or {}).get("rank"), "damage": round(e["damage"]), "hits": e["hits"],
            # armour changes with what is cast on it, so the range is shown: the top is the bare value
            "armor": {"min": round(armor[0]), "max": round(armor[-1]), "median": round(armor[len(armor) // 2]),
                      "n": len(armor)} if armor else None,
            "schools": [{"school": SCHOOLS[k], "reduced": round(1 - v[1] / v[0], 3), "n": v[2]}
                        for k, v in sorted(e["schools"].items()) if v[0] > 0],
            "types": {k: table(v) for k, v in e["types"].items()},
            "members": [{"who": w, **table(c)} for w, c in sorted(e["members"].items(), key=lambda kv: -sum(kv[1].values()))],
            "block": _share(e["blocked"], e["blockable"]) if e["blockable"] else None,
            "units": len(e["units"]), "kills": len(lived),
            "ttk": {"avg": round(sum(lived) / len(lived), 1), "median": round(lived[len(lived) // 2], 1)} if lived else None,
        })
    out.sort(key=lambda x: -x["damage"])
    return out


def across(conn, character_id: str, start_ms: int, end_ms: int, me: str, who: str, spell: str, pet: bool) -> List[Dict[str, Any]]:
    """One member's ability against each kind of enemy: plain hits only, so crit luck stays out."""
    where, args = _out(character_id, start_ms, end_ms)
    found: Dict[str, Dict[str, Any]] = {}
    for r in conn.execute(f"SELECT enemy, amount, raw, t_armor, t_level FROM combat_hits WHERE {where} AND spell=? AND pet=? "
                          f"AND who=? AND miss IS NULL AND crit=0 AND glancing=0 AND enemy IS NOT NULL",
                          args + [spell, int(pet), "" if who == me else who]):
        e = found.setdefault(r["enemy"], {"v": [], "raw": 0.0, "of": 0.0, "armor": [], "levels": set()})
        e["v"].append(r["amount"] or 0)
        if r["raw"]:
            e["raw"] += r["raw"]
            e["of"] += r["amount"] or 0
        if r["t_armor"] is not None:
            e["armor"].append(r["t_armor"])
            e["levels"].add(r["t_level"])
    out = []
    for name, e in found.items():
        v, n = e["v"], len(e["v"])
        if n < 3:
            continue                                  # too few hits to say anything
        mean = sum(v) / n
        sd = math.sqrt(sum((x - mean) ** 2 for x in v) / (n - 1))
        half = 1.96 * sd / math.sqrt(n)
        out.append({"enemy": name, "n": n, "avg": round(mean, 1), "min": round(min(v)), "max": round(max(v)),
                    "sd": round(sd, 1), "ci": [round(mean - half, 1), round(mean + half, 1)],
                    "reduced": round(1 - e["of"] / e["raw"], 3) if e["raw"] > 0 else None,
                    "armor": round(sorted(e["armor"])[len(e["armor"]) // 2]) if e["armor"] else None,
                    "level": _levels(e["levels"])})
    out.sort(key=lambda x: -x["avg"])
    return out


def level_gaps(conn, character_id: str, start_ms: int, end_ms: int, me: str, levels: Dict[str, int]) -> List[Dict[str, Any]]:
    """How attacks landed by how many levels the target was above the attacker."""
    where, args = _out(character_id, start_ms, end_ms)
    rows = conn.execute(f"SELECT who, pet, kind, crit, glancing, miss, guid, t_level FROM combat_hits WHERE {where}", args).fetchall()
    level_of = {r["guid"]: int(r["t_level"]) for r in rows if r["guid"] and r["t_level"]}
    found: Dict[Any, Dict[str, int]] = {}
    for r in rows:
        kind, mine, theirs = TYPES.get(r["kind"]), levels.get(r["who"] or me), level_of.get(r["guid"])
        if r["pet"] or not kind or not mine or theirs is None:
            continue
        table = found.setdefault((kind, theirs - mine), {})
        table[_outcome(r)] = table.get(_outcome(r), 0) + 1
    out = []
    for (kind, gap), counts in sorted(found.items()):
        n = sum(counts.values())
        landed = sum(v for k, v in counts.items() if k in ("normal", "crit", "glancing"))
        out.append({"type": kind, "gap": gap, "attempts": n, "landed": _share(landed, n),
                    **{k.lower(): _share(counts.get(k, 0), n) for k in ("MISS", "DODGE", "PARRY", "RESIST")},
                    "glancing": _share(counts.get("glancing", 0), n), "crit": _share(counts.get("crit", 0), landed)})
    return out


def _gaps(times: List[int], longest: int) -> List[int]:
    return [b - a for a, b in zip(times, times[1:]) if 0 < b - a <= longest]


def pets(conn, character_id: str, start_ms: int, end_ms: int, me: str) -> List[Dict[str, Any]]:
    """Each pet as a fighter of its own: what it dealt, how it landed, what it took."""
    found: Dict[Any, Dict[str, Any]] = {}
    for r in conn.execute("SELECT * FROM combat_hits WHERE character_id=? AND ts>=? AND ts<=? AND pet=1 AND way IN ('out','in') "
                          "ORDER BY ts, pos", (character_id, start_ms, end_ms)):
        p = found.setdefault((r["who"] or me, r["unit"]), {"damage": 0, "swing": [], "crits": 0, "tries": 0, "landed": 0,
                                                          "skill": 0, "times": [], "taken": 0, "hit_by": 0, "active": set()})
        if r["way"] == "in":
            p["taken"] += r["amount"] or 0
            p["hit_by"] += 0 if r["miss"] else 1
            continue
        p["tries"] += 1
        p["active"].add(r["ts"] // 5000)
        if r["kind"] == "swing":
            p["times"].append(r["ts"])
        if r["miss"]:
            continue
        p["landed"] += 1
        p["crits"] += r["crit"] or 0
        p["damage"] += r["amount"] or 0
        if r["kind"] == "swing":
            p["swing"].append(r["amount"] or 0)
        else:
            p["skill"] += r["amount"] or 0
    died: Dict[Any, int] = {}
    for r in conn.execute("SELECT who, unit FROM combat_hits WHERE character_id=? AND ts>=? AND ts<=? AND way='death' AND pet=1",
                          (character_id, start_ms, end_ms)):
        died[(r["who"] or me, r["unit"])] = died.get((r["who"] or me, r["unit"]), 0) + 1
    out = []
    for (who, unit), p in found.items():
        if p["tries"] < 20:
            continue                                  # totems and the like
        between = sorted(_gaps(p["times"], 6000))
        seconds = len(p["active"]) * 5                # time it was actually fighting, in five-second blocks
        out.append({"who": who, "pet": unit, "damage": round(p["damage"]), "active_seconds": seconds,
                    "dps": round(p["damage"] / seconds, 1) if seconds else None,
                    "melee": _spread(p["swing"]), "skill_damage": round(p["skill"]),
                    "hit": _share(p["landed"], p["tries"]), "crit": _share(p["crits"], p["landed"]),
                    "swing_seconds": round(between[len(between) // 2] / 1000, 2) if between else None,
                    "taken": round(p["taken"]), "hits_taken": p["hit_by"], "deaths": died.get((who, unit), 0),
                    "dtps": round(p["taken"] / seconds, 1) if seconds else None})
    out.sort(key=lambda x: -x["damage"])
    return out


def autoshot(conn, character_id: str, start_ms: int, end_ms: int, me: str) -> List[Dict[str, Any]]:
    """The rhythm of automatic ranged shots. The usual gap between two shots is taken as the
    weapon's cycle; longer gaps inside a fight are shots that came late. An estimate."""
    where, args = _out(character_id, start_ms, end_ms)
    times: Dict[str, List[int]] = {}
    for r in conn.execute(f"SELECT who, ts FROM combat_hits WHERE {where} AND pet=0 AND kind='range' AND spell_id=75 ORDER BY ts", args):
        times.setdefault(r["who"] or me, []).append(r["ts"])
    out = []
    for who, t in times.items():
        raw = sorted(_gaps(t, 8000))
        if len(raw) < 20:
            continue
        cycle = raw[len(raw) // 5]                    # the short, undisturbed gaps
        kept = [g for g in raw if g <= cycle * 3]     # longer than that is a pause, not a late shot
        mean = sum(kept) / len(kept)
        late = sum(1 for g in kept if g > cycle * 1.2)
        out.append({"who": who, "shots": len(t), "cycle": round(cycle / 1000, 2), "average": round(mean / 1000, 2),
                    "median": round(kept[len(kept) // 2] / 1000, 2), "efficiency": round(cycle / mean, 3),
                    "late": _share(late, len(kept)), "lost_seconds": round(sum(g - cycle for g in kept if g > cycle) / 1000, 1),
                    "gaps": [round(g / 1000, 2) for g in kept]})
    return out


def _active(conn, character_id: str, start_ms: int, end_ms: int, me: str) -> Dict[str, Any]:
    """When each member (pet included) was actually attacking, in five-second blocks."""
    where, args = _out(character_id, start_ms, end_ms)
    blocks: Dict[str, set] = {}
    damage: Dict[str, float] = {}
    for r in conn.execute(f"SELECT who, ts, amount FROM combat_hits WHERE {where}", args):
        blocks.setdefault(r["who"] or me, set()).add(r["ts"] // 5000)
        damage[r["who"] or me] = damage.get(r["who"] or me, 0) + (r["amount"] or 0)
    return {"blocks": blocks, "damage": damage}


def activity(conn, character_id: str, start_ms: int, end_ms: int, me: str) -> List[Dict[str, Any]]:
    """How much of the group's fighting time each member spent attacking."""
    act = _active(conn, character_id, start_ms, end_ms, me)
    everyone = set().union(*act["blocks"].values()) if act["blocks"] else set()
    out = [{"who": w, "active_seconds": len(b) * 5, "share": round(len(b) / len(everyone), 3) if everyone else None,
            "damage": round(act["damage"][w]), "active_dps": round(act["damage"][w] / (len(b) * 5), 1)}
           for w, b in act["blocks"].items()]
    out.sort(key=lambda x: -x["damage"])
    return out


def casts(conn, character_id: str, start_ms: int, end_ms: int, me: str) -> List[Dict[str, Any]]:
    """What each ability cost and what it gave back in damage."""
    used: Dict[Any, Dict[str, Any]] = {}
    for r in conn.execute("SELECT who, pet, spell, kind, amount, raw, ts FROM combat_hits WHERE character_id=? AND ts>=? AND ts<=? "
                          "AND way='cast' ORDER BY ts", (character_id, start_ms, end_ms)):
        u = used.setdefault((r["who"] or me, r["spell"], r["pet"]), {"casts": 0, "cost": 0.0, "power": r["kind"],
                                                                  "took": []})
        u["casts"] += 1
        u["cost"] += (r["amount"] or 0) / (10 if r["kind"] == "1" else 1)      # rage is logged in tenths
        if r["raw"]:
            u["took"].append(r["raw"])
    dealt = {(k["who"], k["spell"], int(k["pet"])): k["total"] for k in skills(conn, character_id, start_ms, end_ms, me)}
    out = []
    for (who, spell, pet), u in used.items():
        damage = dealt.get((who, spell, pet), 0)
        if not damage:
            continue
        took = sum(u["took"]) / len(u["took"]) / 1000 if u["took"] else None
        out.append({"who": who, "spell": spell, "pet": bool(pet), "casts": u["casts"], "damage": damage,
                    "per_cast": round(damage / u["casts"], 1), "power": POWER.get(u["power"], "資源"),
                    "cost": round(u["cost"] / u["casts"], 1) if u["cost"] else 0,
                    "per_point": round(damage / u["cost"], 2) if u["cost"] else None,
                    "cast_seconds": round(took, 2) if took else None,
                    "per_cast_second": round(damage / (took * len(u["took"])), 1) if took else None})
    out.sort(key=lambda x: -x["damage"])
    return out


def dots(conn, character_id: str, start_ms: int, end_ms: int, me: str) -> List[Dict[str, Any]]:
    """Effects the group put on enemies: how much of each enemy's life they were up, and what
    the ones that tick did."""
    where, args = _out(character_id, start_ms, end_ms)
    life: Dict[str, List[int]] = {}
    ticks: Dict[Any, List[float]] = {}
    for r in conn.execute(f"SELECT who, pet, spell, kind, amount, guid, ts, miss FROM combat_hits WHERE {where}", args):
        if r["guid"]:
            span = life.setdefault(r["guid"], [r["ts"], r["ts"]])
            span[0], span[1] = min(span[0], r["ts"]), max(span[1], r["ts"])
        if r["kind"] == "dot" and not r["miss"]:
            t = ticks.setdefault((r["who"] or me, r["spell"], r["pet"]), [0, 0.0])
            t[0] += 1
            t[1] += r["amount"] or 0
    found: Dict[Any, Dict[str, Any]] = {}
    for r in conn.execute("SELECT who, pet, spell, kind, guid, ts FROM combat_hits WHERE character_id=? AND ts>=? AND ts<=? "
                          "AND way='aura' AND miss='DEBUFF' AND enemy IS NOT NULL ORDER BY ts, pos",
                          (character_id, start_ms, end_ms)):
        e = found.setdefault((r["who"] or me, r["spell"], r["pet"]), {"open": {}, "up": 0, "applied": 0, "targets": set()})
        if r["kind"] == "REMOVED":
            if r["guid"] in e["open"]:
                e["up"] += r["ts"] - e["open"].pop(r["guid"])
            continue
        e["applied"] += 1
        e["targets"].add(r["guid"])
        e["open"].setdefault(r["guid"], r["ts"])
    out = []
    for (who, spell, pet), e in found.items():
        for guid, since in e["open"].items():          # never seen to end: counted until the target was last hit
            e["up"] += max(0, life.get(guid, [since, since])[1] - since)
        lived = sum(max(1000, life[g][1] - life[g][0]) for g in e["targets"] if g in life)
        tick = ticks.get((who, spell, pet))
        out.append({"who": who, "spell": spell, "pet": bool(pet), "applied": e["applied"], "targets": len(e["targets"]),
                    "up_seconds": round(e["up"] / 1000), "coverage": round(min(1.0, e["up"] / lived), 3) if lived else None,
                    "ticks": tick[0] if tick else 0, "tick_damage": round(tick[1]) if tick else 0,
                    "per_application": round(tick[1] / e["applied"], 1) if tick and e["applied"] else None})
    out.sort(key=lambda x: (-x["tick_damage"], -x["applied"]))
    return out


def buffs(conn, character_id: str, start_ms: int, end_ms: int, me: str) -> List[Dict[str, Any]]:
    """Effects on each member and how much of the logged time they were up."""
    seen: Dict[str, List[int]] = {}
    for r in conn.execute("SELECT who, MIN(ts) a, MAX(ts) b FROM combat_hits WHERE character_id=? AND ts>=? AND ts<=? "
                          "GROUP BY who", (character_id, start_ms, end_ms)):
        seen[r["who"] or me] = [r["a"], r["b"]]
    found: Dict[Any, Dict[str, Any]] = {}
    for r in conn.execute("SELECT who, spell, kind, unit, ts FROM combat_hits WHERE character_id=? AND ts>=? AND ts<=? "
                          "AND way='aura' AND miss='BUFF' AND enemy IS NULL ORDER BY ts, pos", (character_id, start_ms, end_ms)):
        who = r["who"] or me
        e = found.setdefault((who, r["spell"]), {"since": None, "up": 0, "applied": 0, "by": {}})
        if r["kind"] == "REMOVED":
            if e["since"] is not None:
                e["up"] += r["ts"] - e["since"]
                e["since"] = None
            continue
        e["applied"] += 1
        caster = (r["unit"] or "").split("-")[0]
        e["by"][caster] = e["by"].get(caster, 0) + 1
        if e["since"] is None:
            e["since"] = r["ts"]
    out: Dict[str, List[Dict[str, Any]]] = {}
    for (who, spell), e in found.items():
        first, last = seen.get(who, [start_ms, end_ms])
        if e["since"] is not None:
            e["up"] += max(0, last - e["since"])
        span = max(1000, last - first)
        out.setdefault(who, []).append({"spell": spell, "applied": e["applied"], "up_seconds": round(e["up"] / 1000),
                                        "uptime": round(min(1.0, e["up"] / span), 3),
                                        "by": max(e["by"], key=e["by"].get) if e["by"] else None})
    return [{"who": w, "buffs": sorted(b, key=lambda x: -x["up_seconds"])[:20]} for w, b in out.items()]


def guard(conn, character_id: str, start_ms: int, end_ms: int, me: str) -> List[Dict[str, Any]]:
    """What happened to attacks aimed at each member."""
    found: Dict[str, Dict[str, Any]] = {}
    for r in conn.execute("SELECT who, kind, amount, crit, crushing, blocked, miss FROM combat_hits WHERE character_id=? "
                          "AND ts>=? AND ts<=? AND way='in' AND pet=0", (character_id, start_ms, end_ms)):
        g = found.setdefault(r["who"] or me, {"n": 0, "counts": {}, "damage": 0.0, "landed": 0, "blocked": 0, "melee": 0})
        g["n"] += 1
        key = r["miss"] or ("crushing" if r["crushing"] else "crit" if r["crit"] else "normal")
        g["counts"][key] = g["counts"].get(key, 0) + 1
        g["melee"] += 1 if r["kind"] == "swing" else 0
        if not r["miss"]:
            g["landed"] += 1
            g["damage"] += r["amount"] or 0
            g["blocked"] += 1 if (r["blocked"] or 0) > 0 else 0
    out = []
    for who, g in found.items():
        c, n = g["counts"], g["n"]
        out.append({"who": who, "attacks": n, "damage": round(g["damage"]),
                    "avg_hit": round(g["damage"] / g["landed"], 1) if g["landed"] else None,
                    "hit": _share(g["landed"], n), "miss": _share(c.get("MISS", 0), n), "dodge": _share(c.get("DODGE", 0), n),
                    "parry": _share(c.get("PARRY", 0), n), "block": _share(g["blocked"] + c.get("BLOCK", 0), n),
                    "resist": _share(c.get("RESIST", 0), n), "crit": _share(c.get("crit", 0), g["landed"]),
                    "crushing": _share(c.get("crushing", 0), g["landed"])})
    out.sort(key=lambda x: -x["attacks"])
    return out


def heals(conn, character_id: str, start_ms: int, end_ms: int, me: str) -> List[Dict[str, Any]]:
    """Each healing ability: what landed, what was wasted on full health, how often it crit."""
    found: Dict[Any, Dict[str, Any]] = {}
    for r in conn.execute("SELECT who, spell, kind, amount, raw, crit FROM combat_hits WHERE character_id=? AND ts>=? AND ts<=? "
                          "AND way='heal'", (character_id, start_ms, end_ms)):
        h = found.setdefault((r["who"] or me, r["spell"]), {"n": 0, "eff": 0.0, "gross": 0.0, "crits": 0, "max": 0.0,
                                                           "tick": r["kind"] == "hot"})
        h["n"] += 1
        h["eff"] += r["amount"] or 0
        h["gross"] += r["raw"] or r["amount"] or 0
        h["crits"] += r["crit"] or 0
        h["max"] = max(h["max"], r["amount"] or 0)
    out = [{"who": w, "spell": sp, "periodic": h["tick"], "count": h["n"], "healing": round(h["eff"]),
            "avg": round(h["eff"] / h["n"], 1), "max": round(h["max"]),
            "overheal": round(1 - h["eff"] / h["gross"], 3) if h["gross"] > 0 else None,
            "crit": _share(h["crits"], h["n"]) if not h["tick"] else None}
           for (w, sp), h in found.items() if h["eff"] > 0 or h["gross"] > 0]
    out.sort(key=lambda x: -x["healing"])
    return out


def register(app, db, can_write, claim=lambda writer, character_id: None) -> None:
    with db() as conn:
        conn.executescript(SCHEMA)
        if "guid" not in [r["name"] for r in conn.execute("PRAGMA table_info(combat_hits)")]:
            conn.execute("ALTER TABLE combat_hits ADD COLUMN guid TEXT")      # tables made before it existed

    @app.post("/api/v1/combat/hits")
    def post_hits(batch: HitsIn, writer: Optional[str] = Depends(can_write)):
        """Rows are identified by where they sit in their log file: sending one again replaces it."""
        claim(writer, batch.character_id)
        clean = []
        for row in batch.rows:
            if len(row) == len(COLUMNS) - 1:
                row = row + [None]                   # an agent from before the last column was added
            if len(row) != len(COLUMNS) or not isinstance(row[0], int) or not isinstance(row[1], int) \
                    or row[2] not in ("out", "in", "heal", "cast", "aura", "death") or not isinstance(row[3], str):
                raise HTTPException(422, "bad row")
            clean.append((batch.source, row[0], batch.character_id, *row[1:]))
        with db() as conn:
            before = conn.total_changes
            conn.executemany("INSERT OR REPLACE INTO combat_hits (source, pos, character_id, %s) VALUES (%s)"
                             % (", ".join(COLUMNS[1:]), ",".join("?" * (len(COLUMNS) + 2))), clean)
            return {"success": True, "accepted": len(clean), "changes": conn.total_changes - before}
