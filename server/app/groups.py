"""Group sessions: one party or raid from joining to leaving, with its own share page.

A session is derived from the GROUP_JOINED / GROUP_LEFT events the addon records. Each one
gets a short random token; `/party/<token>` shows that session alone (who was there, what
was killed and looted, the route) to anyone holding the link. The link exposes nothing
outside the session's time window and never any chat.
"""
import json
import os
import secrets
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from fastapi import Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response

from . import combat

ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
# Older addon versions recorded every roll for group loot as a pick-up, named after the roll
# window's link instead of the item. Those records are left out wherever loot is counted.
ROLL_LINES = ("戰利品", "Loot")
HIDDEN = ("LOCATION_SAMPLE", "CHAT_MESSAGE", "MAP_SCREENSHOT", "PARTY_POSITIONS", "GROUP_METER", "PARTY_LOOT",
          "GROUP_ENCOUNTER", "THREAT", "TRADE_COMPLETED")
HIGHLIGHTS = ("LEVEL_UP", "ACHIEVEMENT_EARNED", "PLAYER_DIED", "PARTY_MEMBER_DIED", "GROUP_MEMBER_LEVEL_UP",
              "INSTANCE_ENTERED", "ENCOUNTER_END")
SOLO_GAP = 20 * 60 * 1000          # ms without a hit after which a stretch of fighting alone is over
SOLO_SHORTEST = 60 * 1000          # shorter than this is not worth a page
RANK_NAMES = {"elite": "精英", "rare_elite": "稀有精英", "boss": "首領", "rare": "稀有"}


