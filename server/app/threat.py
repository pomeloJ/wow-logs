"""Threat: the game's own threat figures for each fight, read by the addon.

The addon (AdventureCollector 0.3.4 and later) reads UnitDetailedThreatSituation while in
combat, every time the threat list of the current target changes, for the player, the pet and
every group member, and sends one THREAT event per enemy and fight:

    mob_guid, mob_name   the enemy (either may be missing when the client hid it)
    started, duration    when the fight began (epoch seconds) and how long it lasted
    units                [{guid, name, pet}], name "" being the character itself
    samples              "t,u,threat,pct,raw,status;..." with t seconds since the fight began,
                         u the 1-based place in units, threat the raw value, pct the percent of
                         what pulls aggro, raw the percent of the tank's threat, status the
                         game's 0 (low), 1 (above the tank), 2 (tanking, not securely), 3 (tanking)
    cut                  true when the fight was too long and only its first readings were kept

Nothing here is a threat formula: the numbers are the game's, only summed up over time. Only
the current target can be read, so an enemy the character never targeted has no figures, and
a reading is only taken when the game says the list changed (the rate between two readings is
taken as even).
"""
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException, Query

WINDOW = 3.0          # seconds: the shortest stretch threat per second is measured over for the peak


def _iso(epoch: Any) -> Optional[str]:
    if not isinstance(epoch, (int, float)) or isinstance(epoch, bool):
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(v: str) -> Optional[float]:
    try:
        return float(v)
    except ValueError:
        return None


def parse(samples: Any) -> List[tuple]:
    """(t, unit, threat, pct, raw, status) rows in time order; malformed rows are skipped."""
    rows = []
    if not isinstance(samples, str):
        return rows
    for part in samples.split(";"):
        bits = [_num(b) for b in part.split(",")]
        if len(bits) != 6 or any(b is None for b in bits):
            continue
        t, u, threat, pct, raw, status = bits
        rows.append((t, int(u), threat, pct, raw, int(status)))
    rows.sort(key=lambda r: r[0])
    return rows


def _peak_rate(points: List[tuple]) -> Optional[float]:
    """Highest threat gained per second over any stretch of at least WINDOW seconds, so never
    below the average over the whole of a stretch that long."""
    best = None
    for i in range(len(points)):
        for j in range(len(points) - 1, i, -1):
            span = points[j][0] - points[i][0]
            if span < WINDOW:
                break
            rate = (points[j][1] - points[i][1]) / span
            best = rate if best is None or rate > best else best
    return None if best is None else round(best, 1)


def analyse(payload: Dict[str, Any], me: Optional[str], series: bool = False) -> Dict[str, Any]:
    # Seen in this game: as a fight starts, units with no threat yet can read "tanking" (status 3)
    # and odd percents (232, 255). Nobody holds an enemy with no threat, so such a row counts as 0.
    rows = [r if r[2] > 0 else (r[0], r[1], 0.0, 0.0, 0.0, 0) for r in parse(payload.get("samples"))]
    units = [u if isinstance(u, dict) else {} for u in (payload.get("units") or [])]
    end = max([r[0] for r in rows] + [0.0])
    duration = payload.get("duration")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        end = max(end, float(duration))
    # Only the target can be read: the enemy is seen from its first reading to its last one,
    # which in a fight against several is only part of the fight.
    seen_from = rows[0][0] if rows else 0.0
    seen_to = rows[-1][0] if rows else 0.0

    def label(i: int) -> Dict[str, Any]:
        u = units[i - 1] if 0 < i <= len(units) else {}
        mine = u.get("name") == ""
        return {"name": (me or "") if mine else u.get("name"), "is_me": mine, "pet": bool(u.get("pet")),
                "guid": u.get("guid")}

    per: Dict[int, List[tuple]] = {}
    for r in rows:
        per.setdefault(r[1], []).append(r)
    out = []
    for i, mine in per.items():
        tanking = 0.0
        for k, r in enumerate(mine):
            if r[5] >= 2:
                tanking += (mine[k + 1][0] if k + 1 < len(mine) else seen_to) - r[0]
        first, final = mine[0], mine[-1]
        gained, took = final[2] - first[2], final[0] - first[0]
        entry = {**label(i), "final_threat": int(final[2]), "peak_threat": int(max(r[2] for r in mine)),
                 "avg_tps": round(gained / took, 1) if took >= 1 else None,
                 "peak_tps": _peak_rate([(r[0], r[2]) for r in mine]),
                 "top_percent": int(max(r[3] for r in mine)), "tanking_seconds": round(tanking, 1),
                 "readings": len(mine)}
        if series:
            entry["series"] = [[r[0], int(r[2]), int(r[3]), r[5]] for r in mine]
        out.append(entry)
    out.sort(key=lambda e: -e["final_threat"])
    for n, e in enumerate(out, 1):
        e["rank"] = n

    # who held the enemy: the unit the game last said is tanking it
    changes, holder = [], None
    for r in rows:
        if r[5] >= 2 and r[1] != holder:
            if holder is not None:
                changes.append({"t": r[0], "from": label(holder)["name"], "to": label(r[1])["name"],
                                "to_me": label(r[1])["is_me"], "from_me": label(holder)["is_me"]})
            holder = r[1]
    return {"mob_name": payload.get("mob_name"), "mob_guid": payload.get("mob_guid"),
            "started": _iso(payload.get("started")), "duration": round(end, 1),
            "seen_from": seen_from, "seen_seconds": round(seen_to - seen_from, 1), "cut": bool(payload.get("cut")),
            "units": out, "aggro_changes": changes,
            "lost_aggro": sum(1 for c in changes if c["from_me"]), "gained_aggro": sum(1 for c in changes if c["to_me"])}


