import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from conftest import AUTH, READ_TOKEN, TOKEN

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from world_importer import create  # noqa: E402
from world_importer.__main__ import main as importer_main  # noqa: E402
from world_importer.norgannon import assignments, import_into, records  # noqa: E402

READ = {"Authorization": f"Bearer {READ_TOKEN}"}
HEAD = "-- generated\n"

FILES = {
    "NorgannonDB.toc": "## Interface: 16001\n## Version: 9.9.9\n",
    "data/info.lua": HEAD + 'NorgannonDB.info = {build="1.60.1",flavor="Forever",generated="2026-11-19",'
                            'interface=16001,sources={{license="GPL-3.0",name="QuestieDB",version="1.1.3"}}}\n',
    "data/world.lua": HEAD
    + 'do local function f() NorgannonDB.zones = {[40]={name="Westfall",uiMapId=1436},'
      '[108]={name="Sentinel Hill",parent=40},[12]={name="Elwynn Forest",uiMapId=1429}} end f() end\n'
    + 'do local function f() NorgannonDB.maps = {areaToUiMap={[40]=1436,[12]=1429}} end f() end\n'
    + 'do local function f() NorgannonDB.professions = {[185]="Cooking"} end f() end\n',
    "data/quests.lua": HEAD + 'local R = NorgannonDB:_PackedLoader("quests")\ndo local function f(R)\n'
    + 'R(10,"Boar \\"Liver\\" Pie",[[finishers={npcs={235}},level=12,minLevel=9,nextQuestInChain=11,'
      'objectives={items={{id=723}},npcs={{id=547}}},prerequisites={any={9}},'
      'rewards={items={769},xp=850,xpConfirmed=true,xpLevel=12},starters={npcs={235}},'
      'text={description="Bring ]] livers.",objectives={"Get 8 livers."}},zoneOrSort=40]])\n'
    + 'R(9,"First Steps",[[level=10,starters={objects={31}},zoneOrSort=-61]])\n'
    + 'R(11,"Hidden One",[[hidden=true,hiddenReason="questie",level=12,zoneOrSort=40]])\n'
    + 'R(12,nil,[[level=60]])\nend f(R) end\n',
    "data/npcs.lua": HEAD + 'local R = NorgannonDB:_PackedLoader("npcs")\ndo local function f(R)\n'
    + 'R(547,"Great Goretusk",[[drops={[723]=36.96,[769]=77.25,[900]=0},maxLevel=17,minLevel=16,respawn=300,'
      'spawns={[40]={{52.5,53.4},{60,60,120}},[108]={{55,55}},[999]={{1,1}},[12]={{-1,-1}}},zone=40]])\n'
    + 'R(235,"Salma Saldean",[[maxLevel=10,minLevel=10,questEnds={10},questStarts={10},reaction="A",'
      'sells={[769]=0},spawns={[40]={{56.4,31.6}}},subName="Cook",zone=40]])\n'
    + 'R(18232,nil)\nend f(R) end\n',
    "data/objects.lua": HEAD + 'local R = NorgannonDB:_PackedLoader("objects")\ndo local function f(R)\n'
    + 'R(31,"Old Lion Statue",[[questStarts={9},spawns={[40]={{53,53.5}}},zone=40]])\nR(38,"Chest")\nend f(R) end\n',
    "data/items.lua": HEAD + 'local R = NorgannonDB:_PackedLoader("items")\ndo local function f(R)\n'
    + 'R(723,"Goretusk Liver",[[class=12,level=1]])\nR(769,"Chunk of Boar Meat",[[class=7,level=5,rewardOf={10}]])\n'
    + 'R(900,"Mystery")\nend f(R) end\n',
    "data/recipes.lua": HEAD
    + 'do local function f() NorgannonDB.recipes = {[2152]={auto=true,item=769,learn=1,levels={1,30,45,60},'
      'name="Boar Roast",reagents={{723,2},{900,1}},skill=185},[3000]={count=5,item=900,learn=50,'
      'levels={50,70,80,90},name="Mystery Dust",reagents={{723,1}},skill=185,trainers={[235]=100}}} end f() end\n'
    + 'do local function f() NorgannonDB.index.recipes = {craftedBy={[769]={2152}}} end f() end\n',
    "locales/zhTW.lua": HEAD + 'if GetLocale() ~= "zhTW" then return end\n'
    + 'assert(loadstring([=[local L = {items={[723]="血牙野豬的肝",[769]="大塊野豬肉"},'
      'npcs={[547]={name="巨型血牙野豬"},[235]={name="薩爾瑪·薩丁",subName="廚師"}},'
      'objects={[31]="舊獅子雕像"},professions={[185]="烹飪"},zones={[40]="西部荒野",[108]="哨兵嶺"}}\n'
    + 'L.quests = {}\nlocal R = NorgannonDB:_PackedLoader(L.quests)\ndo local function f(R)\n'
    + 'R(10,"野豬肝餡餅",[[description="帶八個肝來。",objectives={"收集八個血牙野豬的肝。"}]])\nend f(R) end\n]=]))()\n',
}


