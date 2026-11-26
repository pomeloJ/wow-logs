"""Map descriptions and routes: geometry from the client's tables, routes over walked ground."""
import io
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import AUTH, READ_TOKEN, TOKEN

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from world_importer.__main__ import main as importer_main  # noqa: E402
from test_world import FILES  # noqa: E402

READ = {"Authorization": f"Bearer {READ_TOKEN}"}
UIMAP = '''Name_lang,ID,ParentUiMapID,Flags,System,Type,BountySetID
"Eastern Kingdoms",1415,947,0,0,2,0
"Elwynn Forest",1429,1415,2,0,3,0
Westfall,1436,1415,2,0,3,0
Kalimdor,1414,947,0,0,2,0
Durotar,1411,1414,2,0,3,0
'''
ASSIGN = '''UiMin_0,UiMin_1,UiMax_0,UiMax_1,Region_0,Region_1,Region_2,Region_3,Region_4,Region_5,ID,UiMapID,OrderIndex,MapID,AreaID
0,0,1,1,-16000,-19199.900390625,-1000000,7466.6000976562,16000,1000000,46725,1415,0,0,0
0,0,1,1,-10254.166015625,-1935.4166259766,-1000000,-7939.5830078125,1535.4166259766,1000000,46739,1429,0,0,12
0,0,1,1,-11733.333007812,-483.33331298828,-1000000,-9400,3016.6665039062,1000000,46746,1436,0,0,40
0,0,0.5,0.5,-1,-1,0,1,1,0,46747,1436,1,0,40
0,0,1,1,-1716.6666259766,-7249.9995117188,-1000000,1808.3332519531,-1962.4998779297,1000000,46721,1411,0,1,14
'''


@pytest.fixture
def nav(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    src = tmp_path / "src"
    for rel, body in FILES.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(body, encoding="utf-8")
    out = tmp_path / "world.db"
    assert importer_main(["import", "--source", "norgannondb", "--path", str(src), "--out", str(out)]) == 0
    csvs = tmp_path / "csv"
    csvs.mkdir()
    (csvs / "UiMap.csv").write_text(UIMAP, encoding="utf-8")
    (csvs / "UiMapAssignment.csv").write_text(ASSIGN, encoding="utf-8")
    assert importer_main(["uimaps", "--dir", str(csvs), "--out", str(out)]) == 0
    return TestClient(create_app(str(tmp_path / "server.db"), TOKEN, READ_TOKEN, str(out)))


def jpeg(w=600, h=400):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (40, 90, 40)).save(buf, "JPEG")
    return buf.getvalue()


def walk(client, cid, points, start=None, step=10):
    """Position samples a walker would leave, `step` seconds apart."""
    t = start or datetime(2026, 11, 20, tzinfo=timezone.utc)
    evs = []
    for i, (m, x, y) in enumerate(points):
        evs.append({"event_id": f"{cid}:{i}:{x}:{y}", "character_id": cid, "event_type": "LOCATION_SAMPLE",
                    "timestamp": (t + timedelta(seconds=step * i)).isoformat(), "payload": {"map_id": m, "x": x, "y": y}})
    r = client.post("/api/v1/events/batch", headers=AUTH, json={"schema_version": 1, "device_id": "pc", "events": evs})
    assert r.status_code == 200, r.text


def test_map_description_has_geometry_image_and_rules(nav):
    nav.put("/api/v1/maps/1436/image?name=Westfall", headers=AUTH, content=jpeg())
    m = nav.get("/api/v1/world/map?map_id=1436", headers=READ).json()
    assert m["name"] == "西部荒野" and m["name_en"] == "Westfall" and m["type"] == "zone"
    assert m["parent"]["map_id"] == 1415 and m["continent"]["map_id"] == 1415 and m["instance_id"] == 0
    assert m["image"]["available"] and (m["image"]["width"], m["image"]["height"]) == (600, 400)
    assert m["world"]["width_yards"] == 3500.0 and m["world"]["height_yards"] == 2333.3
    assert "percent" in m["coordinates"]["system"] and "UiMapID" in m["coordinates"]["ids"]
    assert nav.get("/api/v1/world/map?q=westf", headers=READ).json()["map_id"] == 1436
    assert nav.get("/api/v1/world/map?q=西部", headers=READ).json()["map_id"] == 1436
    assert nav.get("/api/v1/world/map?map_id=999999", headers=READ).status_code == 404
    assert nav.get("/api/v1/world/map?map_id=1436").status_code in (401, 403)


def test_route_to_an_npc_picks_the_nearest_spawn_and_measures_yards(nav):
    r = nav.get("/api/v1/world/route?from_map=1436&from_x=50&from_y=50&npc_id=547", headers=READ).json()
    assert r["to"]["name"] == "巨型血牙野豬" and (r["to"]["x"], r["to"]["y"]) == (52.5, 53.4)
    assert [(o["x"], o["y"]) for o in r["other_locations"]][:2] == [(55, 55), (60, 60)]
    assert 80 < r["straight_yards"] < 160 and r["distance_yards"] >= r["straight_yards"]
    assert r["verified_passable"] is False and r["walked_share"] == 0 and r["confidence"] == "low"
    assert r["waypoints"][0] == {"map_id": 1436, "x": 50.0, "y": 50.0, "walked": None}
    assert r["minutes"]["walking"] > r["minutes"]["mount_60"]
    assert "不保證" in r["note"]


