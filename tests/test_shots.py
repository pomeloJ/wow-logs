from datetime import datetime, timedelta, timezone

from conftest import AUTH, READ_TOKEN

READ = {"Authorization": f"Bearer {READ_TOKEN}"}
JPEG = b"\xff\xd8\xff\xe0" + b"1" * 200
THUMB = b"\xff\xd8\xff\xe0" + b"2" * 50
T0 = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


def put(client, shot_id, cid="c1", minutes=5, headers=AUTH):
    r = client.put(f"/api/v1/screenshots/{shot_id}", content=JPEG, headers=headers, params={
        "character_id": cid, "taken_at": (T0 + timedelta(minutes=minutes)).isoformat(), "width": 1920, "height": 1080})
    if r.status_code == 200:
        assert client.put(f"/api/v1/screenshots/{shot_id}/thumb", content=THUMB, headers=headers).status_code == 200
    return r


def events(client, cid, rows):
    ev = [{"event_id": f"{cid}:s{i}", "character_id": cid, "event_type": t,
           "timestamp": (T0 + timedelta(minutes=m)).isoformat(), "payload": p} for i, (m, t, p) in enumerate(rows)]
    client.post("/api/v1/events/batch", headers=AUTH, json={"schema_version": 1, "device_id": "mac", "events": ev})


def test_upload_list_and_place_on_the_timeline(client):
    events(client, "c1", [
        (4, "LOCATION_SAMPLE", {"map_id": 1439, "x": 40.0, "y": 50.0, "zone_name": "黑海岸"}),
        (5.5, "MOB_KILLED", {"monster_name": "雷鱗巫師", "map_id": 1439, "x": 41.0, "y": 50.5, "zone_name": "黑海岸"}),
        (30, "LOCATION_SAMPLE", {"map_id": 1436, "x": 1.0, "y": 1.0, "zone_name": "西部荒野"}),
        (5, "CHAT_MESSAGE", {"message": "秘密"})])
    assert put(client, "WoWScrnShot_112226_200500").status_code == 200
    assert put(client, "later", minutes=120).status_code == 200
    r = client.get("/api/v1/screenshots?character_id=c1", headers=READ).json()
    assert r["total"] == 2 and [s["id"] for s in r["items"]] == ["later", "WoWScrnShot_112226_200500"]   # newest first
    shot = r["items"][1]
    assert shot["place"] == {"map_id": 1439, "zone": "黑海岸", "x": 41.0, "y": 50.5}     # the nearest recorded position
    assert shot["moment"] == "擊殺 雷鱗巫師" and (shot["width"], shot["height"]) == (1920, 1080)
    assert r["items"][0]["place"] is None and r["items"][0]["moment"] is None          # nothing recorded around it
    assert "秘密" not in str(r)
    assert client.get("/api/v1/screenshots/later/image", headers=READ).content == JPEG
    assert client.get("/api/v1/screenshots/later/thumb", headers=READ).content == THUMB
    older = client.get("/api/v1/screenshots", headers=READ,
                       params={"before": (T0 + timedelta(minutes=60)).isoformat()}).json()
    assert [s["id"] for s in older["items"]] == ["WoWScrnShot_112226_200500"]


def test_upload_rules_and_delete(client):
    assert put(client, "a", headers=READ).status_code == 403                 # read tokens cannot upload
    assert put(client, "bad id!").status_code == 400
    r = client.put("/api/v1/screenshots/x", content=b"not a jpeg", headers=AUTH,
                   params={"character_id": "c1", "taken_at": T0.isoformat()})
    assert r.status_code == 400
    r = client.put("/api/v1/screenshots/x", content=JPEG, headers=AUTH,
                   params={"character_id": "c1", "taken_at": "2026-11-22T12:00:00"})
    assert r.status_code == 400                                               # a time needs its timezone
    assert put(client, "a").status_code == 200
    assert client.get("/api/v1/screenshots/a/image").status_code == 401
    assert client.delete("/api/v1/screenshots/a", headers=READ).status_code == 403
    assert client.delete("/api/v1/screenshots/a", headers=AUTH).status_code == 200
    assert client.get("/api/v1/screenshots/a/image", headers=READ).status_code == 404
    assert client.delete("/api/v1/screenshots/a", headers=AUTH).status_code == 404


def test_character_share_token_sees_only_its_own_screenshots(client):
    for cid in ("c1", "c2"):
        client.put(f"/api/v1/characters/{cid}/snapshot", headers=AUTH,
                   json={"captured_at": T0.isoformat(), "device_id": "mac", "data": {"name": cid}})
    put(client, "mine", "c1")
    put(client, "theirs", "c2")
    tok = client.post("/api/v1/characters/c1/voice-token", headers=AUTH).json()["token"]
    S = {"Authorization": f"Bearer {tok}"}
    assert [s["id"] for s in client.get("/api/v1/screenshots?character_id=c2", headers=S).json()["items"]] == ["mine"]
    assert client.get("/api/v1/screenshots/mine/thumb", headers=S).status_code == 200
    assert client.get("/api/v1/screenshots/theirs/thumb", headers=S).status_code == 404


def test_party_page_gets_the_screenshots_of_its_session_only(client):
    events(client, "c1", [
        (0, "GROUP_JOINED", {"group_id": "g1", "members": [{"name": "艾莉"}]}),
        (20, "GROUP_LEFT", {"group_id": "g1"})])
    put(client, "during", minutes=10)
    put(client, "after", minutes=40)
    tok = client.get("/api/v1/groups?character_id=c1", headers=READ).json()["items"][0]["token"]
    d = client.get(f"/api/v1/shared/party/{tok}").json()
    assert [s["id"] for s in d["screenshots"]] == ["during"]
    assert client.get(f"/api/v1/shared/party/{tok}/shot/during").content == JPEG
    assert client.get(f"/api/v1/shared/party/{tok}/shot/during?thumb=1").content == THUMB
    assert client.get(f"/api/v1/shared/party/{tok}/shot/after").status_code == 404      # outside the session
    assert client.get("/api/v1/shared/party/wrongwrong12/shot/during").status_code == 404