@pytest.fixture
def source(tmp_path):
    src = tmp_path / "src"
    for rel, body in FILES.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(body, encoding="utf-8")
    return src


@pytest.fixture
def world_client(tmp_path, source):
    from fastapi.testclient import TestClient
    from app.main import create_app
    out = tmp_path / "world.db"
    assert importer_main(["import", "--source", "norgannondb", "--path", str(source), "--out", str(out)]) == 0
    return TestClient(create_app(str(tmp_path / "server.db"), TOKEN, READ_TOKEN, str(out)))


def test_record_parser_handles_quotes_brackets_and_nil_names():
    recs = list(records(FILES["data/quests.lua"]))
    assert [r[0] for r in recs] == [10, 9, 11, 12]
    assert recs[0][1] == 'Boar "Liver" Pie'
    assert recs[0][2]["text"]["description"] == "Bring ]] livers."     # "]]" inside a string
    assert recs[3][1] is None and recs[3][2] == {"level": 60}
    assert list(records('R(38,"Chest")\n')) == [(38, "Chest", {})]


def test_parser_never_executes_code():
    from adventuresync.luaparser import LuaParseError
    with pytest.raises(LuaParseError):
        list(records('R(1,"x",[[level=os.exit(1)]])\n'))
    with pytest.raises(LuaParseError):
        assignments("NorgannonDB.info = (function() return {} end)()")


def test_import_contents(tmp_path, source):
    conn = create(str(tmp_path / "w.db"))
    counts = import_into(conn, source, log=lambda *_: None)
    assert counts["quests"] == 4 and counts["npcs"] == 3 and counts["items"] == 3
    one = lambda sql: conn.execute(sql).fetchall()  # noqa: E731
    assert one("SELECT name, version, game_build, license FROM world_data_sources") == \
        [("NorgannonDB", "9.9.9", "1.60.1", "GPL-3.0")]
    # the subzone resolves to its parent's map; unknown areas keep no map; {-1,-1} is dropped
    assert one("SELECT area_id, ui_map_id, x, y, respawn FROM world_spawns WHERE entity_id=547 ORDER BY id") == \
        [(40, 1436, 52.5, 53.4, 300), (40, 1436, 60, 60, 120), (108, 1436, 55, 55, 300), (999, None, 1, 1, 300)]
    assert one("SELECT name_zh, area_id, ui_map_id, hidden, xp, xp_confirmed, next_quest "
               "FROM world_quests WHERE quest_id=10") == [("野豬肝餡餅", 40, 1436, 0, 850, 1, 11)]
    assert one("SELECT area_id, ui_map_id FROM world_quests WHERE quest_id=9") == [(None, None)]  # QuestSort
    assert one("SELECT chance FROM world_drops WHERE source_entity=547 ORDER BY item_id") == \
        [(36.96,), (77.25,), (0,)]
    assert one("SELECT name_zh, sub_name_zh FROM world_npcs WHERE npc_id=235") == [("薩爾瑪·薩丁", "廚師")]


