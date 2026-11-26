"""Maps and routes for an assistant: where a map lies in the world, what its picture covers,
and a suggested way from a character to a place (an NPC, an object, a quest, a point).

Geometry. Every map's rectangle in its continent, in yards, comes from the game client's own
tables (world_uimaps in the world database, see world_importer/uimaps.py); a map missing there
is taken from the addon's MAP_RECT events, which ask the client the same thing. World X grows
to the north and Y to the west. Positions everywhere else in the API are a UiMapID with x, y
in percent of that map's picture (0,0 top-left), which is what the game itself shows.

Routes. There is no terrain, collision or navigation mesh for this game version, so a route
cannot be proved walkable. It is computed on a grid over the continent in which cells where
characters have actually walked (their position samples, flights left out) are cheap and the
rest dear: the way follows known ground where it can and cuts across where nothing is known.
Each leg says which it is (`walked` or not); `verified_passable` is always false, and the
answer says so in words, so an assistant never presents a guessed line as a sure way.
"""
import heapq
import io
import json
import math
import os
import sqlite3
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from fastapi import Depends, HTTPException, Query
from fastapi.responses import Response

RES = 15.0                  # yards: the size of a cell of walked ground
MAX_CELLS = 160_000         # a larger search area is searched on coarser cells
WALK, MOUNT60, MOUNT100 = 7.0, 11.2, 14.0       # yards per second
COST_WALKED, COST_NEAR, COST_UNKNOWN = 1.0, 1.6, 3.0
TYPES = {0: "cosmic", 1: "world", 2: "continent", 3: "zone", 4: "dungeon", 5: "micro", 6: "battleground"}
NOTE = ("推測路線：沒有這個遊戲版本的地形與導航資料，綠色（walked）是玩家實際走過的路段，"
        "橘色（walked=false）是直線推測，可能被山壁、水域或建築擋住，不保證能到達。")


def _ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


