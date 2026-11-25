from conftest import AUTH, READ_TOKEN

READ = {"Authorization": f"Bearer {READ_TOKEN}"}


def ev(i, ts="2026-11-21T10:00:00+08:00", etype="LOCATION_SAMPLE", cid="char-1"):
    return {"event_id": f"inst-a:{i:010d}", "character_id": cid, "event_type": etype,
            "timestamp": ts, "payload": {"map_id": 1439, "x": 53.8, "y": 75.1}}


def batch(events):
    return {"schema_version": 1, "device_id": "mac-main", "events": events}


def test_health_is_open(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200 and r.json() == {"status": "ok", "version": "0.1.0"}


def test_auth(client):
    assert client.get("/api/v1/events").status_code == 401
    assert client.get("/api/v1/events", headers={"Authorization": "Bearer nope"}).status_code == 403
    assert client.get("/api/v1/events", headers=READ).status_code == 200
    # the read-only token must not be able to write
    assert client.post("/api/v1/events/batch", json=batch([ev(1)]), headers=READ).status_code == 403


def test_upload_dedup_and_query(client):
    r = client.post("/api/v1/events/batch", json=batch([ev(1), ev(2)]), headers=AUTH)
    assert r.status_code == 200 and r.json()["accepted"] == 2
    r = client.post("/api/v1/events/batch", json=batch([ev(2), ev(3)]), headers=AUTH)
    assert r.json()["accepted"] == 1 and r.json()["duplicates"] == 1
    items = client.get("/api/v1/events", headers=AUTH).json()["items"]
    assert len(items) == 3
    assert items[0]["timestamp"] == "2026-11-21T02:00:00Z"   # stored as UTC
    assert items[0]["payload"]["x"] == 53.8


def test_invalid_event_rejects_whole_batch(client):
    bad = ev(2)
    bad["timestamp"] = "2026-11-21T10:00:00"                 # no timezone
    r = client.post("/api/v1/events/batch", json=batch([ev(1), bad]), headers=AUTH)
    assert r.status_code == 422
    assert client.get("/api/v1/events", headers=AUTH).json()["items"] == []
    assert client.post("/api/v1/events/batch", json={"schema_version": 2, "device_id": "d",
                       "events": [ev(1)]}, headers=AUTH).status_code == 422


def test_filters_and_cursor_pagination(client):
    events = [ev(i, ts=f"2026-11-21T10:{i:02d}:00+08:00") for i in range(25)]
    events.append(ev(99, etype="LEVEL_UP", cid="char-2"))
    client.post("/api/v1/events/batch", json=batch(events), headers=AUTH)
    seen, cursor = [], None
    while True:
        params = {"character_id": "char-1", "limit": 10}
        if cursor:
            params["cursor"] = cursor
        page = client.get("/api/v1/events", params=params, headers=AUTH).json()
        seen += [e["event_id"] for e in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == 25 and len(set(seen)) == 25
    assert seen == sorted(seen, reverse=True)                # newest first, stable
    r = client.get("/api/v1/events", params={"event_type": "LEVEL_UP"}, headers=AUTH)
    assert [e["character_id"] for e in r.json()["items"]] == ["char-2"]
    r = client.get("/api/v1/locations", headers=AUTH, params={
        "start_time": "2026-11-21T10:05:00+08:00", "end_time": "2026-11-21T10:07:00+08:00"})
    assert len(r.json()["items"]) == 3
    assert client.get("/api/v1/events", params={"limit": 5000}, headers=AUTH).status_code == 422


def snap(ts, level):
    return {"captured_at": ts, "device_id": "mac-main",
            "data": {"name": "德魯伊", "realm": "R", "class": "DRUID", "level": level}}


def test_snapshot_rules(client):
    url = "/api/v1/characters/Player-1-ABC/snapshot"
    assert client.put(url, json=snap("2026-11-21T10:00:00+08:00", 24), headers=AUTH).json()["applied"]
    # older must not overwrite newer
    assert not client.put(url, json=snap("2026-11-21T09:00:00+08:00", 20), headers=AUTH).json()["applied"]
    # same time re-send is idempotent (stored one is kept)
    assert not client.put(url, json=snap("2026-11-21T10:00:00+08:00", 24), headers=AUTH).json()["applied"]
    assert client.put(url, json=snap("2026-11-21T11:00:00+08:00", 25), headers=AUTH).json()["applied"]
    got = client.get(url, headers=AUTH).json()
    assert got["data"]["level"] == 25 and got["captured_at"] == "2026-11-21T03:00:00Z"
    chars = client.get("/api/v1/characters", headers=AUTH).json()["items"]
    assert chars[0]["id"] == "Player-1-ABC" and chars[0]["level"] == 25 and chars[0]["name"] == "德魯伊"
    assert client.get("/api/v1/characters/Player-1-ABC", headers=AUTH).json()["class"] == "DRUID"
    assert client.get("/api/v1/characters/none", headers=AUTH).status_code == 404


def test_data_survives_restart(make_client):
    make_client().post("/api/v1/events/batch", json=batch([ev(1)]), headers=AUTH)
    again = make_client()                                    # new app, same db file
    assert len(again.get("/api/v1/events", headers=AUTH).json()["items"]) == 1
    assert again.get("/api/v1/stats/summary", headers=AUTH).json()["events"] == 1


def test_dashboard_page_is_served_without_data(client):
    r = client.get("/")
    assert r.status_code == 200 and "Adventure Hub" in r.text
    assert "text/html" in r.headers["content-type"]


def test_map_image_roundtrip(client):
    jpeg = b"\xff\xd8\xff\xe0" + b"x" * 100
    url = "/api/v1/maps/1436/image"
    assert client.get(url, headers=AUTH).status_code == 404
    assert client.put(url, content=b"not an image", headers=AUTH).status_code == 400
    assert client.put(url, content=jpeg, headers=READ).status_code == 403
    r = client.put(url, content=jpeg, headers=AUTH,
                   params={"name": "西部荒野", "captured_at": "2026-11-21T10:00:00+08:00"})
    assert r.status_code == 200
    got = client.get(url, headers=READ)
    assert got.content == jpeg and got.headers["content-type"] == "image/jpeg"
    items = client.get("/api/v1/maps", headers=READ).json()["items"]
    assert [(m["map_id"], m["name"]) for m in items] == [(1436, "西部荒野")]


def test_exclude_type_filter(client):
    client.post("/api/v1/events/batch", headers=AUTH,
                json=batch([ev(1), ev(2), ev(3, etype="LEVEL_UP")]))
    r = client.get("/api/v1/events", params={"exclude_type": "LOCATION_SAMPLE"}, headers=AUTH)
    assert [e["event_type"] for e in r.json()["items"]] == ["LEVEL_UP"]


def test_activity_summary(client):
    def e(i, minute, etype, payload):
        return {"event_id": f"a:{i}", "character_id": "char-1", "event_type": etype,
                "timestamp": f"2026-11-21T10:{minute:02d}:00+08:00", "payload": payload}
    events = [
        e(1, 0, "MOB_KILLED", {"monster_id": 547, "monster_name": "野豬"}),
        e(2, 1, "XP_GAINED", {"amount": 142}),
        e(3, 1, "ITEM_LOOTED", {"item_id": 769, "item_name": "野豬肉", "count": 2}),
        e(4, 2, "MOB_KILLED", {"monster_id": 547, "monster_name": "野豬"}),
        e(5, 2, "MOB_KILLED", {"monster_id": 122, "monster_name": "路霸"}),
        e(6, 3, "XP_GAINED", {"amount": 100}),
        e(7, 3, "MONEY_CHANGED", {"delta": 44}),
        e(8, 4, "MONEY_CHANGED", {"delta": -110}),
        e(9, 4, "COMBAT", {"duration": 25}),
        e(10, 30, "ITEM_LOOTED", {"item_id": 769, "item_name": "野豬肉"}),   # after a long break
    ]
    client.post("/api/v1/events/batch", json=batch(events), headers=AUTH)
    a = client.get("/api/v1/stats/activity", params={"character_id": "char-1"}, headers=AUTH).json()
    assert (a["kills"], a["xp"], a["items_looted"], a["fights"], a["combat_seconds"]) == (3, 242, 3, 1, 25)
    assert (a["money_in"], a["money_out"]) == (44, 110)
    assert a["active_seconds"] == 240                    # the 26-minute gap is not play time
    assert [(m["name"], m["count"]) for m in a["top_monsters"]] == [("野豬", 2), ("路霸", 1)]
    assert a["top_items"] == [{"id": 769, "name": "野豬肉", "count": 3, "value": 0}] and a["loot_value"] == 0
    assert a["xp_points"] == [["2026-11-21T02:01:00Z", 142], ["2026-11-21T02:03:00Z", 100]]
    later = client.get("/api/v1/stats/activity", headers=AUTH,
                       params={"start_time": "2026-11-21T10:10:00+08:00"}).json()
    assert later["kills"] == 0 and later["items_looted"] == 1


def test_setup_page_and_client_downloads(tmp_path):
    import io
    import zipfile
    from fastapi.testclient import TestClient
    from app.main import create_app
    web = TestClient(create_app(db_path=str(tmp_path / "s.db"), write_token="w", read_token="r"))
    page = web.get("/setup")
    assert page.status_code == 200 and "AdventureSync" in page.text
    from adventuresync import __version__
    assert web.get("/api/v1/setup").json() == {"source": True, "source_version": __version__, "addon_version": "0.1.7", "builds": []}
    src = zipfile.ZipFile(io.BytesIO(web.get("/download/client-source.zip").content))
    names = set(src.namelist())
    assert {"AdventureSync/adventuresync/core.py", "AdventureSync/adventuresync/gui.py", "AdventureSync/AdventureSync.pyw",
            "AdventureSync/requirements.txt", "AdventureSync/README.txt",
            "AdventureSync/addon/AdventureCollector/AdventureCollector.toc", "AdventureSync/addon/AdventureCollector/Core.lua"} <= names
    # nothing that could hold a token, and nothing compiled
    assert not [n for n in names if n.endswith((".toml", ".pyc", ".db")) or "config" in n.lower() or "__pycache__" in n]
    addon = zipfile.ZipFile(io.BytesIO(web.get("/download/addon.zip").content))
    assert sorted(addon.namelist()) == ["AdventureCollector/AdventureCollector.toc", "AdventureCollector/Core.lua"]
    # ready-made programs: only the owner may put them there, anyone may fetch them
    w = {"Authorization": "Bearer w"}
    assert web.put("/api/v1/builds/AdventureSync-windows-x64.exe", content=b"MZ" + b"x" * 5000).status_code == 401
    assert web.put("/api/v1/builds/AdventureSync-windows-x64.exe", content=b"MZ", headers={"Authorization": "Bearer r"}).status_code == 403
    assert web.put("/api/v1/builds/AdventureSync-windows-x64.exe", content=b"MZ" + b"x" * 5000, headers=w).json()["size"] == 5002
    for bad in ("x.sh", "..%2Fx.exe", "a b.exe", ".hidden.exe"):
        assert web.put(f"/api/v1/builds/{bad}", content=b"x", headers=w).status_code in (400, 404)
    assert web.put("/api/v1/builds/empty.zip", content=b"", headers=w).status_code == 400
    listed = web.get("/api/v1/setup").json()["builds"]
    assert [(b["name"], b["size"], b["version"]) for b in listed] == [("AdventureSync-windows-x64.exe", 5002, None)]
    # a build uploaded with its version says so; uploading it again without one forgets the old version
    put = lambda **params: web.put("/api/v1/builds/AdventureSync-windows-x64.exe", content=b"MZ" + b"y" * 5000, headers=w, params=params)   # noqa: E731
    assert put(version="1.2.3").json()["version"] == "1.2.3"
    assert web.get("/api/v1/setup").json()["builds"][0]["version"] == "1.2.3"
    assert put(version="../x").status_code == 400 and put(version="1.2.3-beta").status_code == 400
    assert put().status_code == 200 and web.get("/api/v1/setup").json()["builds"][0]["version"] is None
    assert put(version="1.2.4").status_code == 200
    got = web.get("/download/builds/AdventureSync-windows-x64.exe")
    # downloads keep their name from version to version: nothing in between may keep a copy
    assert got.headers["cache-control"] == "no-store" and web.get("/download/addon.zip").headers["cache-control"] == "no-store"
    assert web.get("/download/builds/AdventureSync-windows-x64.exe?v=1.2.4-5002").content == got.content
    assert got.content[:2] == b"MZ" and "attachment" in got.headers["content-disposition"]
    assert web.get("/download/builds/nope.exe").status_code == 404
    assert web.delete("/api/v1/builds/AdventureSync-windows-x64.exe", headers=w).status_code == 200
    assert web.get("/api/v1/setup").json()["builds"] == [] and not [f for f in (tmp_path / "builds").iterdir()]


def test_a_personal_token_is_one_person_with_any_number_of_characters(tmp_path):
    from datetime import datetime, timezone
    from fastapi.testclient import TestClient
    from app.main import create_app
    make = lambda host: TestClient(create_app(db_path=str(tmp_path / "s.db"), write_token="w", read_token="r"),   # noqa: E731
                                   client=(host, 50000))
    lan, out = make("192.168.1.50"), make("203.0.113.9")
    # the owner's page: only from the local network, and never through a proxy that reports an outside visitor
    assert lan.get("/admin").status_code == 200 and out.get("/admin").status_code == 404
    assert out.get("/api/v1/admin/overview").status_code == 404 and out.post("/api/v1/admin/tokens", json={}).status_code == 404
    for name, value in (("X-Forwarded-For", "203.0.113.9"), ("CF-Connecting-IP", "203.0.113.9"), ("X-Forwarded-For", "192.168.1.9, 203.0.113.9"),
                        ("Forwarded", "for=203.0.113.9;proto=https"), ("X-Real-IP", "not-an-address"), ("Forwarded", "by=x")):
        assert lan.get("/admin", headers={name: value}).status_code == 404, (name, value)
    assert lan.get("/admin", headers={"X-Forwarded-For": "192.168.1.9", "Forwarded": 'for="[fd00::1]:4711"'}).status_code == 200
    assert "/admin" not in lan.get("/openapi.json").text

    token = lan.post("/api/v1/admin/tokens", json={"label": "小明"}).json()["token"]
    assert len(token) == 16 and lan.get("/api/v1/admin/overview").json()["tokens"][0]["characters"] == []
    mine, now = {"Authorization": f"Bearer {token}"}, datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc).isoformat()
    snap = lambda name: {"captured_at": now, "device_id": "pc", "data": {"name": name, "class": "MAGE", "level": 10}}                 # noqa: E731
    event = lambda cid, n: {"event_id": f"e:{cid}:{n}", "character_id": cid, "event_type": "LEVEL_UP", "timestamp": now, "payload": {}}  # noqa: E731
    batch = lambda *ev: {"schema_version": 1, "device_id": "pc", "events": list(ev)}                                                  # noqa: E731
    assert out.get("/api/v1/events", headers=mine).status_code == 403               # nothing uploaded yet: it shows nothing
    # one person, two characters, one token: both are theirs, in one batch or apart
    assert out.put("/api/v1/characters/A/snapshot", headers=mine, json=snap("小明法師")).status_code == 200
    assert out.put("/api/v1/characters/B/snapshot", headers=mine, json=snap("小明獵人")).status_code == 200
    assert out.post("/api/v1/events/batch", headers=mine, json=batch(event("A", 1), event("B", 1), event("A", 2))).json()["accepted"] == 3
    seen = lan.get("/api/v1/admin/overview").json()
    assert [c["name"] for c in seen["tokens"][0]["characters"]] == ["小明法師", "小明獵人"] and seen["tokens"][0]["last_used_at"]
    assert {c["id"]: c["token_label"] for c in seen["characters"]} == {"A": "小明", "B": "小明"}
    assert out.get("/api/v1/token", headers=mine).json()["kind"] == "personal"
    # with the same token they see both, switch between them, and nothing of anyone else's
    assert sorted(c["id"] for c in out.get("/api/v1/characters", headers=mine).json()["items"]) == ["A", "B"]
    for cid in ("A", "B"):
        assert {e["character_id"] for e in out.get(f"/api/v1/events?character_id={cid}", headers=mine).json()["items"]} == {cid}
        assert out.get(f"/api/v1/characters/{cid}", headers=mine).json()["id"] == cid
    assert out.put("/api/v1/characters/Z/snapshot", headers={"Authorization": "Bearer w"}, json=snap("主人的角色")).status_code == 200
    assert out.get("/api/v1/characters/Z", headers=mine).status_code == 403
    asked = out.get("/api/v1/events?character_id=Z", headers=mine)
    assert asked.status_code == 403 or {e["character_id"] for e in asked.json()["items"]} <= {"A", "B"}
    # another person's token: their own characters are fine, somebody else's are refused, in every kind of upload
    other = {"Authorization": "Bearer " + lan.post("/api/v1/admin/tokens", json={"label": "小華"}).json()["token"]}
    assert out.put("/api/v1/characters/C/snapshot", headers=other, json=snap("小華戰士")).status_code == 200
    assert out.put("/api/v1/characters/A/snapshot", headers=other, json=snap("x")).status_code == 409
    assert out.post("/api/v1/events/batch", headers=other, json=batch(event("C", 1), event("A", 9))).status_code == 409
    assert out.post("/api/v1/combat/hits", headers=other, json={"character_id": "A", "source": "l", "rows": [[1, 1, "cast", "x", 0] + [None] * 23]}).status_code == 409
    assert out.get("/api/v1/token?character_id=A", headers=other).json()["can_upload"] is False
    assert out.get("/api/v1/token?character_id=C", headers=other).json()["can_upload"] is True
    jpeg = b"\xff\xd8\xff" + b"0" * 64
    shot = lambda sid, cid, h: out.put(f"/api/v1/screenshots/{sid}", params={"character_id": cid, "taken_at": now}, content=jpeg, headers=h)  # noqa: E731
    assert shot("s1", "A", mine).status_code == 200 and shot("s2", "B", mine).status_code == 200
    assert shot("s1", "A", other).status_code == 409 and shot("s1", "C", other).status_code == 409
    assert out.delete("/api/v1/screenshots/s1", headers=other).status_code == 409
    assert out.get("/api/v1/screenshots/s2/image", headers=mine).status_code == 200      # a second character's picture too
    assert out.get("/api/v1/screenshots/s1/image", headers=other).status_code == 404
    assert out.put("/api/v1/builds/x.zip", content=b"x", headers=mine).status_code == 403       # the owner's token only
    # the owner can hand a character to someone else, or to nobody
    his = other["Authorization"][7:]
    assert lan.post(f"/api/v1/admin/characters/B/token?token={his}").status_code == 200
    assert out.put("/api/v1/characters/B/snapshot", headers=other, json=snap("小明獵人")).status_code == 200
    assert out.put("/api/v1/characters/B/snapshot", headers=mine, json=snap("x")).status_code == 409
    assert lan.post("/api/v1/admin/characters/B/token").status_code == 200           # nobody's: the next to upload takes it
    assert out.put("/api/v1/characters/B/snapshot", headers=mine, json=snap("小明獵人")).status_code == 200
    assert lan.post("/api/v1/admin/characters/nobody/token").status_code == 404
    assert lan.post("/api/v1/admin/characters/B/token?token=nope").status_code == 404
    # a deleted token stops working at once; its characters keep their data and wait for a new owner
    assert lan.delete(f"/api/v1/admin/tokens/{token}").status_code == 200
    assert out.put("/api/v1/characters/A/snapshot", headers=mine, json=snap("x")).status_code == 403
    left = {c["id"]: c["token"] for c in lan.get("/api/v1/admin/overview").json()["characters"]}
    assert left["A"] is None and left["C"] == his and out.get("/api/v1/characters/A", headers={"Authorization": "Bearer r"}).status_code == 200
    given = lan.post("/api/v1/admin/tokens", json={"label": "新的", "character_id": "A"}).json()["token"]
    assert out.put("/api/v1/characters/A/snapshot", headers={"Authorization": f"Bearer {given}"}, json=snap("小明法師")).status_code == 200


def test_tokens_from_the_one_character_days_are_carried_over(tmp_path):
    import sqlite3
    from fastapi.testclient import TestClient
    from app.main import create_app
    TestClient(create_app(db_path=str(tmp_path / "s.db"), write_token="w", read_token="r"))
    with sqlite3.connect(tmp_path / "s.db") as conn:
        conn.execute("INSERT INTO upload_tokens VALUES ('oldtoken','阿強','A','2026-11-01T00:00:00Z','2026-11-02T00:00:00Z',NULL)")
    web = TestClient(create_app(db_path=str(tmp_path / "s.db"), write_token="w", read_token="r"), client=("192.168.1.5", 1))
    tok, = web.get("/api/v1/admin/overview").json()["tokens"]
    assert tok["label"] == "阿強" and [c["id"] for c in tok["characters"]] == ["A"]