def test_failed_import_keeps_the_old_database(tmp_path, source):
    out = tmp_path / "world.db"
    assert importer_main(["import", "--source", "norgannondb", "--path", str(source), "--out", str(out)]) == 0
    before = out.read_bytes()
    (source / "data/npcs.lua").write_text('R(1,"broken",[[level=]])\n', encoding="utf-8")
    assert importer_main(["import", "--source", "norgannondb", "--path", str(source), "--out", str(out)]) == 1
    assert out.read_bytes() == before and not out.with_suffix(".tmp").exists()


def test_missing_world_database(client):
    assert client.get("/api/v1/world/sources", headers=READ).json() == {"available": False, "items": []}
    assert client.get("/api/v1/world/quests", headers=READ).status_code == 503
    assert client.get("/api/v1/health").status_code == 200           # the rest keeps working


def test_world_api_needs_a_token_and_is_read_only(world_client, tmp_path):
    assert world_client.get("/api/v1/world/quests").status_code == 401
    src = world_client.get("/api/v1/world/sources", headers=READ).json()
    assert src["available"] and src["items"][0]["version"] == "9.9.9" and src["items"][0]["counts"]["quests"] == 4
    with pytest.raises(sqlite3.OperationalError):
        sqlite3.connect(f"file:{tmp_path / 'world.db'}?mode=ro", uri=True).execute("DELETE FROM world_quests")


def test_quest_search_and_detail(world_client):
    get = lambda url: world_client.get("/api/v1/world/" + url, headers=READ).json()  # noqa: E731
    assert [q["id"] for q in get("quests?map_id=1436")["items"]] == [10]        # hidden ones are left out
    assert [q["id"] for q in get("quests?map_id=1436&include_hidden=1")["items"]] == [10, 11]
    assert [q["id"] for q in get("quests?q=餡餅")["items"]] == [10]
    assert [q["id"] for q in get("quests?q=liver")["items"]] == [10]
    assert [q["id"] for q in get("quests?q=9")["items"]] == [9]
    assert get("quests?q=100%25")["total"] == 0                                 # % is not a wildcard
    q = get("quests/10")
    assert q["name"] == "野豬肝餡餅" and q["zone"] == "西部荒野" and q["description"] == "帶八個肝來。"
    assert q["finishers"][0]["name"] == "薩爾瑪·薩丁"
    assert q["finishers"][0]["location"] == {"map_id": 1436, "area_id": 40, "zone": "西部荒野",
                                             "x": 56.4, "y": 31.6, "respawn": None}
    kinds = {o["kind"]: o for o in q["objectives"]}
    assert kinds["npc"]["name"] == "巨型血牙野豬"
    assert kinds["item"]["dropped_by"][0]["name"] == "巨型血牙野豬" and kinds["item"]["dropped_by"][0]["chance"] == 36.96
    assert q["prerequisites"] == [{"id": 9, "name": "First Steps", "level": 10}]
    assert q["next_quest"]["id"] == 11 and q["rewards"]["items"] == [{"id": 769, "name": "大塊野豬肉"}]
    assert get("quests/9")["starters"][0] == {"kind": "object", "id": 31, "name": "舊獅子雕像", "location": {
        "map_id": 1436, "area_id": 40, "zone": "西部荒野", "x": 53, "y": 53.5, "respawn": None}}
    assert world_client.get("/api/v1/world/quests/777", headers=READ).status_code == 404


def test_quest_status_is_unknown_unless_recorded(world_client):
    now = datetime.now(timezone.utc).isoformat()
    snap = {"captured_at": now, "device_id": "mac", "data": {"name": "小德", "quests": [{"quest_id": 10}]}}
    world_client.put("/api/v1/characters/c1/snapshot", json=snap, headers=AUTH)
    ev = {"event_id": "a:1", "character_id": "c1", "event_type": "QUEST_COMPLETED", "timestamp": now,
          "payload": {"quest_id": 9}}
    world_client.post("/api/v1/events/batch", headers=AUTH,
                      json={"schema_version": 1, "device_id": "mac", "events": [ev]})
    status = lambda q: world_client.get(f"/api/v1/world/quests/{q}?character_id=c1",  # noqa: E731
                                        headers=READ).json()["status"]
    assert (status(10), status(9), status(11)) == ("in_log", "completed", "unknown")
    assert "status" not in world_client.get("/api/v1/world/quests/10", headers=READ).json()