def test_route_follows_walked_ground_and_crosses_zones(nav):
    # a walker went from Westfall east into Elwynn along a bend (north, then east)
    pts = [(1436, 30, 70 - i) for i in range(0, 30)] + [(1436, 30 + i, 40) for i in range(1, 50)]
    walk(nav, "w1", pts)
    r = nav.get("/api/v1/world/route?from_map=1436&from_x=30&from_y=70&to_map=1436&to_x=79&to_y=40", headers=READ).json()
    assert r["walked_share"] > 0.8 and r["confidence"] == "high"
    xs = [(w["x"], w["y"]) for w in r["waypoints"]]
    assert any(abs(x - 30) < 3 and abs(y - 40) < 3 for x, y in xs)     # it goes round the bend, not straight across
    # into the next zone: every waypoint names its own map
    r = nav.get("/api/v1/world/route?from_map=1436&from_x=60&from_y=40&to_map=1429&to_x=30&to_y=60", headers=READ).json()
    assert r["waypoints"][-1]["map_id"] == 1429 and r["waypoints"][0]["map_id"] == 1436
    assert [m["map_id"] for m in r["maps"]] == [1436, 1429]


def test_flights_are_not_walked_ground(nav):
    walk(nav, "f1", [(1436, 10 + i * 8, 20) for i in range(10)], step=10)     # ~280 yards per 10 s
    r = nav.get("/api/v1/world/route?from_map=1436&from_x=10&from_y=20&to_map=1436&to_x=80&to_y=20", headers=READ).json()
    assert r["walked_share"] == 0


def test_other_continent_is_not_planned(nav):
    r = nav.get("/api/v1/world/route?from_map=1436&from_x=50&from_y=50&to_map=1411&to_x=50&to_y=50", headers=READ).json()
    assert r["waypoints"] == [] and "另一塊大陸" in r["warnings"][0]


def test_route_from_the_characters_position_and_quest_role(nav):
    now = datetime.now(timezone.utc).isoformat()
    nav.put("/api/v1/characters/c1/snapshot", headers=AUTH, json={"captured_at": now, "device_id": "pc", "data": {
        "name": "小德", "map_id": 1436, "x": 50, "y": 40, "quests": [{"quest_id": 10, "complete": False}]}})
    r = nav.get("/api/v1/world/route?character_id=c1&quest_id=10", headers=READ).json()
    assert r["from"]["source"].startswith("character snapshot") and r["to"]["role"] == "objective"
    assert r["to"]["name"] == "巨型血牙野豬"
    r = nav.get("/api/v1/world/route?character_id=c1&quest_id=10&role=turn_in", headers=READ).json()
    assert r["to"]["name"] == "薩爾瑪·薩丁" and (r["to"]["x"], r["to"]["y"]) == (56.4, 31.6)
    # a share link for one character routes only that character
    tok = nav.post("/api/v1/characters/c1/voice-token", headers=AUTH).json()["token"]
    S = {"Authorization": f"Bearer {tok}"}
    assert nav.get("/api/v1/world/route?quest_id=10", headers=S).json()["to"]["role"] == "objective"
    assert nav.get("/api/v1/world/route?character_id=c2&quest_id=10", headers=S).status_code == 403
    assert nav.get("/api/v1/world/route?character_id=nobody&npc_id=547", headers=READ).status_code == 404


def test_route_image_is_drawn_on_the_map(nav):
    from PIL import Image
    nav.put("/api/v1/maps/1436/image?name=Westfall", headers=AUTH, content=jpeg())
    r = nav.get("/api/v1/world/route/image?from_map=1436&from_x=40&from_y=40&npc_id=235&facing=90", headers=READ)
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    img = Image.open(io.BytesIO(r.content))
    assert img.width == 1200
    assert nav.get("/api/v1/world/route?from_map=1436&from_x=40&from_y=40&npc_id=235", headers=READ).json()["image"] \
        .startswith("/api/v1/world/route/image?")
    # no picture for the map: said so
    assert nav.get("/api/v1/world/route/image?from_map=1429&from_x=40&from_y=40&to_map=1429&to_x=50&to_y=50",
                   headers=READ).status_code == 404


def test_addon_map_rects_fill_maps_missing_from_the_tables(nav):
    ev = {"event_id": "r1", "character_id": "c9", "event_type": "MAP_RECT", "timestamp": "2026-11-20T00:00:00+00:00",
          "payload": {"map_id": 2482, "map_name": "Mount Hyjal", "continent_id": 1, "map_type": 3, "parent_map_id": 1414,
                      "world_x0": 6000, "world_y0": -3000, "world_x1": 3000, "world_y1": -7500}}
    assert nav.post("/api/v1/events/batch", headers=AUTH, json={"schema_version": 1, "device_id": "pc", "events": [ev]}).status_code == 200
    from app import navigate  # noqa: F401
    m = nav.get("/api/v1/world/map?map_id=2482", headers=READ).json()
    assert m["world"]["height_yards"] == 3000 and m["world"]["width_yards"] == 4500
    assert "addon" in m["sources"]["geometry"]


def test_limited_sharing_token_without_location_cannot_route(nav):
    from app.main import create_app  # noqa: F401
    assert "/api/v1/world/route" in __import__("app.sections", fromlist=["ROUTES"]).ROUTES