def register(app, db, can_read) -> None:
    @app.get("/api/v1/characters/{character_id}/threat")
    def character_threat(character_id: str, start_time: Optional[datetime] = None,
                         end_time: Optional[datetime] = None, limit: int = Query(20, ge=1, le=200),
                         series: bool = False, scope: Optional[str] = Depends(can_read)):
        """Threat in each fight, newest first, from the game's own threat figures (see the addon's
        UnitDetailedThreatSituation readings). One entry per enemy and fight.

        `duration` is the whole fight; the enemy itself is seen from `seen_from` (seconds into the
        fight) for `seen_seconds`, while it was the target. Per unit (the character, its pet and
        group members): `final_threat`, `peak_threat`, `avg_tps` (threat gained per second while
        seen; null under a second), `peak_tps` (best stretch of at least 3 s),
        `top_percent` (highest percent of what pulls aggro; 100 means it had or was pulling it),
        `tanking_seconds` (while seen) and `rank` by final threat. A unit with no threat never
        counts as holding the enemy, whatever the game says for it at the start of a fight. `aggro_changes` lists who took the enemy
        from whom and when (`t` in seconds from the start); `lost_aggro` / `gained_aggro` count
        those for the character. `series=true` adds every reading as [t, threat, percent, status].

        Only the target can be read in this game, so an enemy that was never targeted has no
        entry. Inside dungeons the game hides the target's name and GUID; the enemy is then taken
        from the combat log of the same moments, with `mob_guessed` true (and `mob_unsure` when
        another enemy was hit nearly as often). Empty until the addon 0.3.4 or later has been played with."""
        if scope is not None and character_id != scope:
            raise HTTPException(403, "this link only covers one character")
        start = end = None
        for value, name in ((start_time, "start"), (end_time, "end")):
            if value is not None:
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                if name == "start":
                    start = value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                else:
                    end = value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with db() as conn:
            row = conn.execute("SELECT name FROM characters WHERE id=?", (character_id,)).fetchone()
            me = row["name"] if row else None
            return {"character_id": character_id, "fights": fights(conn, character_id, start, end, me, series, limit)}


def fights(conn, character_id: str, start: Optional[str], end: Optional[str], me: Optional[str],
           series: bool = False, limit: int = 200) -> List[Dict[str, Any]]:
    """The THREAT events between two times (ISO, either may be None), newest first, analysed."""
    where, args = ["character_id=?", "event_type='THREAT'"], [character_id]
    if start:
        where.append("timestamp>=?")
        args.append(start)
    if end:
        where.append("timestamp<=?")
        args.append(end)
    rows = conn.execute(f"SELECT event_id, timestamp, payload_json FROM events WHERE {' AND '.join(where)} "
                        "ORDER BY timestamp DESC, event_id DESC LIMIT ?", (*args, limit)).fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload_json"])
        except ValueError:
            continue
        if isinstance(payload, dict):
            fight = {"event_id": r["event_id"], "timestamp": r["timestamp"], **analyse(payload, me, series)}
            if not fight["mob_name"] and not fight["mob_guid"]:
                fight.update(guess(conn, character_id, payload.get("started"), fight["seen_from"], fight["seen_seconds"]))
            out.append(fight)
    return out


MARGIN = 1.0          # seconds: the addon's clock and the combat log's are not read at the same moment


def guess(conn, character_id: str, started: Any, seen_from: float, seen_seconds: float) -> Dict[str, Any]:
    """Inside dungeons the game hides the target from addons (its GUID and name come back secret),
    so a fight can have threat figures and no enemy. The combat log of the same moments still
    names everything: the enemy is taken to be the one the character itself hit most while the
    enemy was seen, else the one hit or hitting most. `mob_guessed` says the name came this way;
    `mob_unsure` that another enemy was hit nearly as often."""
    if not isinstance(started, (int, float)) or isinstance(started, bool):
        return {}
    a = int((started + seen_from - MARGIN) * 1000)
    b = int((started + seen_from + seen_seconds + MARGIN) * 1000)
    try:
        rows = conn.execute(
            "SELECT guid, MAX(enemy) AS enemy, COUNT(*) AS n, "
            "SUM(CASE WHEN way='out' AND COALESCE(who,'')='' AND COALESCE(pet,0)=0 THEN 1 ELSE 0 END) AS mine "
            "FROM combat_hits WHERE character_id=? AND ts>=? AND ts<=? AND way IN ('out','in') "
            "AND guid IS NOT NULL AND guid NOT LIKE 'Player-%' AND enemy IS NOT NULL "
            "GROUP BY guid ORDER BY mine DESC, n DESC LIMIT 2", (character_id, a, b)).fetchall()
    except Exception:          # no combat log table on this server
        return {}
    if not rows:
        return {}
    top = rows[0]
    by = (lambda r: r["mine"]) if top["mine"] else (lambda r: r["n"])
    unsure = len(rows) > 1 and by(rows[1]) * 2 > by(top)
    return {"mob_name": top["enemy"], "mob_guid": top["guid"], "mob_guessed": True, "mob_unsure": unsure}