def test_npcs_items_and_maps(world_client):
    get = lambda url: world_client.get("/api/v1/world/" + url, headers=READ).json()  # noqa: E731
    assert [n["id"] for n in get("npcs?q=野豬")["items"]] == [547]
    assert [n["id"] for n in get("npcs?map_id=1436")["items"]] == [235, 547]
    n = get("npcs/547")
    assert n["name"] == "巨型血牙野豬" and n["spawn_count"] == 4 and n["rank"] == "normal"
    assert n["drops"] == [{"id": 769, "name": "大塊野豬肉", "chance": 77.25},
                          {"id": 723, "name": "血牙野豬的肝", "chance": 36.96},
                          {"id": 900, "name": "Mystery", "chance": None}]   # 0 = chance unknown
    seller = get("npcs/235")
    assert seller["title"] == "廚師" and seller["sells"] == [{"id": 769, "name": "大塊野豬肉"}]
    assert seller["quest_starts"][0]["name"] == "野豬肝餡餅"
    it = get("items/769")
    assert it["dropped_by"][0]["name"] == "巨型血牙野豬" and it["sold_by"][0]["name"] == "薩爾瑪·薩丁"
    assert it["reward_of"][0]["id"] == 10
    assert [i["id"] for i in get("items?q=肝")["items"]] == [723]
    assert [(m["map_id"], m["name"]) for m in get("maps")["items"]] == [(1429, "Elwynn Forest"), (1436, "西部荒野")]
    assert len(get("maps?zones_only=false")["items"]) == 3


def test_nearby(world_client):
    get = lambda url: world_client.get("/api/v1/world/nearby?" + url, headers=READ).json()["items"]  # noqa: E731
    near = get("map_id=1436&x=53&y=53&radius=3")
    assert [(e["kind"], e["id"]) for e in near] == [("object", 31), ("npc", 547)]     # nearest first
    assert near[1]["name"] == "巨型血牙野豬" and near[1]["spawns_in_range"] == 2 and near[1]["distance"] == 0.64
    assert [e["id"] for e in get("map_id=1436&x=53&y=53&radius=3&kind=npc")] == [547]
    assert [e["id"] for e in get("map_id=1436&x=53&y=53&radius=3&quest_only=1")] == [31]
    assert near[0]["quest_starts"] == [{"id": 9, "name": "First Steps", "level": 10, "min_level": None}]
    cook = get("map_id=1436&x=56&y=32&radius=2")[0]
    assert cook["quest_ends"] == [{"id": 10, "name": "野豬肝餡餅", "level": 12, "min_level": 9}]
    assert [e["id"] for e in get("map_id=1436&x=53&y=53&radius=30")] == [31, 547, 235]
    assert get("map_id=1429&x=53&y=53&radius=30") == []                               # another map
    assert world_client.get("/api/v1/world/nearby?map_id=1&x=500&y=1", headers=READ).status_code == 422


def test_voice_uses_world_names(world_client):
    now = datetime.now(timezone.utc).isoformat()
    snap = {"captured_at": now, "device_id": "mac", "data": {
        "name": "小德", "equipment": [{"slot": 5, "item_id": 769}],
        "quests": [{"quest_id": 10, "title": "野豬肝餡餅", "level": 12, "complete": False}]}}
    world_client.put("/api/v1/characters/c1/snapshot", json=snap, headers=AUTH)
    tok = world_client.post("/api/v1/characters/c1/voice-token", headers=AUTH).json()["token"]
    text = world_client.get(f"/voice?t={tok}&show=gear,quests").text
    assert "胸部：大塊野豬肉" in text and "交給 薩爾瑪·薩丁（西部荒野）" in text