def _dt(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _ms(ts: str) -> int:
    return int(_dt(ts).timestamp() * 1000)


def _amounts(table: Any) -> List[Any]:
    """(name, number) pairs of a table that came from outside."""
    if not isinstance(table, dict):
        return []
    return [(k, v) for k, v in table.items() if isinstance(v, (int, float)) and not isinstance(v, bool)]


def register(app, db, can_read, activity: Callable, maps_dir: str, world=None, shots=None) -> None:
    with db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS group_shares (group_id TEXT PRIMARY KEY, "
                     "character_id TEXT NOT NULL, token TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL)")

    def sessions(conn, character_id: str) -> List[Dict[str, Any]]:
        rows = conn.execute(
            "SELECT event_type, timestamp, payload_json FROM events WHERE character_id=? AND "
            "event_type IN ('GROUP_JOINED','GROUP_MEMBER_JOINED','GROUP_MEMBER_LEFT','GROUP_MEMBER_LEVEL_UP',"
            "'GROUP_LEFT') "
            "ORDER BY timestamp, event_id", (character_id,)).fetchall()
        last = conn.execute("SELECT MAX(timestamp) t FROM events WHERE character_id=?",
                            (character_id,)).fetchone()["t"]
        out: List[Dict[str, Any]] = []
        by_id: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            p = json.loads(r["payload_json"])
            gid = p.get("group_id")
            if not isinstance(gid, str):
                continue
            if r["event_type"] == "GROUP_JOINED":
                if gid in by_id:
                    continue
                s = {"group_id": gid, "character_id": character_id, "started_at": r["timestamp"],
                     "ended_at": None, "ongoing": True, "raid": bool(p.get("raid")),
                     "leader": bool(p.get("leader")), "zone_name": p.get("zone_name"),
                     "members": {}}
                for m in p.get("members") or []:
                    if isinstance(m, dict) and m.get("name"):
                        s["members"][m["name"]] = {"name": m["name"], "class": m.get("class"),
                                                   "level": m.get("level"), "left": False}
                by_id[gid] = s
                out.append(s)
                continue
            s = by_id.get(gid)
            if not s:
                continue
            if r["event_type"] == "GROUP_MEMBER_JOINED" and p.get("name"):
                # also sent again when a far-away member's level becomes known
                s["members"][p["name"]] = {"name": p["name"], "class": p.get("class"),
                                           "level": p.get("level"), "left": False}
            elif r["event_type"] == "GROUP_MEMBER_LEFT" and p.get("name") in s["members"]:
                s["members"][p["name"]]["left"] = True
            elif r["event_type"] == "GROUP_MEMBER_LEVEL_UP" and p.get("name") in s["members"]:
                s["members"][p["name"]]["level"] = p.get("level")      # show the level they ended on
            elif r["event_type"] == "GROUP_LEFT":
                s["ended_at"], s["ongoing"] = r["timestamp"], False
        for k, s in enumerate(out):
            if s["ongoing"]:
                # No end recorded: it runs until the next session starts, or the newest data.
                nxt = out[k + 1]["started_at"] if k + 1 < len(out) else None
                s["ended_at"] = nxt or last or s["started_at"]
                s["ongoing"] = nxt is None
            s["members"] = list(s["members"].values())
            s["duration"] = int((_dt(s["ended_at"]) - _dt(s["started_at"])).total_seconds())
        return out

    def solo_sessions(conn, character_id: str, groups: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        """Stretches of fighting outside any group, oldest first, shaped like group sessions.

        They come from the combat log's hits, dealt and taken (a buff going up alone is no
        fight): a stretch runs for as long as no more than
        SOLO_GAP passes between two hits, and whatever of it lies inside a group session is
        cut away. One is named after the moment it starts, so it keeps its name (and its
        link) while it grows."""
        edges = conn.execute(
            "SELECT ts, prev, nxt FROM (SELECT ts, LAG(ts) OVER (ORDER BY ts) prev, LEAD(ts) OVER (ORDER BY ts) nxt "
            "FROM combat_hits WHERE character_id=? AND way IN ('out','in')) WHERE prev IS NULL OR nxt IS NULL OR ts-prev>? OR nxt-ts>?",
            (character_id, SOLO_GAP, SOLO_GAP)).fetchall()
        spans, start = [], None
        for e in edges:
            if e["prev"] is None or e["ts"] - e["prev"] > SOLO_GAP:
                start = e["ts"]
            if e["nxt"] is None or e["nxt"] - e["ts"] > SOLO_GAP:
                spans.append([start if start is not None else e["ts"], e["ts"]])
                start = None
        busy = sorted((_ms(g["started_at"]), _ms(g["ended_at"]) + 999)
                      for g in (groups if groups is not None else sessions(conn, character_id)))
        newest = spans[-1][1] if spans else 0
        iso = lambda ms: datetime.fromtimestamp(ms // 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")   # noqa: E731

        def worth(a: int, b: int) -> bool:
            """Long enough for a page, or short but something was killed in it (seen in the
            game: two quick kills, fifty seconds in all, and no page to look at them on)."""
            if b - a >= SOLO_SHORTEST:
                return True
            return conn.execute("SELECT 1 FROM events WHERE character_id=? AND event_type='MOB_KILLED' "
                                "AND timestamp BETWEEN ? AND ? LIMIT 1",
                                (character_id, iso(a), iso(b + 5000))).fetchone() is not None

        out: List[Dict[str, Any]] = []
        for a, b in spans:
            for ga, gb in busy:                     # keep what is before a group session, go on after it
                if gb < a or ga > b:
                    continue
                if ga - 1000 >= a and worth(a, ga - 1000):
                    out.append((a, ga - 1000))
                a = gb + 1
            if b >= a and worth(a, b):
                out.append((a, b))
        found = []
        for a, b in out:
            started = datetime.fromtimestamp(a // 1000, timezone.utc)
            ended = datetime.fromtimestamp(-(-b // 1000), timezone.utc)
            found.append({"group_id": "solo-%d" % (a // 1000), "character_id": character_id, "solo": True,
                          "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"), "ended_at": ended.strftime("%Y-%m-%dT%H:%M:%SZ"),
                          # still going while the newest hit is its last one and that was a moment ago
                          "ongoing": b == newest and datetime.now(timezone.utc).timestamp() * 1000 - b < SOLO_GAP,
                          "raid": False, "leader": False, "zone_name": None, "members": [],
                          "duration": int((ended - started).total_seconds())})
        return found

    def every(conn, character_id: str) -> List[Dict[str, Any]]:
        """Group sessions and stretches alone."""
        groups = sessions(conn, character_id)
        return groups + solo_sessions(conn, character_id, groups)

    def token_for(conn, s: Dict[str, Any]) -> str:
        row = conn.execute("SELECT token FROM group_shares WHERE group_id=?", (s["group_id"],)).fetchone()
        if row:
            return row["token"]
        token = "".join(secrets.choice(ALPHABET) for _ in range(12))
        conn.execute("INSERT INTO group_shares VALUES (?,?,?,?)",
                     (s["group_id"], s["character_id"], token,
                      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
        return token

    def detail(conn, s: Dict[str, Any]) -> Dict[str, Any]:
        """Everything that happened between the session's start and end."""
        cid = s["character_id"]
        act = activity(cid, _dt(s["started_at"]), _dt(s["ended_at"]))
        rows = conn.execute(
            "SELECT event_type, timestamp, payload_json FROM events WHERE character_id=? AND "
            "timestamp>=? AND timestamp<=? ORDER BY timestamp, event_id",
            (cid, s["started_at"], s["ended_at"])).fetchall()
        who = conn.execute("SELECT name, full_name, class, level, race FROM characters WHERE id=?", (cid,)).fetchone()
        me = (who["name"] if who else None) or "我"
        kills_by: Counter = Counter()
        kills_of: Dict[str, Counter] = {}             # member -> what they landed the kill on
        monster_ids: Dict[str, int] = {}
        loot_of: Dict[str, Dict[Any, Dict[str, Any]]] = {}
        deaths_of: Counter = Counter()
        damage: Counter = Counter()
        healing: Counter = Counter()
        taken: Counter = Counter()
        spells: Dict[str, Counter] = {}               # member -> damage by ability
        best: Dict[str, List[Any]] = {}               # member -> hardest hit
        fight_seconds = 0
        hits: Dict[str, List[int]] = {}               # member -> hits landed, critical ones
        control: Dict[str, List[int]] = {}            # member -> dispels, interrupts
        overheal: Counter = Counter()
        hurt: Counter = Counter()                     # "enemy｜ability" -> damage done to the group
        kill_times: List[str] = []
        minutes: List[Any] = []                       # [time, {member: damage that minute}]
        encounters: List[Dict[str, Any]] = []
        member_route: Dict[int, Dict[str, List[List[Any]]]] = {}
        quests: List[Dict[str, Any]] = []
        skills: Dict[str, str] = {}
        highlights: List[Dict[str, Any]] = []
        counts: Counter = Counter()
        zones: List[str] = []
        instances: List[str] = []
        route: Dict[int, List[List[Any]]] = {}
        map_names: Dict[int, Counter] = {}
        marks: List[Dict[str, Any]] = []
        timeline: List[Dict[str, Any]] = []
        for r in rows:
            p = json.loads(r["payload_json"])
            kind = r["event_type"]
            counts[kind] += 1
            zone = p.get("zone_name")
            if zone and (not zones or zones[-1] != zone) and zone not in zones:
                zones.append(zone)
            has_pos = isinstance(p.get("map_id"), int) and isinstance(p.get("x"), (int, float)) \
                and isinstance(p.get("y"), (int, float))
            if kind == "LOCATION_SAMPLE":
                if has_pos:
                    route.setdefault(p["map_id"], []).append([r["timestamp"], p["x"], p["y"]])
                    if zone:
                        map_names.setdefault(p["map_id"], Counter())[zone] += 1
                continue
            if kind == "MOB_KILLED":
                kill_times.append(r["timestamp"])
                who_killed = p.get("killed_by") or me
                kills_by[who_killed] += 1
                label = p.get("monster_name") or f"#{p.get('monster_id')}"
                kills_of.setdefault(who_killed, Counter())[label] += 1
                if isinstance(p.get("monster_id"), int):
                    monster_ids[label] = p["monster_id"]
            elif kind == "PARTY_LOOT" and p.get("looter"):
                if p.get("how") in ("created", "received") or p.get("item_name") in ROLL_LINES:
                    continue                      # crafted, bought or handed over: not loot
                bag = loot_of.setdefault(p["looter"], {})
                it = bag.setdefault(p.get("item_id") or p.get("item_name"),
                                    {"id": p.get("item_id"), "name": p.get("item_name"), "count": 0, "value": 0})
                n = p.get("count") or 1
                it["count"] += n
                it["value"] += (p.get("sell_price") or 0) * n
            elif kind == "PARTY_MEMBER_DIED" and p.get("name"):
                deaths_of[p["name"]] += 1
            elif kind == "PLAYER_DIED":
                deaths_of[me] += 1
            elif kind == "GROUP_METER":
                for table, total in ((p.get("damage"), damage), (p.get("healing"), healing), (p.get("taken"), taken)):
                    for name, amount in _amounts(table):
                        total[name or me] += amount           # "" stands for the recorder
                if _amounts(p.get("damage")):
                    # a minute can come in two parts (the game wrote the rest of it late): one point
                    if minutes and minutes[-1][0] == r["timestamp"]:
                        for name, a in _amounts(p.get("damage")):
                            minutes[-1][1][name or me] = minutes[-1][1].get(name or me, 0) + a
                    else:
                        minutes.append([r["timestamp"], {name or me: a for name, a in _amounts(p.get("damage"))}])
                for name, table in (p.get("spells") or {}).items() if isinstance(p.get("spells"), dict) else []:
                    for spell, amount in _amounts(table):
                        spells.setdefault(name or me, Counter())[spell] += amount
                for name, hit in (p.get("best") or {}).items() if isinstance(p.get("best"), dict) else []:
                    if isinstance(hit, list) and len(hit) >= 4 and isinstance(hit[0], (int, float)) \
                            and hit[0] > best.get(name or me, [0])[0]:
                        best[name or me] = hit
                if isinstance(p.get("fight"), (int, float)):
                    fight_seconds += p["fight"]
                for name, amount in _amounts(p.get("over")):
                    overheal[name or me] += amount
                for name, amount in _amounts(p.get("hurt")):
                    hurt[name] += amount
                for key, total in (("hits", hits), ("cc", control)):
                    for name, pair in (p.get(key) or {}).items() if isinstance(p.get(key), dict) else []:
                        if isinstance(pair, list) and len(pair) == 2 and all(isinstance(x, int) for x in pair):
                            mine = total.setdefault(name or me, [0, 0])
                            mine[0] += pair[0]
                            mine[1] += pair[1]
            elif kind == "GROUP_ENCOUNTER":
                encounters.append({
                    "name": p.get("name"), "success": bool(p.get("success")), "timestamp": r["timestamp"],
                    "duration": p.get("duration"),
                    **{key: [{"name": name or me, "amount": int(a)} for name, a in
                             sorted(_amounts(p.get(key)), key=lambda x: -x[1])]
                       for key in ("damage", "healing", "taken")}})
            elif kind == "PARTY_POSITIONS" and isinstance(p.get("map_id"), int) and isinstance(p.get("members"), dict):
                for name, xy in p["members"].items():
                    if isinstance(xy, dict) and isinstance(xy.get("x"), (int, float)):
                        member_route.setdefault(p["map_id"], {}).setdefault(name, []).append(
                            [r["timestamp"], xy["x"], xy.get("y")])
            elif kind == "QUEST_COMPLETED":
                quests.append({"name": p.get("quest_name") or f"任務 #{p.get('quest_id')}",
                               "xp": p.get("xp_reward"), "money": p.get("money_reward")})
            elif kind == "SKILL_UP" and p.get("text"):
                # one line per skill: the text without its number identifies the skill
                skills["".join(ch for ch in p["text"] if not ch.isdigit())] = p["text"]
            if kind in HIGHLIGHTS:
                highlights.append({"type": kind, "timestamp": r["timestamp"],
                                   **{k: p[k] for k in ("name", "level", "zone_name", "success") if k in p}})
            if kind == "INSTANCE_ENTERED" and p.get("name") and p["name"] not in instances:
                instances.append(p["name"])
            if has_pos and kind in ("MOB_KILLED", "PLAYER_DIED", "QUEST_COMPLETED"):
                marks.append({"type": kind, "map_id": p["map_id"], "x": p["x"], "y": p["y"], "t": r["timestamp"]})
            if kind in HIDDEN or kind in ("XP_GAINED", "COMBAT", "MONEY_CHANGED") \
                    or (kind == "ITEM_LOOTED" and p.get("item_name") in ROLL_LINES):
                continue
            if kind == "GROUP_MEMBER_JOINED" and p.get("update"):
                continue                          # only fills in a level; nobody new joined
            keep = {k: p[k] for k in ("monster_name", "killed_by", "item_name", "count", "quest_name", "level",
                                      "name", "zone_name", "text", "success", "sell_price") if k in p}
            timeline.append({"type": kind, "timestamp": r["timestamp"], **keep})
        # the map where most of the session happened comes first; sub-zone names share a map,
        # so the name seen most often is the zone itself
        maps = [{"map_id": m, "name": map_names[m].most_common(1)[0][0] if m in map_names else None,
                 "points": pts, "members": member_route.get(m, {}),
                 "has_image": os.path.isfile(os.path.join(maps_dir, f"{m}.jpg"))}
                for m, pts in sorted(route.items(), key=lambda kv: -len(kv[1]))]
        # Level and rank of what was fought, from the world database when it is installed.
        monsters = []
        for mo in act["top_monsters"]:
            row = {"name": mo.get("name") or f"#{mo.get('id')}", "count": mo["count"], "level": None, "rank": None}
            info = world.npc_info(mo["id"]) if world and isinstance(mo.get("id"), int) else None
            if info:
                row.update(level=info["level"], rank=RANK_NAMES.get(info["rank"]))
                if row["rank"]:
                    highlights.append({"type": "RARE_KILL", "timestamp": None, "name": row["name"],
                                       "rank": row["rank"], "count": mo["count"]})
            monsters.append(row)
        hours = max(s["duration"], 1) / 3600
        out = {k: s[k] for k in ("group_id", "started_at", "ended_at", "ongoing", "duration", "raid",
                                 "leader", "members")}
        out["solo"] = bool(s.get("solo"))
        out.update({
            "owner": {"name": me, "class": who["class"] if who else None, "level": who["level"] if who else None},
            "zones": zones, "instances": instances,
            "stats": {k: act[k] for k in ("kills", "xp", "items_looted", "loot_value", "fights", "combat_seconds",
                                          "money_in", "money_out")},
            "deaths": counts["PLAYER_DIED"], "quests_completed": counts["QUEST_COMPLETED"],
            "level_ups": counts["LEVEL_UP"],
            "kills_by": [{"name": n, "kills": c, "monsters": [{"name": k, "count": v} for k, v in
                                                          kills_of[n].most_common(6)]}
                         for n, c in kills_by.most_common()],
            "top_monsters": act["top_monsters"], "top_items": act["top_items"], "monsters": monsters,
            "xp_points": act["xp_points"],
            "pace": {"kills_per_hour": round(act["kills"] / hours, 1), "xp_per_hour": int(act["xp"] / hours),
                     "avg_fight_seconds": round(act["combat_seconds"] / act["fights"], 1) if act["fights"] else None,
                     "combat_share": round(min(1, act["combat_seconds"] / max(s["duration"], 1)), 3)},
            "quests": quests, "skill_ups": list(skills.values()), "highlights": highlights,
            "member_loot": [{"name": n, "items": sorted(b.values(), key=lambda i: (-i["value"], -i["count"]))[:12],
                             "value": sum(i["value"] for i in b.values()),
                             "count": sum(i["count"] for i in b.values())} for n, b in loot_of.items()],
            "deaths_by": [{"name": n, "deaths": c} for n, c in deaths_of.most_common()],
            "screenshots": shots.between(conn, cid, s["started_at"], s["ended_at"]) if shots else [],
            "damage": [{"name": n, "amount": int(v)} for n, v in damage.most_common()],
            "healing": [{"name": n, "amount": int(v)} for n, v in healing.most_common()],
            # Read from the game's combat log file, when the recorder had it switched on.
            "meter": {"fight_seconds": int(fight_seconds), "members": [
                {"name": n, "damage": int(damage[n]), "healing": int(healing[n]), "taken": int(taken[n]),
                 "spells": [{"name": k, "amount": int(v)} for k, v in spells.get(n, Counter()).most_common(6)],
                 "best": dict(zip(("amount", "spell", "target", "crit"), best[n])) if n in best else None,
                 "hits": hits.get(n, [0, 0])[0], "crits": hits.get(n, [0, 0])[1], "overheal": int(overheal[n]),
                 "dispels": control.get(n, [0, 0])[0], "interrupts": control.get(n, [0, 0])[1]}
                for n in sorted(set(damage) | set(healing) | set(taken) | set(control), key=lambda n: -damage[n])],
                "threats": [dict(zip(("enemy", "spell"), k.split("｜", 1)), amount=int(v))
                            for k, v in hurt.most_common(8) if "｜" in k]},
            "kill_times": kill_times,
            "skills": combat.skills(conn, cid, _ms(s["started_at"]), _ms(s["ended_at"]) + 999, me),
            "damage_minutes": minutes, "encounters": encounters,
            "maps": maps, "marks": marks[:1500], "timeline": timeline[-400:],
        })
        out["owner"]["full_name"] = who["full_name"] if who else None
        seen = {me, *(m["name"] for m in out["members"]), *damage, *healing, *taken, *kills_by, *loot_of, *deaths_of,
                *(n for m in member_route.values() for n in m)}
        out["full_names"] = shown_names(conn, cid, seen, out["owner"])
        return out

    def shown_names(conn, cid: str, seen, owner: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        """{short name: name as the game shows it} for the names on a page. The page keeps every
        short name as it is (the data is keyed by it) and shows this one instead where it has one."""
        found = combat.names(conn, cid)
        if owner and owner.get("full_name") and owner.get("name"):
            found[owner["name"]] = owner["full_name"]
        return {k: v for k, v in found.items() if k in seen and v != k}

    def brief(conn, s: Dict[str, Any]) -> Dict[str, Any]:
        act = activity(s["character_id"], _dt(s["started_at"]), _dt(s["ended_at"]))
        out = {**{k: s[k] for k in ("group_id", "started_at", "ended_at", "ongoing", "duration", "raid",
                                    "zone_name", "members")},
               "kills": act["kills"], "xp": act["xp"], "items_looted": act["items_looted"],
               "token": token_for(conn, s),
               "full_names": shown_names(conn, s["character_id"], {m["name"] for m in s["members"]})}
        if s.get("solo"):
            # where it was, and how much was dealt: what tells one stretch alone from another
            a, b = _ms(s["started_at"]), _ms(s["ended_at"]) + 999
            zone = conn.execute("SELECT payload_json FROM events WHERE character_id=? AND timestamp>=? AND timestamp<=? "
                                "AND payload_json LIKE '%zone_name%' ORDER BY timestamp LIMIT 1",
                                (s["character_id"], s["started_at"], s["ended_at"])).fetchone()
            dealt = conn.execute("SELECT COALESCE(SUM(amount),0) n FROM combat_hits WHERE character_id=? AND ts>=? AND ts<=? "
                                 "AND way='out'", (s["character_id"], a, b)).fetchone()["n"]
            out.update(solo=True, zone_name=json.loads(zone["payload_json"]).get("zone_name") if zone else None,
                       damage=int(dealt))
        return out

    @app.get("/api/v1/groups")
    def list_groups(character_id: str, scope: Optional[str] = Depends(can_read)):
        """Group sessions of a character, newest first, each with its share token."""
        if scope is not None and character_id != scope:
            raise HTTPException(403, "this link only covers one character")
        with db() as conn:
            items = [brief(conn, s) for s in sessions(conn, character_id)]
        return {"items": items[::-1]}

    # ------------------------------------------------ for tools: the same data, asked for by character
    # The share pages above are addressed by a session's own token. These are addressed by
    # character and need a read token, and answer in pieces small enough for a program (an MCP
    # server, a script) to hand to a language model.
    HEAVY = {"route": ("maps", "marks"), "timeline": ("timeline",), "skills": ("skills",),
             "charts": ("xp_points", "damage_minutes", "kill_times"), "screenshots": ("screenshots",)}

    def with_rates(d: Dict[str, Any]) -> Dict[str, Any]:
        """Per-second figures and shares worked out, so a reader does not have to divide."""
        meter = d.get("meter") or {}
        fight = max(1, meter.get("fight_seconds") or 0)
        total = sum(m["damage"] for m in meter.get("members", [])) or 0
        if meter.get("members"):
            meter["group_dps"] = round(total / fight, 1)
        for m in meter.get("members", []):
            m.update(dps=round(m["damage"] / fight, 1), hps=round(m["healing"] / fight, 1), dtps=round(m["taken"] / fight, 1),
                     damage_pct=round(m["damage"] / total * 100, 1) if total else None,
                     crit_pct=round(m["crits"] / m["hits"] * 100, 1) if m["hits"] else None)
        for e in d.get("encounters", []):
            seconds = max(1, e.get("duration") or 0)
            dealt = sum(r["amount"] for r in e["damage"])
            e["group_dps"] = round(dealt / seconds, 1)
            for r in e["damage"]:
                r.update(dps=round(r["amount"] / seconds, 1), pct=round(r["amount"] / dealt * 100, 1) if dealt else None)
        return d
    PARTS = ("defence", "level_gaps", "dots", "guard", "buffs", "debuffs", "heals", "activity", "pets", "autoshot", "casts")

    def mine(scope: Optional[str], character_id: str) -> None:
        if scope is not None and character_id != scope:
            raise HTTPException(403, "this token does not cover that character")

    def find(conn, character_id: str, group_id: str) -> Dict[str, Any]:
        found = sessions(conn, character_id)
        if group_id == "latest" and found:
            return found[-1]
        if group_id.startswith("solo"):
            found = solo_sessions(conn, character_id, found)
            if group_id == "solo-latest" and found:
                return found[-1]
        for s in found:
            if s["group_id"] == group_id:
                return s
        raise HTTPException(404, "no such group session")

    def owner(conn, character_id: str) -> Any:
        return conn.execute("SELECT name, level FROM characters WHERE id=?", (character_id,)).fetchone()

    def analysis(conn, character_id: str, a: int, b: int, levels: Dict[str, int], parts: Optional[str],
                 limit: int) -> Dict[str, Any]:
        who = owner(conn, character_id)
        me = (who["name"] if who else None) or "我"
        if who and who["level"]:
            levels = {**levels, me: who["level"]}
        wanted = [p.strip() for p in parts.split(",") if p.strip()] if parts else list(PARTS)
        unknown = [p for p in wanted if p not in PARTS]
        if unknown:
            raise HTTPException(400, f"unknown part {unknown[0]!r}; the parts are: {', '.join(PARTS)}")
        make = {"defence": lambda: combat.defence(conn, character_id, a, b, me, world.npc_info if world else None),
                "level_gaps": lambda: combat.level_gaps(conn, character_id, a, b, me, levels),
                "autoshot": lambda: [{k: v for k, v in x.items() if k != "gaps"}
                                     for x in combat.autoshot(conn, character_id, a, b, me)]}
        out: Dict[str, Any] = {}
        for part in wanted:
            rows = make[part]() if part in make else getattr(combat, part)(conn, character_id, a, b, me)
            out[part] = rows[:limit]
        return out

    def chosen(rows: List[Dict[str, Any]], member: Optional[str], spell: Optional[str], limit: int) -> List[Dict[str, Any]]:
        return [k for k in rows if (not member or k["who"] == member) and (not spell or k["spell"] == spell)][:limit]

    @app.get("/api/v1/characters/{character_id}/groups")
    def character_groups(character_id: str, limit: int = Query(20, ge=1, le=200), scope: Optional[str] = Depends(can_read)):
        """The character's group (party or raid) sessions, newest first.

        Each has its `group_id`, when it ran, who was in it, where, and totals for kills, the
        recorder's experience and loot. `page` is the address of the session's public page,
        which anyone can open without a token. Use `group_id` (or the word `latest`) with the
        other `/groups/...` endpoints."""
        mine(scope, character_id)
        with db() as conn:
            items = [brief(conn, s) for s in sessions(conn, character_id)][::-1][:limit]
        for item in items:
            item["page"] = f"/party/{item['token']}"
        return {"items": items}

    @app.get("/api/v1/characters/{character_id}/solo")
    def character_solo(character_id: str, limit: int = Query(20, ge=1, le=200), scope: Optional[str] = Depends(can_read)):
        """The character's stretches of fighting alone, newest first (needs the combat log).

        Everything fought outside a group, cut into stretches wherever twenty minutes pass
        without a hit. Each has a `group_id` that starts with `solo-`, when it ran, where, and
        totals for kills, experience, loot and damage dealt. That `group_id` (or `solo-latest`)
        works with every `/groups/{group_id}...` endpoint: the session in full, `/fight`,
        `/skills` and `/analysis` answer for a stretch alone exactly as for a group session,
        with the character (and pet) as the only member. `page` is its page, as for a group."""
        mine(scope, character_id)
        with db() as conn:
            items = [brief(conn, s) for s in solo_sessions(conn, character_id)[::-1][:limit]]
        for item in items:
            item["page"] = f"/party/{item['token']}"
        return {"items": items}

    @app.get("/api/v1/characters/{character_id}/groups/{group_id}")
    def character_group(character_id: str, group_id: str, include: Optional[str] = None,
                        scope: Optional[str] = Depends(can_read)):
        """One group session in full. `group_id` may be `latest`.

        Members; totals and pace; who landed the killing blows and on what; monsters fought with
        level and rank; loot per member; quests and skill-ups of the recorder; highlights (deaths,
        level-ups, dungeons, boss fights); and, when the game's combat log was on, `meter` (damage,
        healing, damage taken, crit rate, top abilities and hardest hit per member) and
        `encounters` (each boss fight with its length and per-member damage).

        Per-second figures are worked out: `dps`, `hps`, `dtps`, `damage_pct` and `crit_pct` per
        member (over `meter.fight_seconds`, the time the group was actually fighting), and
        `group_dps` with each member's `dps` and `pct` per boss fight.

        The bulky parts are left out unless named in `include` (comma separated): `route` (map
        positions and marks), `timeline` (event by event), `skills` (see `/skills`), `charts`
        (per-minute series), `screenshots`; `all` for everything. What was left out is in `omitted`."""
        mine(scope, character_id)
        with db() as conn:
            s = find(conn, character_id, group_id)
            d = detail(conn, s)
            d["token"] = token_for(conn, s)
        d["page"] = f"/party/{d['token']}"
        want = {x.strip() for x in (include or "").split(",") if x.strip()}
        unknown = want - set(HEAVY) - {"all"}
        if unknown:
            raise HTTPException(400, f"unknown part {sorted(unknown)[0]!r}; the parts are: {', '.join(HEAVY)}, all")
        d["omitted"] = [name for name in HEAVY if name not in want and "all" not in want]
        for name in d["omitted"]:
            for key in HEAVY[name]:
                d.pop(key, None)
        return with_rates(d)

    @app.get("/api/v1/characters/{character_id}/groups/{group_id}/fight")
    def character_group_fight(character_id: str, group_id: str, boss: Optional[str] = None,
                              step: Optional[int] = Query(None, ge=1, le=600), log: int = Query(0, ge=0, le=500),
                              limit: int = Query(8, ge=1, le=100), scope: Optional[str] = Depends(can_read)):
        """One boss fight of a group session up close, or the whole session (needs the combat log).

        `boss` is the boss's name or its number in the session's `encounters` (1 = first); left out,
        the answer covers the whole session. Gives per member damage, `dps`, share, healing and
        damage taken; `dps_over_time` (each member's damage per second in slices of `step` seconds,
        5 for a boss fight); the `limit` biggest abilities used in that stretch; and, with `log=N`,
        the first N things that happened (hits, misses, heals, auras, deaths; `at` is seconds in)."""
        mine(scope, character_id)
        with db() as conn:
            s = find(conn, character_id, group_id)
            d = with_rates(detail(conn, s))
            who = owner(conn, character_id)
            me = (who["name"] if who else None) or "我"
            fights = d["encounters"]
            if boss is None:
                a, b = _ms(s["started_at"]), _ms(s["ended_at"]) + 999
                about = {"boss": None, "seconds": d["meter"].get("fight_seconds"), "started_at": s["started_at"], "ended_at": s["ended_at"]}
                members = [{k: m[k] for k in ("name", "damage", "dps", "damage_pct", "healing", "hps", "taken", "dtps", "crit_pct")}
                           for m in d["meter"]["members"]]
                step = step or max(30, -(-s["duration"] // 60))
            else:
                chosen_fight = fights[int(boss) - 1] if boss.isdigit() and 1 <= int(boss) <= len(fights) else \
                    next((f for f in fights if f["name"] == boss), None)
                if chosen_fight is None:
                    raise HTTPException(404, "no such boss fight in this session; its fights are: "
                                        + (", ".join(f"{i + 1}. {f['name']}" for i, f in enumerate(fights)) or "none"))
                seconds = max(1, chosen_fight.get("duration") or 0)
                b = _ms(chosen_fight["timestamp"]) + 1999
                a = b - seconds * 1000 - 3000
                about = {"boss": chosen_fight["name"], "number": fights.index(chosen_fight) + 1, "success": chosen_fight["success"],
                         "seconds": seconds, "ended_at": chosen_fight["timestamp"], "group_dps": chosen_fight["group_dps"]}
                rows: Dict[str, Dict[str, Any]] = {}
                for key in ("damage", "healing", "taken"):
                    for r in chosen_fight[key]:
                        row = rows.setdefault(r["name"], {"name": r["name"], "damage": 0, "dps": 0.0, "damage_pct": None, "healing": 0, "taken": 0})
                        row[key] = r["amount"]
                        if key == "damage":
                            row.update(dps=r["dps"], damage_pct=r["pct"])
                members = sorted(rows.values(), key=lambda r: -r["damage"])
                step = step or 5
            series = combat.dps(conn, character_id, a, b, me, step)
            out = {"group_id": s["group_id"], "fight": about, "members": members,
                   "dps_over_time": {"step_seconds": series["step"],
                                     "series": {name: [round((own + pet) / series["step"], 1) for own, pet in zip(v["own"], v["pet"])]
                                                for name, v in series["series"].items()}},
                   "skills": [{k: sk[k] for k in ("who", "spell", "pet", "total", "hits", "avg", "max", "crit_pct", "hit_pct")}
                              for sk in combat.skills(conn, character_id, a, b, me)[:limit]]}
            if log:
                out["log"] = [{"at": round((e["ts"] - a) / 1000, 1), **{k: e[k] for k in ("way", "who", "pet", "enemy", "spell", "amount", "crit", "miss", "kind")
                                                                         if e.get(k) not in (None, 0, False)}}
                              for e in combat.events(conn, character_id, a, b, me, log)]
            return out

    @app.get("/api/v1/characters/{character_id}/groups/{group_id}/skills")
    def character_group_skills(character_id: str, group_id: str, member: Optional[str] = None, spell: Optional[str] = None,
                               enemy: Optional[str] = None, limit: int = Query(30, ge=1, le=500),
                               scope: Optional[str] = Depends(can_read)):
        """Ability by ability, what each member did in a group session (needs the combat log).

        Per member and ability: attempts, hits, misses by reason, total, smallest, largest, average,
        median, spread, normal and critical hits measured apart, and how much of the hit the
        target's armour and resistances took off (`reduced`). Largest total first.

        Narrow it with `member` (a character name), `spell` (an ability name) or `enemy` (a monster
        name: only attacks on that kind). `enemies` lists what was fought, to pick from."""
        mine(scope, character_id)
        with db() as conn:
            s = find(conn, character_id, group_id)
            a, b = _ms(s["started_at"]), _ms(s["ended_at"]) + 999
            who = owner(conn, character_id)
            me = (who["name"] if who else None) or "我"
            return {"group_id": s["group_id"],
                    "skills": chosen(combat.skills(conn, character_id, a, b, me, enemy), member, spell, limit),
                    "enemies": combat.enemies(conn, character_id, a, b)[:limit]}

    @app.get("/api/v1/characters/{character_id}/groups/{group_id}/analysis")
    def character_group_analysis(character_id: str, group_id: str, parts: Optional[str] = None,
                                 limit: int = Query(20, ge=1, le=500), scope: Optional[str] = Depends(can_read)):
        """The deeper combat questions about a group session (needs the combat log).

        `parts` picks what to answer (comma separated; all when left out), each a list cut to `limit`:
        `defence` how each kind of enemy took the group's attacks, its armour, mitigation and time to kill;
        `level_gaps` hit and miss rates by how many levels the target was above the attacker;
        `dots` effects put on enemies, how much of the time they were up and what they ticked for;
        `guard` what happened to attacks aimed at each member (hit, missed, dodged, parried);
        `buffs` effects on each member and their uptime; `debuffs` harmful effects on each member (how
        often, how long, most stacks, from whom); `heals` each healing ability with overhealing;
        `activity` how much of the fighting time each member was attacking;
        `pets` each pet as a fighter; `autoshot` the rhythm of hunters' automatic shots;
        `casts` casts, cost and damage per point of mana, rage or energy.
        Shares come as `{n, of, pct, ci}`: n out of `of` tries, with a 95% interval."""
        mine(scope, character_id)
        with db() as conn:
            s = find(conn, character_id, group_id)
            levels = {m["name"]: m["level"] for m in s["members"] if m.get("level")}
            return {"group_id": s["group_id"],
                    **analysis(conn, character_id, _ms(s["started_at"]), _ms(s["ended_at"]) + 999, levels, parts, limit)}

    def stretch(hours: float, start_time: Optional[datetime], end_time: Optional[datetime]):
        """A stretch of time in ms: the given start and end, or the last `hours` up to now."""
        for t in (start_time, end_time):
            if t is not None and t.tzinfo is None:
                raise HTTPException(400, "times must include a timezone, like 2026-11-24T20:00:00+08:00")
        end = end_time or datetime.now(timezone.utc)
        start = start_time or end - timedelta(hours=hours)
        return int(start.timestamp() * 1000), int(end.timestamp() * 1000)

    @app.get("/api/v1/characters/{character_id}/combat/skills")
    def character_combat_skills(character_id: str, hours: float = Query(24, gt=0, le=24 * 366),
                                start_time: Optional[datetime] = None, end_time: Optional[datetime] = None,
                                member: Optional[str] = None, spell: Optional[str] = None, enemy: Optional[str] = None,
                                limit: int = Query(30, ge=1, le=500), scope: Optional[str] = Depends(can_read)):
        """Ability statistics over any stretch of time, in a group or alone (needs the combat log).

        The same numbers as `/groups/{group_id}/skills`, for the last `hours` (24 unless given) or
        between `start_time` and `end_time`. Everyone near the character whose attacks the log
        recorded as the player's own or the group's is included; `member` narrows to one name."""
        mine(scope, character_id)
        a, b = stretch(hours, start_time, end_time)
        with db() as conn:
            who = owner(conn, character_id)
            me = (who["name"] if who else None) or "我"
            return {"skills": chosen(combat.skills(conn, character_id, a, b, me, enemy), member, spell, limit),
                    "enemies": combat.enemies(conn, character_id, a, b)[:limit]}

    @app.get("/api/v1/characters/{character_id}/combat/analysis")
    def character_combat_analysis(character_id: str, parts: Optional[str] = None, hours: float = Query(24, gt=0, le=24 * 366),
                                  start_time: Optional[datetime] = None, end_time: Optional[datetime] = None,
                                  limit: int = Query(20, ge=1, le=500), scope: Optional[str] = Depends(can_read)):
        """The deeper combat questions over any stretch of time (needs the combat log).

        The same `parts` as `/groups/{group_id}/analysis`, for the last `hours` (24 unless given)
        or between `start_time` and `end_time`. For `level_gaps` only the character's own level is
        known outside a group session."""
        mine(scope, character_id)
        a, b = stretch(hours, start_time, end_time)
        with db() as conn:
            return analysis(conn, character_id, a, b, {}, parts, limit)

    @app.get("/api/v1/characters/{character_id}/combat/auras")
    def character_combat_auras(character_id: str, hours: float = Query(1, gt=0, le=24 * 366),
                               start_time: Optional[datetime] = None, end_time: Optional[datetime] = None,
                               group_id: Optional[str] = None, who: Optional[str] = None, spell: Optional[str] = None,
                               kind: Optional[str] = Query(None, alias="type", pattern="^(BUFF|DEBUFF)$"),
                               on: Optional[str] = Query(None, pattern="^(members|enemies)$"),
                               limit: int = Query(200, ge=1, le=2000), scope: Optional[str] = Depends(can_read)):
        """Buffs and debuffs with their times (needs the combat log), in the order they started.

        Each: `target` (a member, or an enemy when `on_enemy`), `caster` (who put it on: for an
        effect on a member the player or monster that cast it, for one on an enemy the member),
        `spell`, `type` BUFF or DEBUFF, `start` and `end` (epoch ms; `end` null and `still_up`
        when it had not worn off by the end), `seconds`, `refreshes` and the most `stacks`.

        The stretch: a group or solo session (`group_id`, also `latest` / `solo-latest`), or the
        last `hours` (1 unless given), or `start_time` to `end_time`. Narrow it with `who` (a target
        or caster name), `spell`, `type`, or `on` (`members` / `enemies`). Effects on everyone near
        the character are only there if the game wrote them into its combat log: on the group and
        the player always, on enemies only what the group put on them."""
        mine(scope, character_id)
        with db() as conn:
            if group_id:
                s = find(conn, character_id, group_id)
                a, b = _ms(s["started_at"]), _ms(s["ended_at"]) + 999
            else:
                a, b = stretch(hours, start_time, end_time)
            who_ = owner(conn, character_id)
            me = (who_["name"] if who_ else None) or "我"
            rows = [x for x in combat.auras(conn, character_id, a, b, me)
                    if (not who or who in (x["target"], x["caster"])) and (not spell or x["spell"] == spell)
                    and (not kind or x["type"] == kind) and (not on or x["on_enemy"] == (on == "enemies"))]
            return {"start": a, "end": b, "total": len(rows), "auras": rows[:limit]}

    def by_token(conn, token: str) -> Dict[str, Any]:
        row = conn.execute("SELECT group_id, character_id FROM group_shares WHERE token=?", (token,)).fetchone()
        if row:
            for s in (every if row["group_id"].startswith("solo") else sessions)(conn, row["character_id"]):
                if s["group_id"] == row["group_id"]:
                    return s
        raise HTTPException(404, "no such group session")

    @app.get("/api/v1/shared/party/{token}")
    def shared_party(token: str):
        """One group session, readable by anyone who has its link."""
        with db() as conn:
            return detail(conn, by_token(conn, token))

    def window(conn, token: str, start: Optional[int], end: Optional[int]):
        """The session behind a link, and a stretch of time (in ms) that never leaves it."""
        s = by_token(conn, token)
        a, b = _ms(s["started_at"]), _ms(s["ended_at"]) + 999
        who = conn.execute("SELECT name FROM characters WHERE id=?", (s["character_id"],)).fetchone()
        return s["character_id"], max(a, start or a), min(b, end or b), (who["name"] if who else None) or "我"

    @app.get("/api/v1/shared/party/{token}/skills")
    def shared_skills(token: str, enemy: Optional[str] = None, start: Optional[int] = None, end: Optional[int] = None):
        """Ability statistics, for everything fought or one kind of enemy."""
        with db() as conn:
            cid, a, b, me = window(conn, token, start, end)
            return {"skills": combat.skills(conn, cid, a, b, me, enemy), "enemies": combat.enemies(conn, cid, a, b)}

    @app.get("/api/v1/shared/party/{token}/research")
    def shared_research(token: str):
        """The deeper questions: how each enemy defended, level gaps, pets, shot rhythm, costs."""
        with db() as conn:
            s = by_token(conn, token)
            cid, a, b, me = window(conn, token, None, None)
            who = conn.execute("SELECT level FROM characters WHERE id=?", (cid,)).fetchone()
            levels = {m["name"]: m["level"] for m in s["members"] if m.get("level")}
            if who and who["level"]:
                levels[me] = who["level"]
            return {"defence": combat.defence(conn, cid, a, b, me, world.npc_info if world else None),
                    "level_gaps": combat.level_gaps(conn, cid, a, b, me, levels), "levels": levels,
                    "pets": combat.pets(conn, cid, a, b, me), "autoshot": combat.autoshot(conn, cid, a, b, me),
                    "casts": combat.casts(conn, cid, a, b, me), "dots": combat.dots(conn, cid, a, b, me),
                    "buffs": combat.buffs(conn, cid, a, b, me), "debuffs": combat.debuffs(conn, cid, a, b, me),
                    "guard": combat.guard(conn, cid, a, b, me),
                    "heals": combat.heals(conn, cid, a, b, me), "activity": combat.activity(conn, cid, a, b, me)}

    @app.get("/api/v1/shared/party/{token}/across")
    def shared_across(token: str, who: str, spell: str, pet: bool = False):
        """For the session's page: one member's ability against each kind of enemy."""
        with db() as conn:
            cid, a, b, me = window(conn, token, None, None)
            return {"targets": combat.across(conn, cid, a, b, me, who, spell, pet)}

    @app.get("/api/v1/shared/party/{token}/aggro")
    def shared_aggro(token: str):
        """For the session's page: threat in each fight of the session, with every reading."""
        from . import threat
        with db() as conn:
            cid, a, b, me = window(conn, token, None, None)
            iso = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return {"fights": threat.fights(conn, cid, iso(a), iso(b), me, series=True, limit=300)}

    @app.get("/api/v1/shared/party/{token}/auras")
    def shared_auras(token: str):
        """For the session's page: every buff and debuff on the group, with its times."""
        with db() as conn:
            cid, a, b, me = window(conn, token, None, None)
            rows = [x for x in combat.auras(conn, cid, a, b, me) if not x["on_enemy"]]
            return {"start": a, "end": b, "auras": rows[:5000]}

    @app.get("/api/v1/combat/npcs")
    def npc_database(character_id: str, scope: Optional[str] = Depends(can_read)):
        """Everything this character's logs have shown about each kind of enemy, over all time."""
        if scope is not None and character_id != scope:
            raise HTTPException(403, "this link only covers one character")
        with db() as conn:
            who = conn.execute("SELECT name FROM characters WHERE id=?", (character_id,)).fetchone()
            return {"items": combat.defence(conn, character_id, 0, 2 ** 62, (who["name"] if who else None) or "我",
                                            world.npc_info if world else None)}

    @app.get("/api/v1/shared/party/{token}/amounts")
    def shared_amounts(token: str, spell: str, pet: bool = False, enemy: Optional[str] = None,
                       start: Optional[int] = None, end: Optional[int] = None):
        """For the session's page: every landed hit of one ability, per member, to draw its spread."""
        with db() as conn:
            cid, a, b, me = window(conn, token, start, end)
            return {"members": combat.amounts(conn, cid, a, b, me, spell, pet, enemy)}

    @app.get("/api/v1/shared/party/{token}/dps")
    def shared_dps(token: str, start: Optional[int] = None, end: Optional[int] = None, step: int = 5):
        """For the session's page: damage per member in time slices, own and pet apart."""
        with db() as conn:
            cid, a, b, me = window(conn, token, start, end)
            return combat.dps(conn, cid, a, b, me, step)

    @app.get("/api/v1/shared/party/{token}/hits")
    def shared_hits(token: str, start: Optional[int] = None, end: Optional[int] = None, limit: int = 600):
        """For the session's page: hits, misses, heals, auras and deaths of a stretch of time, oldest first."""
        with db() as conn:
            cid, a, b, me = window(conn, token, start, end)
            return {"items": combat.events(conn, cid, a, b, me, max(1, min(limit, 2000)))}

    @app.get("/api/v1/shared/party/{token}/hits.csv")
    def shared_csv(token: str):
        """Every row of the session, for a spreadsheet or another program."""
        with db() as conn:
            cid, a, b, me = window(conn, token, None, None)
            text = "".join(combat.csv_lines(conn, cid, a, b, me))
        return Response("\ufeff" + text, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="combat-hits.csv"'})

    @app.get("/api/v1/shared/party/{token}/hits.json")
    def shared_json(token: str):
        """Every row of the session as JSON: column names once, then the rows."""
        with db() as conn:
            cid, a, b, me = window(conn, token, None, None)
            cols = ("ts",) + combat.COLUMNS[2:]
            rows = [list(r) for r in conn.execute(f"SELECT {', '.join(cols)} FROM combat_hits WHERE character_id=? "
                                                  "AND ts>=? AND ts<=? ORDER BY ts, pos", (cid, a, b))]
        for row in rows:
            row[2] = row[2] or me
        return {"columns": cols, "rows": rows}

    @app.get("/api/v1/shared/party/{token}/map/{map_id}")
    def shared_map(token: str, map_id: int):
        """For the session's page: the background image of a map the session visited."""
        with db() as conn:
            s = by_token(conn, token)
            seen = conn.execute("SELECT 1 FROM events WHERE character_id=? AND map_id=? AND timestamp>=? "
                                "AND timestamp<=? LIMIT 1",
                                (s["character_id"], map_id, s["started_at"], s["ended_at"])).fetchone()
        path = os.path.join(maps_dir, f"{map_id}.jpg")
        if not seen or not os.path.isfile(path):
            raise HTTPException(404, "no image for this map")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/v1/shared/party/{token}/shot/{shot_id}")
    def shared_shot(token: str, shot_id: str, thumb: bool = False):
        """A screenshot taken during that session, for anyone with the session's link."""
        with db() as conn:
            s = by_token(conn, token)
            ok = shots is not None and conn.execute(
                "SELECT 1 FROM screenshots WHERE id=? AND character_id=? AND taken_at>=? AND taken_at<=?",
                (shot_id, s["character_id"], s["started_at"], s["ended_at"])).fetchone()
        path = shots.path(shot_id, thumb) if ok else None
        if not path or not os.path.isfile(path):
            raise HTTPException(404, "no such screenshot")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/party/{token}", include_in_schema=False)
    def party_page(token: str):
        # Static page with no data in it; it reads the token from its own address.
        return FileResponse(os.path.join(os.path.dirname(__file__), "party.html"), media_type="text/html",
                            headers={"Cache-Control": "no-cache", "X-Robots-Tag": "noindex"})