class Geometry:
    """Map rectangles and the walked-ground grid, cached for a few minutes."""

    def __init__(self, world, db):
        self.world, self.db = world, db
        self._maps: Tuple[float, Dict[int, Dict[str, Any]]] = (0.0, {})
        self._walked: Dict[int, Tuple[float, Set[Tuple[int, int]]]] = {}

    def maps(self) -> Dict[int, Dict[str, Any]]:
        if time.time() - self._maps[0] < 300:
            return self._maps[1]
        out: Dict[int, Dict[str, Any]] = {}
        if self.world.available():
            try:
                with self.world.conn() as c:
                    for r in c.execute("SELECT * FROM world_uimaps"):
                        out[r["ui_map_id"]] = {**dict(r), "geometry_source": "client tables (" + (r["source"] or "") + ")"}
            except sqlite3.OperationalError:            # an older world database without the table
                pass
        with self.db() as conn:
            for r in conn.execute("SELECT payload_json FROM events WHERE event_type='MAP_RECT' ORDER BY timestamp"):
                p = json.loads(r["payload_json"])
                mid = p.get("map_id")
                if not isinstance(mid, int) or (mid in out and out[mid].get("x_min") is not None):
                    continue
                try:
                    x_max, y_max, x_min, y_min = (float(p[k]) for k in ("world_x0", "world_y0", "world_x1", "world_y1"))
                except (KeyError, TypeError, ValueError):
                    continue
                if x_max <= x_min or y_max <= y_min:
                    continue
                out[mid] = {**out.get(mid, {}), "ui_map_id": mid, "name": out.get(mid, {}).get("name") or p.get("map_name"),
                            "parent_ui_map_id": p.get("parent_map_id"), "type": p.get("map_type"),
                            "instance_id": p.get("continent_id"), "x_min": x_min, "x_max": x_max,
                            "y_min": y_min, "y_max": y_max, "geometry_source": "addon (C_Map.GetWorldPosFromMapPos)"}
        self._maps = (time.time(), out)
        return out

    def rect(self, map_id: Optional[int]) -> Optional[Dict[str, Any]]:
        m = self.maps().get(map_id) if map_id is not None else None
        return m if m and m.get("x_min") is not None else None

    @staticmethod
    def to_world(m: Dict[str, Any], x: float, y: float) -> Tuple[float, float]:
        return m["x_max"] - y / 100 * (m["x_max"] - m["x_min"]), m["y_max"] - x / 100 * (m["y_max"] - m["y_min"])

    @staticmethod
    def to_map(m: Dict[str, Any], wx: float, wy: float) -> Tuple[float, float]:
        return (m["y_max"] - wy) / (m["y_max"] - m["y_min"]) * 100, (m["x_max"] - wx) / (m["x_max"] - m["x_min"]) * 100

    def where(self, inst: int, wx: float, wy: float, prefer: Iterable[int] = ()) -> Optional[Dict[str, Any]]:
        """The map a world point is on: one of `prefer` if it covers it, else the smallest zone."""
        best = None
        for mid in list(prefer) + [None]:
            for m in ([self.rect(mid)] if mid is not None else self.maps().values()):
                if not m or m.get("x_min") is None or m.get("instance_id") != inst or m.get("type") not in (3, 4, 5, 6):
                    continue
                if m["x_min"] <= wx <= m["x_max"] and m["y_min"] <= wy <= m["y_max"]:
                    if mid is not None:
                        return m
                    area = (m["x_max"] - m["x_min"]) * (m["y_max"] - m["y_min"])
                    if best is None or area < best[0]:
                        best = (area, m)
        return best[1] if best else None

    def walked(self, inst: int) -> Set[Tuple[int, int]]:
        """Cells (RES yards) of one continent where characters have walked: position samples
        joined to the next one when the step is slow enough to be on foot or mounted."""
        hit = self._walked.get(inst)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        cells: Set[Tuple[int, int]] = set()
        prev: Dict[str, Tuple[float, int, float, float]] = {}
        with self.db() as conn:
            rows = conn.execute("SELECT character_id, timestamp, payload_json FROM events WHERE event_type='LOCATION_SAMPLE' "
                                "ORDER BY character_id, timestamp").fetchall()
        for r in rows:
            p = json.loads(r["payload_json"])
            m = self.rect(p.get("map_id"))
            if not m or m.get("instance_id") != inst or not isinstance(p.get("x"), (int, float)) or not isinstance(p.get("y"), (int, float)):
                continue
            t = _ts(r["timestamp"])
            wx, wy = self.to_world(m, p["x"], p["y"])
            last = prev.get(r["character_id"])
            prev[r["character_id"]] = (t, inst, wx, wy)
            if not last:
                continue
            dt, d = t - last[0], math.hypot(wx - last[2], wy - last[3])
            if dt <= 0 or dt > 180 or d / dt > 16 or d > 400:       # a flight, a hearthstone, or a gap
                continue
            steps = max(1, int(d / (RES / 2)))
            for i in range(steps + 1):
                f = i / steps
                cells.add((int((last[2] + (wx - last[2]) * f) // RES), int((last[3] + (wy - last[3]) * f) // RES)))
        self._walked[inst] = (time.time(), cells)
        return cells


def _simplify(points: List[Tuple[float, float]], tol: float) -> List[Tuple[float, float]]:
    """Ramer-Douglas-Peucker."""
    if len(points) < 3:
        return points
    (ax, ay), (bx, by) = points[0], points[-1]
    length = math.hypot(bx - ax, by - ay) or 1e-9
    far, idx = 0.0, 0
    for i, (px, py) in enumerate(points[1:-1], 1):
        d = abs((bx - ax) * (ay - py) - (ax - px) * (by - ay)) / length
        if d > far:
            far, idx = d, i
    if far <= tol:
        return [points[0], points[-1]]
    return _simplify(points[:idx + 1], tol)[:-1] + _simplify(points[idx:], tol)


def plan(walked: Set[Tuple[int, int]], a: Tuple[float, float], b: Tuple[float, float]) -> List[Tuple[float, float, bool]]:
    """World points from a to b, each with whether the leg that ends there is on walked ground."""
    dist = math.hypot(b[0] - a[0], b[1] - a[1])
    pad = max(300.0, dist * 0.3)
    x0, x1 = min(a[0], b[0]) - pad, max(a[0], b[0]) + pad
    y0, y1 = min(a[1], b[1]) - pad, max(a[1], b[1]) + pad
    k = 1
    while ((x1 - x0) / (RES * k)) * ((y1 - y0) / (RES * k)) > MAX_CELLS:
        k *= 2
    size = RES * k
    known = walked if k == 1 else {(i // k, j // k) for i, j in walked}
    gx0, gy0 = int(x0 // size), int(y0 // size)
    gx1, gy1 = int(x1 // size), int(y1 // size)
    start, goal = (int(a[0] // size), int(a[1] // size)), (int(b[0] // size), int(b[1] // size))

    near_cache: Dict[Tuple[int, int], float] = {}

    def cost(c: Tuple[int, int]) -> float:
        v = near_cache.get(c)
        if v is None:
            if c in known:
                v = COST_WALKED
            elif any((c[0] + di, c[1] + dj) in known for di in (-1, 0, 1) for dj in (-1, 0, 1)):
                v = COST_NEAR
            else:
                v = COST_UNKNOWN
            near_cache[c] = v
        return v

    came: Dict[Tuple[int, int], Tuple[int, int]] = {}
    best = {start: 0.0}
    heap = [(0.0, start)]
    steps = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
             (1, 1, 1.4142), (1, -1, 1.4142), (-1, 1, 1.4142), (-1, -1, 1.4142))
    while heap:
        _, c = heapq.heappop(heap)
        if c == goal:
            break
        g = best[c]
        for di, dj, w in steps:
            n = (c[0] + di, c[1] + dj)
            if not (gx0 <= n[0] <= gx1 and gy0 <= n[1] <= gy1):
                continue
            ng = g + w * (cost(c) + cost(n)) / 2
            if ng < best.get(n, 1e18):
                best[n], came[n] = ng, c
                heapq.heappush(heap, (ng + 1.2 * math.hypot(goal[0] - n[0], goal[1] - n[1]), n))
    path = [goal]
    while path[-1] != start and path[-1] in came:
        path.append(came[path[-1]])
    path.reverse()
    # runs of walked and not-walked cells, each simplified on its own
    out: List[Tuple[float, float, bool]] = [(a[0], a[1], True)]
    run: List[Tuple[float, float]] = []
    run_walked = None
    for c in path[1:-1]:
        w = c in known
        p = ((c[0] + 0.5) * size, (c[1] + 0.5) * size)
        if run_walked is None or w == run_walked:
            run.append(p)
            run_walked = w
            continue
        out += [(x, y, run_walked) for x, y in _simplify(run, size * 1.5)]
        run, run_walked = [p], w
    if run:
        out += [(x, y, bool(run_walked)) for x, y in _simplify(run, size * 1.5)]
    out.append((b[0], b[1], goal in known))
    return out


def register(app, db, world, can_read, maps_dir: str) -> Geometry:
    geo = Geometry(world, db)

    def map_name(mid: Optional[int]) -> Optional[str]:
        """The Chinese name where the world database has one, else the client's English one."""
        if mid is None:
            return None
        if world.available():
            with world.conn() as c:
                r = c.execute("SELECT name, name_zh FROM world_maps WHERE ui_map_id=? AND parent_area_id IS NULL "
                              "ORDER BY area_id LIMIT 1", (mid,)).fetchone()
                if r and (r["name_zh"] or r["name"]):
                    return r["name_zh"] or r["name"]
        m = geo.maps().get(mid)
        return m.get("name") if m else None

    def image_info(mid: int) -> Dict[str, Any]:
        path = os.path.join(maps_dir, f"{mid}.jpg")
        if not os.path.isfile(path):
            return {"available": False}
        w = h = None
        try:
            from PIL import Image
            with Image.open(path) as im:
                w, h = im.size
        except Exception:
            pass
        with db() as conn:
            r = conn.execute("SELECT updated_at FROM map_images WHERE map_id=?", (mid,)).fetchone()
        return {"available": True, "url": f"/api/v1/maps/{mid}/image", "format": "image/jpeg", "width": w, "height": h,
                "content_box": [0, 0, w, h] if w else None, "updated_at": r["updated_at"] if r else None}

    def describe(mid: int) -> Dict[str, Any]:
        m = geo.maps().get(mid) or {}
        rect = geo.rect(mid)
        parent = m.get("parent_ui_map_id")
        cont = None
        p, seen = m, set()
        while p and p.get("type") not in (2, None) and p.get("parent_ui_map_id") and p["parent_ui_map_id"] not in seen:
            seen.add(p["parent_ui_map_id"])
            p = geo.maps().get(p["parent_ui_map_id"])
        if p and p.get("type") == 2:
            cont = {"map_id": p["ui_map_id"], "name": map_name(p["ui_map_id"])}
        out = {
            "map_id": mid, "name": map_name(mid), "name_en": m.get("name"), "type": TYPES.get(m.get("type")),
            "parent": {"map_id": parent, "name": map_name(parent)} if parent else None, "continent": cont,
            "instance_id": m.get("instance_id"), "area_id": m.get("area_id"),
            "image": image_info(mid),
            "coordinates": {
                "system": "x, y in percent of this map's picture: (0,0) top-left, (100,100) bottom-right; "
                          "pixel = (x / 100 * width, y / 100 * height). The game shows the same numbers.",
                "ids": "map_id is the UiMapID the game's map API uses. instance_id is the continent the map lies in "
                       "(0 Eastern Kingdoms, 1 Kalimdor, ...) and area_id the zone's AreaTable id: neither is a map_id.",
            },
            "sources": {"geometry": m.get("geometry_source"), "build": m.get("build"), "image": "game map art via Warcraft Wiki"
                        if mid in range(1411, 1462) else "player's in-game map screenshot"},
        }
        if rect:
            out["world"] = {"x_min": rect["x_min"], "x_max": rect["x_max"], "y_min": rect["y_min"], "y_max": rect["y_max"],
                            "width_yards": round(rect["y_max"] - rect["y_min"], 1),
                            "height_yards": round(rect["x_max"] - rect["x_min"], 1),
                            "axes": "world X grows north, Y grows west (yards). top-left of the picture = (x_max, y_max).",
                            "to_world": "X = x_max - y/100*(x_max-x_min); Y = y_max - x/100*(y_max-y_min)"}
        return out

    @app.get("/api/v1/world/map", dependencies=[Depends(can_read)])
    def world_map(map_id: Optional[int] = None, q: Optional[str] = Query(None, max_length=60)):
        """One map for drawing and measuring: its UiMapID, names, parent and continent, the background
        picture (address, size), how a position in percent maps to the picture's pixels, and where the map
        lies in the world in yards. Look a map up by `map_id`, or by name with `q` (English or Chinese)."""
        if map_id is None and not q:
            raise HTTPException(400, "give map_id or q")
        if map_id is None:
            want = q.strip().lower()
            hits = [mid for mid, m in geo.maps().items() if want in (m.get("name") or "").lower()]
            if not hits and world.available():
                with world.conn() as c:
                    hits = [r["ui_map_id"] for r in c.execute(
                        "SELECT DISTINCT ui_map_id FROM world_maps WHERE parent_area_id IS NULL AND ui_map_id IS NOT NULL "
                        "AND (name_zh LIKE ? OR lower(name) LIKE ?) LIMIT 10", (f"%{q}%", f"%{want}%"))]
            if not hits:
                raise HTTPException(404, "no map by that name")
            hits.sort(key=lambda mid: (geo.maps().get(mid, {}).get("type") != 3, mid))
            map_id = hits[0]
        if map_id not in geo.maps() and not image_info(map_id)["available"]:
            raise HTTPException(404, "unknown map")
        return describe(map_id)

    # ---------------------------------------------------------------- routes
    def latest_position(cid: str) -> Optional[Dict[str, Any]]:
        with db() as conn:
            ev = conn.execute("SELECT timestamp, payload_json FROM events WHERE character_id=? AND "
                              "event_type='LOCATION_SAMPLE' ORDER BY timestamp DESC LIMIT 1", (cid,)).fetchone()
            snap = conn.execute("SELECT captured_at, payload_json FROM character_snapshots WHERE character_id=?",
                                (cid,)).fetchone()
        found = []
        if ev:
            p = json.loads(ev["payload_json"])
            found.append((ev["timestamp"], p, "last position sample"))
        if snap:
            p = json.loads(snap["payload_json"])
            found.append((snap["captured_at"], p, "character snapshot"))
        found = [f for f in found if isinstance(f[1].get("map_id"), int) and isinstance(f[1].get("x"), (int, float))]
        if not found:
            return None
        at, p, src = max(found, key=lambda f: _ts(f[0]) if isinstance(f[0], str) else 0)
        return {"map_id": p["map_id"], "x": p["x"], "y": p["y"], "at": at,
                "source": src + " (only as new as the last save; a position read off an AI screenshot is fresher)"}

    def spots(c, kind: str, eid: int, role: str, note: Optional[str] = None) -> List[Dict[str, Any]]:
        table, key = ("world_npcs", "npc_id") if kind == "npc" else ("world_objects", "object_id")
        r = c.execute(f"SELECT name, name_zh FROM {table} WHERE {key}=?", (eid,)).fetchone()
        name = (r["name_zh"] or r["name"]) if r else None
        return [{"kind": kind, "id": eid, "name": name, "role": role, "note": note,
                 "map_id": s["ui_map_id"], "x": s["x"], "y": s["y"]}
                for s in c.execute("SELECT ui_map_id, x, y FROM world_spawns WHERE kind=? AND entity_id=? "
                                   "AND ui_map_id IS NOT NULL LIMIT 400", (kind, eid))]

    def quest_spots(c, qid: int, role: str, cid: Optional[str]) -> Tuple[List[Dict[str, Any]], str]:
        r = c.execute("SELECT data FROM world_quests WHERE quest_id=?", (qid,)).fetchone()
        if not r:
            raise HTTPException(404, "quest not found")
        d = json.loads(r["data"] or "{}")
        if role == "auto":
            role = "start"
            if cid:
                with db() as conn:
                    snap = conn.execute("SELECT payload_json FROM character_snapshots WHERE character_id=?", (cid,)).fetchone()
                log = {q.get("quest_id"): q for q in (json.loads(snap["payload_json"]).get("quests") or [])} if snap else {}
                if qid in log:
                    role = "turn_in" if log[qid].get("complete") else "objective"
        out: List[Dict[str, Any]] = []
        if role in ("start", "turn_in"):
            group = d.get("starters" if role == "start" else "finishers") or {}
            for kind, key in (("npc", "npcs"), ("object", "objects")):
                for eid in (group.get(key) or [])[:3]:
                    out += spots(c, kind, eid, role)
        else:
            obj = d.get("objectives") or {}
            for kind, key in (("npc", "npcs"), ("object", "objects")):
                for o in (obj.get(key) or [])[:4]:
                    if "id" in o:
                        out += spots(c, kind, o["id"], "objective")
            for o in (obj.get("items") or [])[:4]:
                if "id" not in o:
                    continue
                for s in c.execute("SELECT source_kind, source_entity FROM world_drops WHERE item_id=? "
                                   "ORDER BY chance DESC LIMIT 2", (o["id"],)).fetchall():
                    out += spots(c, s["source_kind"], s["source_entity"], "objective", f"drops item {o['id']}")
        return out, role

    def solve(character_id, scope, from_map, from_x, from_y, to_map, to_x, to_y, npc_id, object_id, quest_id, role):
        if scope is not None:
            if character_id not in (None, scope):
                raise HTTPException(403, "this link only covers one character")
            character_id = str(scope)
        # where from
        if from_map is not None and from_x is not None and from_y is not None:
            start = {"map_id": from_map, "x": from_x, "y": from_y, "source": "given"}
        elif character_id:
            start = latest_position(character_id)
            if not start:
                raise HTTPException(404, "no known position for this character: give from_map, from_x, from_y")
        else:
            raise HTTPException(400, "give character_id, or from_map with from_x and from_y")
        start["map_name"] = map_name(start["map_id"])
        # where to
        warnings: List[str] = []
        if to_map is not None and to_x is not None and to_y is not None:
            cands = [{"kind": "point", "map_id": to_map, "x": to_x, "y": to_y, "role": "point"}]
            target = {"kind": "point"}
        else:
            with world.conn() as c:
                if npc_id is not None:
                    cands, target = spots(c, "npc", npc_id, "npc"), {"kind": "npc", "id": npc_id}
                elif object_id is not None:
                    cands, target = spots(c, "object", object_id, "object"), {"kind": "object", "id": object_id}
                elif quest_id is not None:
                    cands, used = quest_spots(c, quest_id, role, character_id)
                    target = {"kind": "quest", "id": quest_id, "role": used}
                else:
                    raise HTTPException(400, "give a destination: to_map/to_x/to_y, npc_id, object_id or quest_id")
            if not cands:
                raise HTTPException(404, "the world data has no location for that destination")
        ms = geo.rect(start["map_id"])
        a = geo.to_world(ms, start["x"], start["y"]) if ms else None

        def measure(cnd):
            m = geo.rect(cnd["map_id"])
            if a and m and m["instance_id"] == ms["instance_id"]:
                b = geo.to_world(m, cnd["x"], cnd["y"])
                return math.hypot(b[0] - a[0], b[1] - a[1])
            if cnd["map_id"] == start["map_id"]:
                return None
            return math.inf
        for cnd in cands:
            d = measure(cnd)
            cnd["distance_yards"] = None if d is None or d == math.inf else round(d)
            cnd["_d"] = d if d is not None else math.hypot(cnd["x"] - start["x"], cnd["y"] - start["y"]) * 1e-3
            cnd["map_name"] = map_name(cnd["map_id"])
        cands.sort(key=lambda c: c["_d"])
        goal = cands[0]
        for c in cands:
            c.pop("_d", None)
        out: Dict[str, Any] = {"from": start, "to": {**target, **goal}, "other_locations": cands[1:20],
                               "verified_passable": False, "note": NOTE}
        mg = geo.rect(goal["map_id"])
        if not ms or not mg:
            if goal["map_id"] == start["map_id"]:
                out.update(method="straight line on the map (no geometry for this map)", confidence="low",
                           waypoints=[{"map_id": start["map_id"], "x": start["x"], "y": start["y"], "walked": False},
                                      {"map_id": goal["map_id"], "x": goal["x"], "y": goal["y"], "walked": False}],
                           distance_yards=None)
            else:
                out.update(method="none", waypoints=[], distance_yards=None, confidence="none")
                warnings.append("沒有這兩張地圖的世界座標，無法跨地圖規劃。")
            out["warnings"] = warnings
            return out
        if mg["instance_id"] != ms["instance_id"]:
            out.update(method="none", waypoints=[], distance_yards=None, confidence="none")
            warnings.append(f"目的地在另一塊大陸（instance {mg['instance_id']}），需要搭船、飛艇或傳送，這裡不規劃。")
            out["warnings"] = warnings
            return out
        b = geo.to_world(mg, goal["x"], goal["y"])
        pts = plan(geo.walked(ms["instance_id"]), a, b)
        legs, total, on_walked, maps_seen = [], 0.0, 0.0, []
        for i, (wx, wy, w) in enumerate(pts):
            m = geo.where(ms["instance_id"], wx, wy, prefer=(start["map_id"], goal["map_id"])) or ms
            x, y = geo.to_map(m, wx, wy)
            if i:
                seg = math.hypot(wx - pts[i - 1][0], wy - pts[i - 1][1])
                total += seg
                on_walked += seg if w else 0
            if m["ui_map_id"] not in maps_seen:
                maps_seen.append(m["ui_map_id"])
            legs.append({"map_id": m["ui_map_id"], "x": round(x, 2), "y": round(y, 2), "walked": bool(w) if i else None})
        share = on_walked / total if total else 1.0
        if len(maps_seen) > 1:
            warnings.append("路線經過 " + " → ".join(f"{map_name(m) or m}（{m}）" for m in maps_seen)
                            + "，每個路點都標了所在地圖。")
        if share < 0.5:
            warnings.append("大部分路段沒有人走過，請以遊戲內地形為準。")
        out.update(
            method="grid search over the continent: walked ground (players' position history) cheap, the rest dear",
            waypoints=legs, maps=[{"map_id": m, "name": map_name(m)} for m in maps_seen],
            distance_yards=round(total), straight_yards=round(math.hypot(b[0] - a[0], b[1] - a[1])),
            walked_share=round(share, 2), confidence="high" if share > 0.8 else "medium" if share > 0.4 else "low",
            minutes={"walking": round(total / WALK / 60, 1), "mount_60": round(total / MOUNT60 / 60, 1),
                     "mount_100": round(total / MOUNT100 / 60, 1)},
            warnings=warnings)
        return out

    def params(character_id: Optional[str] = None,
               from_map: Optional[int] = None, from_x: Optional[float] = Query(None, ge=0, le=100),
               from_y: Optional[float] = Query(None, ge=0, le=100),
               to_map: Optional[int] = None, to_x: Optional[float] = Query(None, ge=0, le=100),
               to_y: Optional[float] = Query(None, ge=0, le=100),
               npc_id: Optional[int] = None, object_id: Optional[int] = None, quest_id: Optional[int] = None,
               role: str = Query("auto", pattern="^(auto|start|turn_in|objective)$")):
        return dict(character_id=character_id, from_map=from_map, from_x=from_x, from_y=from_y, to_map=to_map,
                    to_x=to_x, to_y=to_y, npc_id=npc_id, object_id=object_id, quest_id=quest_id, role=role)

    @app.get("/api/v1/world/route")
    def route(p: Dict[str, Any] = Depends(params), scope: Optional[str] = Depends(can_read)):
        """A suggested way from a character (its last known position, or `from_map`/`from_x`/`from_y`, e.g.
        read off an AI screenshot) to a destination: a point (`to_map`/`to_x`/`to_y`), an NPC (`npc_id`), an object
        (`object_id`) or a quest (`quest_id`; `role` auto picks where the character needs to go: the quest giver,
        the objectives, or where to hand it in). The nearest of the destination's locations is chosen; the others
        are listed. Waypoints are in walking order, each on the map it lies on, and say whether that leg follows
        ground characters have walked. There is no terrain data: a route is never verified passable
        (`verified_passable` is false), and legs with `walked: false` are straight guesses.
        `/world/route/image` draws the same route on the map."""
        out = solve(scope=scope, **p)
        q = "&".join(f"{k}={v}" for k, v in p.items() if v is not None and k != "role") + f"&role={p['role']}"
        out["image"] = "/api/v1/world/route/image?" + q
        return out

    @app.get("/api/v1/world/route/image")
    def route_image(p: Dict[str, Any] = Depends(params), facing: Optional[float] = Query(None, ge=0, le=360),
                    scope: Optional[str] = Depends(can_read)):
        """The route drawn on the map background, as JPEG: blue the start (with an arrow for `facing`, degrees
        clockwise from north as the AI screenshot shows it), red the destination and small red dots its other
        locations, green legs over walked ground, dashed orange guessed legs. Cropped to the route."""
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            raise HTTPException(503, "drawing needs Pillow on the server")
        out = solve(scope=scope, **p)
        data = draw(out, facing)
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    WIDTH, HEIGHT = 1200, 800

    def world_canvas(ms, pts, others):
        """The zone pictures the route crosses, pasted where they lie in the world, cropped to the route."""
        from PIL import Image
        inst = ms["instance_id"]

        def world(mid, x, y):
            m = geo.rect(mid)
            return geo.to_world(m, x, y) if m and m["instance_id"] == inst else None
        line = [(w, walked) for w, walked in ((world(m, x, y), walked) for m, x, y, walked in pts) if w]
        dots = [w for w in (world(*o) for o in others) if w]
        xs = [w[0] for w, _ in line]
        ys = [w[1] for w, _ in line]
        span = max(max(xs) - min(xs), (max(ys) - min(ys)) / 1.5, 400.0) * 1.35
        cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
        x_hi, x_lo = cx + span / 2, cx - span / 2                 # north and south edges
        y_hi, y_lo = cy + span * 0.75, cy - span * 0.75           # west and east edges
        scale = WIDTH / (y_hi - y_lo)                             # pixels per yard
        img = Image.new("RGB", (WIDTH, HEIGHT), (24, 26, 30))
        start_goal = {pts[0][0], pts[-1][0]}
        maps = [m for m in geo.maps().values() if m.get("x_min") is not None and m.get("instance_id") == inst
                and m.get("type") in (3, 6) and m["x_min"] < x_hi and m["x_max"] > x_lo and m["y_min"] < y_hi
                and m["y_max"] > y_lo and os.path.isfile(os.path.join(maps_dir, f"{m['ui_map_id']}.jpg"))]
        # bigger maps first, the start's and destination's last: where pictures overlap, those win
        if not maps:
            raise HTTPException(404, "no background picture for the maps this route crosses")
        maps.sort(key=lambda m: (m["ui_map_id"] in start_goal, -(m["x_max"] - m["x_min"]) * (m["y_max"] - m["y_min"])))
        for m in maps:
            with Image.open(os.path.join(maps_dir, f"{m['ui_map_id']}.jpg")) as pic:
                W, H = pic.size
                top, bottom = min(x_hi, m["x_max"]), max(x_lo, m["x_min"])
                left, right = min(y_hi, m["y_max"]), max(y_lo, m["y_min"])
                src = ((m["y_max"] - left) / (m["y_max"] - m["y_min"]) * W, (m["x_max"] - top) / (m["x_max"] - m["x_min"]) * H,
                       (m["y_max"] - right) / (m["y_max"] - m["y_min"]) * W, (m["x_max"] - bottom) / (m["x_max"] - m["x_min"]) * H)
                dst = (int((y_hi - left) * scale), int((x_hi - top) * scale), int((y_hi - right) * scale), int((x_hi - bottom) * scale))
                if dst[2] - dst[0] < 2 or dst[3] - dst[1] < 2:
                    continue
                piece = pic.convert("RGB").crop(tuple(int(v) for v in src)).resize((dst[2] - dst[0], dst[3] - dst[1]), Image.LANCZOS)
                img.paste(piece, dst[:2])
        to = lambda w: ((y_hi - w[1]) * scale, (x_hi - w[0]) * scale)   # noqa: E731
        return img, to, line, dots, 1 / scale

    def single_map(mid, pts, others):
        """No geometry for the map: its own picture, positions in percent, cropped to the route."""
        from PIL import Image
        path = os.path.join(maps_dir, f"{mid}.jpg")
        if not os.path.isfile(path):
            raise HTTPException(404, f"no background picture for map {mid}")
        img = Image.open(path).convert("RGB")
        W, H = img.size
        line = [((x / 100 * W, y / 100 * H), w) for m, x, y, w in pts if m == mid]
        dots = [(x / 100 * W, y / 100 * H) for m, x, y in others if m == mid]
        xs = [p[0] for p, _ in line] or [W / 2]
        ys = [p[1] for p, _ in line] or [H / 2]
        span = max(max(xs) - min(xs), max(ys) - min(ys), min(W, H) * 0.25)
        cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
        half = span / 2 + span * 0.18
        box = (max(0, int(cx - half * 1.5)), max(0, int(cy - half)), min(W, int(cx + half * 1.5)), min(H, int(cy + half)))
        scale = WIDTH / max(1, box[2] - box[0])
        img = img.crop(box).resize((int((box[2] - box[0]) * scale), int((box[3] - box[1]) * scale)), Image.LANCZOS)
        to = lambda p: ((p[0] - box[0]) * scale, (p[1] - box[1]) * scale)   # noqa: E731
        return img, to, line, dots, None

    def draw(r: Dict[str, Any], facing: Optional[float]) -> bytes:
        from PIL import Image, ImageDraw
        start, goal = r["from"], r["to"]
        pts = [(w["map_id"], w["x"], w["y"], w["walked"]) for w in r.get("waypoints") or []] or \
              [(start["map_id"], start["x"], start["y"], None), (goal["map_id"], goal["x"], goal["y"], False)]
        others = [(o["map_id"], o["x"], o["y"]) for o in r.get("other_locations") or []]
        ms = geo.rect(start["map_id"])
        if ms:
            img, to, line, dots, yards_per_px = world_canvas(ms, pts, others)
        else:
            img, to, line, dots, yards_per_px = single_map(start["map_id"], pts, others)
        d = ImageDraw.Draw(img)
        for p in dots:
            x, y = to(p)
            d.ellipse((x - 4, y - 4, x + 4, y + 4), fill=(220, 40, 40), outline=(0, 0, 0))
        for i in range(1, len(line)):
            (a, _), (b, walked) = line[i - 1], line[i]
            a, b = to(a), to(b)
            if walked:
                d.line((a, b), fill=(0, 0, 0), width=9)
                d.line((a, b), fill=(60, 220, 100), width=5)
            else:                                   # dashed: a guess
                n = max(1, int(math.hypot(b[0] - a[0], b[1] - a[1]) / 14))
                for k in range(0, n, 2):
                    p0 = (a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n)
                    p1 = (a[0] + (b[0] - a[0]) * min(n, k + 1) / n, a[1] + (b[1] - a[1]) * min(n, k + 1) / n)
                    d.line((p0, p1), fill=(0, 0, 0), width=8)
                    d.line((p0, p1), fill=(255, 170, 30), width=4)
        if line:
            x, y = to(line[0][0])
            if facing is not None:                  # bearing clockwise from north; the picture's north is up
                rad = math.radians(facing)
                tip = (x + 26 * math.sin(rad), y - 26 * math.cos(rad))
                d.line(((x, y), tip), fill=(0, 0, 0), width=7)
                d.line(((x, y), tip), fill=(80, 170, 255), width=4)
            d.ellipse((x - 9, y - 9, x + 9, y + 9), fill=(80, 170, 255), outline=(0, 0, 0), width=2)
            gx, gy = to(line[-1][0])
            d.ellipse((gx - 10, gy - 10, gx + 10, gy + 10), fill=(230, 40, 40), outline=(255, 255, 255), width=3)
        legend = ["start (blue)  destination (red)  other spots (red dots)",
                  "green: walked by players   orange dashed: guessed, not verified"]
        if r.get("distance_yards"):
            legend.append(f"about {r['distance_yards']} yards, {r['minutes']['walking']} min on foot"
                          f" / {r['minutes']['mount_60']} min mounted")
        if yards_per_px:                            # a 100-yard scale bar
            bar = 100 / yards_per_px
            if 20 < bar < img.width / 2:
                d.line(((14, img.height - 18), (14 + bar, img.height - 18)), fill=(255, 255, 255), width=4)
                d.text((20 + bar, img.height - 26), "100 yd", fill=(255, 255, 255))
        th = 14 * len(legend) + 10
        d.rectangle((8, 8, 470, 8 + th), fill=(0, 0, 0))
        for i, t in enumerate(legend):
            d.text((14, 12 + 14 * i), t, fill=(255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=88)
        return buf.getvalue()

    return geo