def test_share_token_cannot_ask_quest_status_of_another_character(world_client):
    now = datetime.now(timezone.utc).isoformat()
    for cid in ("c1", "c2"):
        world_client.put(f"/api/v1/characters/{cid}/snapshot", headers=AUTH,
                         json={"captured_at": now, "device_id": "mac", "data": {"name": cid}})
    tok = world_client.post("/api/v1/characters/c1/voice-token", headers=AUTH).json()["token"]
    S = {"Authorization": f"Bearer {tok}"}
    assert world_client.get("/api/v1/world/quests/10?character_id=c1", headers=S).json()["status"] == "unknown"
    assert world_client.get("/api/v1/world/quests/10?character_id=c2", headers=S).status_code == 403
    assert world_client.get("/api/v1/world/npcs/547", headers=S).status_code == 200


def test_quest_pins(world_client):
    now = datetime.now(timezone.utc).isoformat()
    quests = [{"quest_id": 10, "title": "野豬肝餡餅", "complete": False}, {"quest_id": 424242, "title": "未知"}]
    world_client.put("/api/v1/characters/c1/snapshot", headers=AUTH,
                     json={"captured_at": now, "device_id": "mac", "data": {"name": "小德", "quests": quests}})
    pins = world_client.get("/api/v1/world/quest-pins?character_id=c1", headers=READ).json()["items"]
    turn = [p for p in pins if p["role"] == "turn_in"]
    assert [(p["name"], p["map_id"], p["x"], p["y"], p["complete"]) for p in turn] == \
        [("薩爾瑪·薩丁", 1436, 56.4, 31.6, False)]
    targets = [p for p in pins if p["role"] == "target"]
    assert {p["name"] for p in targets} == {"巨型血牙野豬"} and all(p["map_id"] == 1436 for p in targets)
    assert len(targets) == 6                       # 3 mapped spawns as kill target + 3 as liver dropper
    assert {p["note"] for p in targets} == {None, "血牙野豬的肝"}
    # a quest ready to hand in only shows where to hand it in
    quests[0]["complete"] = True
    later = datetime.now(timezone.utc).replace(year=2099).isoformat()
    world_client.put("/api/v1/characters/c1/snapshot", headers=AUTH,
                     json={"captured_at": later, "device_id": "mac", "data": {"name": "小德", "quests": quests}})
    pins = world_client.get("/api/v1/world/quest-pins?character_id=c1", headers=READ).json()["items"]
    assert [p["role"] for p in pins] == ["turn_in"] and pins[0]["complete"] is True
    tok = world_client.post("/api/v1/characters/c1/voice-token", headers=AUTH).json()["token"]
    S = {"Authorization": f"Bearer {tok}"}
    assert world_client.get("/api/v1/world/quest-pins?character_id=c1", headers=S).status_code == 200
    assert world_client.get("/api/v1/world/quest-pins?character_id=other", headers=S).status_code == 403


def test_item_details_for_bags(world_client):
    get = lambda url: world_client.get("/api/v1/world/" + url, headers=READ).json()  # noqa: E731
    liver = get("items/723")
    assert liver["kind"] == "任務物品" and liver["needed_by"] == [{"id": 10, "name": "野豬肝餡餅", "level": 12}]
    assert [(r["name"], r["skill"], r["learn"], r["uses"]) for r in liver["reagent_for"]] == \
        [("大塊野豬肉", "烹飪", 1, 2), ("Mystery", "烹飪", 50, 1)]
    assert liver["reagent_for_total"] == 2 and liver["crafted_by"] == []
    meat = get("items/769")
    assert meat["kind"] == "商品材料" and meat["needed_by"] == []
    made = meat["crafted_by"][0]
    assert made["source"] == "auto" and made["levels"] == [1, 30, 45, 60] and made["makes"] == 1
    assert made["reagents"] == [{"id": 723, "name": "血牙野豬的肝", "count": 2}, {"id": 900, "name": "Mystery", "count": 1}]
    dust = get("items/900")
    assert dust["kind"] is None and dust["crafted_by"][0]["makes"] == 5 and dust["crafted_by"][0]["source"] == "trainer"
    names = get("names?items=723,769,555")
    assert names["items"] == {"723": "血牙野豬的肝", "769": "大塊野豬肉"} and names["needed_by"] == {"723": [10]}
    assert get("names?items=")["needed_by"] == {}
    assert get("sources")["items"][0]["counts"]["recipes"] == 2


