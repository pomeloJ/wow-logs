"""Mocked game -> SavedVariables file -> agent -> server -> GET. No WoW login needed."""
import httpx

from adventuresync.core import Config, Queue, Uploader, scan
from conftest import AUTH, TOKEN
from wowmock import Game

CHAR = "Player-1234-00ABCDEF"


def test_full_pipeline_offline_restart_and_second_device(tmp_path, client):
    wow = tmp_path / "wow"
    sv = wow / "_forever_" / "WTF" / "Account" / "1#1" / "SavedVariables" / "AdventureCollector.lua"
    cfg = Config(url="http://testserver", api_token=TOKEN, device_id="mac-main",
                 wow_path=str(wow), batch_size=3, db_path=str(tmp_path / "queue.db"))

    # --- play session 1 and log out
    game = Game()
    game.login()
    for x in (0.55, 0.57, 0.59, 0.61):
        game.mock.x = x
        game.tick(15)
    game.logout(sv)
    n1 = len(game.events())
    assert n1 == 6

    # --- agent reads the file while the server is unreachable
    queue = Queue(cfg.db_path)
    seen = {}
    scan(cfg, queue, seen)

    def down(request):
        raise httpx.ConnectError("offline")
    offline = httpx.Client(transport=httpx.MockTransport(down), base_url="http://testserver")
    assert Uploader(cfg, queue, offline).sync_once()["error"]
    assert queue.counts()["pending"] == n1
    queue.conn.close()

    # --- agent restarts; network is back; everything is delivered exactly once
    queue = Queue(cfg.db_path)
    res = Uploader(cfg, queue, client).sync_once()
    assert res == {"uploaded": n1, "rejected": 0, "snapshots": 1, "error": None, "waiting": []}
    scan(cfg, queue, {})                                     # re-read the same file
    assert Uploader(cfg, queue, client).sync_once()["uploaded"] == 0

    events = client.get("/api/v1/events", params={"character_id": CHAR}, headers=AUTH).json()["items"]
    assert len(events) == n1
    locs = client.get("/api/v1/locations", params={"map_id": 1439}, headers=AUTH).json()["items"]
    assert [e["payload"]["x"] for e in locs] == [61.0, 59.0, 57.0, 55.0, 53.8]
    assert locs[0]["payload"]["zone_name"] == "黑海岸"
    char = client.get(f"/api/v1/characters/{CHAR}", headers=AUTH).json()
    assert char["name"] == "測試德魯伊" and char["level"] == 24 and char["class"] == "DRUID"

    # --- session 2 on the same machine: file now holds old + new events
    game.mock.time += 3600
    game.login()
    game.fire("PLAYER_LEVEL_UP", 25)
    game.mock.level = 25
    game.logout(sv)
    scan(cfg, queue, {})
    res = Uploader(cfg, queue, client).sync_once()
    assert res["uploaded"] == len(game.events()) - n1 and res["snapshots"] == 1
    snap = client.get(f"/api/v1/characters/{CHAR}/snapshot", headers=AUTH).json()
    assert snap["data"]["level"] == 25

    # --- same character on a second computer: separate install, no id collisions
    wow2 = tmp_path / "wow2"
    sv2 = wow2 / "_forever_" / "WTF" / "Account" / "1#1" / "SavedVariables" / "AdventureCollector.lua"
    pc = Game()
    pc.mock.time = game.mock.time + 7200
    pc.mock.level = 25
    pc.login()
    pc.logout(sv2)
    cfg2 = Config(url="http://testserver", api_token=TOKEN, device_id="win-pc",
                  wow_path=str(wow2), db_path=str(tmp_path / "queue2.db"))
    q2 = Queue(cfg2.db_path)
    scan(cfg2, q2, {})
    assert Uploader(cfg2, q2, client).sync_once()["uploaded"] == len(pc.events())

    total = client.get("/api/v1/stats/summary", headers=AUTH).json()
    assert total["events"] == len(game.events()) + len(pc.events())
    assert total["characters"] == 1


def test_map_screenshot_is_cropped_and_uploaded(tmp_path, client):
    import io
    import os
    from PIL import Image

    wow = tmp_path / "wow"
    flavor = wow / "_forever_"
    sv = flavor / "WTF" / "Account" / "1#1" / "SavedVariables" / "AdventureCollector.lua"
    game = Game()
    game.login()
    game.slash("mapshot")     # mocked UI is 1000x500 units; map spans x 200-800, y 50-450 from the bottom
    game.logout(sv)

    # The real screen is 20px taller than the UI area (like a MacBook notch strip on top),
    # so the map sits 20px lower in the screenshot than UI-relative maths would suggest.
    shot = Image.new("RGB", (1000, 520), (200, 0, 0))
    shot.paste((0, 160, 0), (200, 70, 800, 470))
    shots = flavor / "Screenshots"
    shots.mkdir()
    target = shots / "WoWScrnShot_112126_120000.jpg"
    shot.save(target, quality=95)
    os.utime(target, (game.mock.time + 1, game.mock.time + 1))
    old = shots / "WoWScrnShot_old.jpg"           # an unrelated, older screenshot
    Image.new("RGB", (1000, 500), (0, 0, 200)).save(old)
    os.utime(old, (game.mock.time - 500, game.mock.time - 500))

    cfg = Config(url="http://testserver", api_token=TOKEN, device_id="mac-main",
                 wow_path=str(wow), db_path=str(tmp_path / "q.db"))
    queue = Queue(cfg.db_path)
    up = Uploader(cfg, queue, client)
    scan(cfg, queue, {}, up)

    maps = client.get("/api/v1/maps", headers=AUTH).json()["items"]
    assert [(m["map_id"], m["name"]) for m in maps] == [(1436, "西部荒野")]
    img = Image.open(io.BytesIO(client.get("/api/v1/maps/1436/image", headers=AUTH).content))
    assert img.size == (600, 400)
    for xy in ((5, 5), (595, 395), (300, 200)):   # only the map area survived the crop
        r, g_, b = img.getpixel(xy)
        assert g_ > 120 and r < 60 and b < 60

    # Processed once: a second scan does not upload again even if the image is replaced.
    client.put("/api/v1/maps/1436/image", content=b"\xff\xd8\xff" + b"0" * 10, headers=AUTH)
    scan(cfg, queue, {}, up)
    assert len(client.get("/api/v1/maps/1436/image", headers=AUTH).content) == 13
