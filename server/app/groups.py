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
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from fastapi import Depends, HTTPException
from fastapi.responses import FileResponse, Response

from . import combat

ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
# Older addon versions recorded every roll for group loot as a pick-up, named after the roll
# window's link instead of the item. Those records are left out wherever loot is counted.
ROLL_LINES = ("戰利品", "Loot")
HIDDEN = ("LOCATION_SAMPLE", "CHAT_MESSAGE", "MAP_SCREENSHOT", "PARTY_POSITIONS", "GROUP_METER", "PARTY_LOOT",
          "GROUP_ENCOUNTER")
HIGHLIGHTS = ("LEVEL_UP", "ACHIEVEMENT_EARNED", "PLAYER_DIED", "PARTY_MEMBER_DIED", "GROUP_MEMBER_LEVEL_UP",
              "INSTANCE_ENTERED", "ENCOUNTER_END")
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
        who = conn.execute("SELECT name, class, level, race FROM characters WHERE id=?", (cid,)).fetchone()
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
        return out

    def brief(conn, s: Dict[str, Any]) -> Dict[str, Any]:
        act = activity(s["character_id"], _dt(s["started_at"]), _dt(s["ended_at"]))
        return {**{k: s[k] for k in ("group_id", "started_at", "ended_at", "ongoing", "duration", "raid",
                                     "zone_name", "members")},
                "kills": act["kills"], "xp": act["xp"], "items_looted": act["items_looted"],
                "token": token_for(conn, s)}

    @app.get("/api/v1/groups")
    def list_groups(character_id: str, scope: Optional[str] = Depends(can_read)):
        """Group sessions of a character, newest first, each with its share token."""
        if scope is not None and character_id != scope:
            raise HTTPException(403, "this link only covers one character")
        with db() as conn:
            items = [brief(conn, s) for s in sessions(conn, character_id)]
        return {"items": items[::-1]}

    def by_token(conn, token: str) -> Dict[str, Any]:
        row = conn.execute("SELECT group_id, character_id FROM group_shares WHERE token=?", (token,)).fetchone()
        if row:
            for s in sessions(conn, row["character_id"]):
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
                    "buffs": combat.buffs(conn, cid, a, b, me), "guard": combat.guard(conn, cid, a, b, me),
                    "heals": combat.heals(conn, cid, a, b, me), "activity": combat.activity(conn, cid, a, b, me)}

    @app.get("/api/v1/shared/party/{token}/across")
    def shared_across(token: str, who: str, spell: str, pet: bool = False):
        with db() as conn:
            cid, a, b, me = window(conn, token, None, None)
            return {"targets": combat.across(conn, cid, a, b, me, who, spell, pet)}

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
        with db() as conn:
            cid, a, b, me = window(conn, token, start, end)
            return {"members": combat.amounts(conn, cid, a, b, me, spell, pet, enemy)}

    @app.get("/api/v1/shared/party/{token}/dps")
    def shared_dps(token: str, start: Optional[int] = None, end: Optional[int] = None, step: int = 5):
        with db() as conn:
            cid, a, b, me = window(conn, token, start, end)
            return combat.dps(conn, cid, a, b, me, step)

    @app.get("/api/v1/shared/party/{token}/hits")
    def shared_hits(token: str, start: Optional[int] = None, end: Optional[int] = None, limit: int = 600):
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