def test_sources_carry_level_zones_and_spawn_points(world_client):
    get = lambda url: world_client.get("/api/v1/world/" + url, headers=READ).json()  # noqa: E731
    src = get("items/723")["dropped_by"][0]
    assert (src["name"], src["chance"], src["min_level"], src["max_level"], src["rank"]) == \
        ("巨型血牙野豬", 36.96, 16, 17, "normal")
    assert src["zones"] == [{"map_id": 1436, "zone": "西部荒野", "spawns": 3}]   # the unmapped spawn is left out
    assert get("items/723")["dropped_by_total"] == 1
    assert get("items/769")["sold_by"][0]["zones"][0]["zone"] == "西部荒野"
    assert get("items/900")["dropped_by"][0]["chance"] is None                 # 0 in the data = unknown
    sp = get("spawns?kind=npc&id=547")
    assert sp["name"] == "巨型血牙野豬" and sp["spawn_count"] == 4 and len(sp["spawns"]) == 3
    assert sp["spawns"][0] == {"map_id": 1436, "x": 52.5, "y": 53.4}
    assert get("spawns?kind=object&id=31")["zones"] == [{"map_id": 1436, "zone": "西部荒野", "spawns": 1}]
    assert world_client.get("/api/v1/world/spawns?kind=npc&id=1", headers=READ).status_code == 404
    assert world_client.get("/api/v1/world/spawns?kind=item&id=1", headers=READ).status_code == 422


EXTRA_QUESTS = (
    'R(20,"Horde Only",[[level=12,races=178,zoneOrSort=40]])\n'
    'R(21,"Druid Thing",[[classes=1024,level=10,races=8589934669,rewards={xp=1000,xpLevel=10},zoneOrSort=40]])\n'
    'R(22,"Seasonal",[[event=true,level=10,zoneOrSort=40]])\n'
    'R(23,"Daily",[[level=10,specialFlags=1,zoneOrSort=40]])\n'
    'R(24,"High",[[level=30,minLevel=28,rewards={xp=3000,xpLevel=30},zoneOrSort=40]])\n'
    'R(25,"Old",[[level=14,rewards={items={769},xp=1000,xpLevel=14},starters={npcs={235}},zoneOrSort=40]])\n'
    'R(26,"Either A",[[exclusiveTo={27},level=12,zoneOrSort=40]])\n'
    'R(27,"Either B",[[exclusiveTo={26},level=12,zoneOrSort=40]])\n'
    'R(28,"Chain 2",[[level=20,prerequisites={all={24}},rewards={xp=2000,xpLevel=20},zoneOrSort=40]])\n'
    'R(29,"Mage Thing",[[classes=128,level=10,zoneOrSort=40]])\n')


def test_zone_quest_progress(tmp_path, source):
    from fastapi.testclient import TestClient
    from app.main import create_app
    path = source / "data/quests.lua"
    path.write_text(path.read_text(encoding="utf-8").replace("end f(R) end", EXTRA_QUESTS + "end f(R) end"), encoding="utf-8")
    out = tmp_path / "world.db"
    assert importer_main(["import", "--source", "norgannondb", "--path", str(source), "--out", str(out)]) == 0
    client = TestClient(create_app(str(tmp_path / "server.db"), TOKEN, READ_TOKEN, str(out)))
    data = {"name": "小德", "level": 22, "race": "NightElf", "race_id": 4, "class": "DRUID",
            "quests": [{"quest_id": 10, "title": "野豬肝餡餅"}], "quests_done": [9, 26]}
    client.put("/api/v1/characters/c1/snapshot", headers=AUTH,
               json={"captured_at": datetime.now(timezone.utc).isoformat(), "device_id": "mac", "data": data})
    get = lambda url: client.get("/api/v1/world/" + url, headers=READ).json()  # noqa: E731

    z = get("zone-quests?character_id=c1&map_id=1436")
    assert z["known"] is True and z["zone"] == "西部荒野"
    # worth doing now first (most XP at this level), then log, too low, locked, done.
    # Left out: other side (20), other class (29), seasonal (22), repeatable (23), hidden (11),
    # and the branch closed by having done its alternative (27).
    assert [(q["id"], q["state"]) for q in z["items"]] == [
        (21, "available"), (25, "available"), (10, "in_log"), (24, "too_low"), (28, "locked"), (26, "completed")]
    by = {q["id"]: q for q in z["items"]}
    assert (by[21]["xp"], by[21]["xp_now"], by[21]["xp_share"]) == (1000, 500, 0.5)      # 12 levels below
    assert by[24]["xp_now"] == 3000 and by[24]["min_level"] == 28
    assert by[28]["needs"] == [{"id": 24, "name": "High", "level": 30}]
    assert by[25]["start"]["name"] == "薩爾瑪·薩丁" and by[25]["rewards"] == [{"id": 769, "name": "大塊野豬肉"}]

    zones = get("quest-zones?character_id=c1")
    assert zones["known"] is True and len(zones["items"]) == 1
    row = zones["items"][0]
    assert (row["zone"], row["total"], row["completed"], row["in_log"], row["available"], row["too_low"], row["locked"]) \
        == ("西部荒野", 6, 1, 1, 2, 1, 1)

    status = lambda q: get(f"quests/{q}?character_id=c1")["status"]  # noqa: E731
    assert (status(10), status(26), status(21)) == ("in_log", "completed", "not_done")

    # without the game's completed list the answer is flagged as incomplete knowledge
    del data["quests_done"]
    client.put("/api/v1/characters/c2/snapshot", headers=AUTH,
               json={"captured_at": datetime.now(timezone.utc).isoformat(), "device_id": "mac", "data": data})
    assert get("zone-quests?character_id=c2&map_id=1436")["known"] is False
    assert get("quests/21?character_id=c2")["status"] == "unknown"
    tok = client.post("/api/v1/characters/c1/voice-token", headers=AUTH).json()["token"]
    S = {"Authorization": f"Bearer {tok}"}
    assert client.get("/api/v1/world/zone-quests?character_id=c2&map_id=1436", headers=S).status_code == 403
    assert client.get("/api/v1/world/quest-zones?character_id=c2", headers=S).status_code == 403


def test_xp_share_by_level_gap():
    from app.world import xp_share
    assert [xp_share(20, q) for q in (20, 15, 14, 13, 12, 5)] == [1.0, 1.0, 0.8, 0.6, 0.5, 0.5]
    assert xp_share(None, 10) == 1.0 and xp_share(20, -1) == 1.0 and xp_share(10, 30) == 1.0


def test_voice_todo_lists_what_is_left_in_the_current_zone(tmp_path, source):
    from fastapi.testclient import TestClient
    from app.main import create_app
    path = source / "data/quests.lua"
    path.write_text(path.read_text(encoding="utf-8").replace("end f(R) end", EXTRA_QUESTS + "end f(R) end"), encoding="utf-8")
    out = tmp_path / "world.db"
    assert importer_main(["import", "--source", "norgannondb", "--path", str(source), "--out", str(out)]) == 0
    client = TestClient(create_app(str(tmp_path / "server.db"), TOKEN, READ_TOKEN, str(out)))
    data = {"name": "小德", "level": 22, "race": "NightElf", "class": "DRUID", "map_id": 1436,
            "quests": [{"quest_id": 10, "title": "野豬肝餡餅"}], "quests_done": [9, 26]}
    client.put("/api/v1/characters/c1/snapshot", headers=AUTH,
               json={"captured_at": datetime.now(timezone.utc).isoformat(), "device_id": "mac", "data": data})
    tok = client.post("/api/v1/characters/c1/voice-token", headers=AUTH).json()["token"]
    text = client.get(f"/voice?t={tok}&show=todo").text
    assert "西部荒野還沒做的任務" in text and "共 6 個，已完成 1 個，進行中 1 個，現在可接 2 個" in text
    assert "Druid Thing（10 級，約 500 經驗，因為等級差距只剩部分經驗）" in text
    assert "Old（14 級，約 500 經驗，因為等級差距只剩部分經驗，找 薩爾瑪·薩丁 接）" in text
    assert "High" not in text and "注意" not in text
    data.pop("quests_done")
    data["map_id"] = 9999
    client.put("/api/v1/characters/c2/snapshot", headers=AUTH,
               json={"captured_at": datetime.now(timezone.utc).isoformat(), "device_id": "mac", "data": data})
    tok2 = client.post("/api/v1/characters/c2/voice-token", headers=AUTH).json()["token"]
    assert "目前所在的區域沒有任務資料" in client.get(f"/voice?t={tok2}&show=todo").text


def test_character_quests_endpoint_for_tools(world_client, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    client = TestClient(create_app(str(tmp_path / "plain.db"), TOKEN, READ_TOKEN, str(tmp_path / "missing-world.db")))
    now = datetime.now(timezone.utc).isoformat()
    data = {"name": "小德", "level": 22, "quests_done": [9, 7], "quests": [
        {"quest_id": 10, "title": "野豬肝餡餅", "level": 12, "category": "西部荒野", "complete": False,
         "objectives": [{"text": "血牙野豬的肝: 3/8", "finished": False}, {"text": "和薩爾瑪談談", "finished": True}]},
        {"quest_id": 424242, "title": "資料庫沒有的任務", "level": 30, "complete": True, "objectives": []}]}
    for c in (world_client, client):                     # with and without a world database
        c.put("/api/v1/characters/c1/snapshot", headers=AUTH,
              json={"captured_at": now, "device_id": "mac", "data": data})
    r = world_client.get("/api/v1/characters/c1/quests", headers=READ).json()
    assert (r["count"], r["ready_to_turn_in"], r["completed_total"], r["level"]) == (2, 1, 2, 22)
    q = r["items"][0]
    assert q["objectives"] == [{"text": "血牙野豬的肝: 3/8", "finished": False, "current": 3, "required": 8},
                               {"text": "和薩爾瑪談談", "finished": True, "current": None, "required": None}]
    w = q["world"]
    assert w["zone"] == "西部荒野" and w["map_id"] == 1436 and w["turn_in"]["name"] == "薩爾瑪·薩丁"
    assert (w["turn_in"]["location"]["x"], w["turn_in"]["location"]["y"]) == (56.4, 31.6)
    assert (w["xp"], w["xp_now"], w["xp_confirmed"]) == (850, 425, True)          # 10 levels below: half
    assert w["reward_items"] == [{"id": 769, "name": "大塊野豬肉"}] and w["next_quest"]["id"] == 11
    assert r["items"][1]["world"] is None and r["items"][1]["complete"] is True
    plain = client.get("/api/v1/characters/c1/quests", headers=READ).json()        # no world data installed
    assert plain["count"] == 2 and all(i["world"] is None for i in plain["items"])
    assert world_client.get("/api/v1/characters/nobody/quests", headers=READ).status_code == 404
    assert world_client.get("/api/v1/characters/c1/quests").status_code == 401
    tok = world_client.post("/api/v1/characters/c1/voice-token", headers=AUTH).json()["token"]
    S = {"Authorization": f"Bearer {tok}"}
    assert world_client.get("/api/v1/characters/c1/quests", headers=S).status_code == 200
    assert world_client.get("/api/v1/characters/c2/quests", headers=S).status_code == 403
