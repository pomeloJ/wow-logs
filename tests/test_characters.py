"""Several characters behind one token: the default character, and refusals that name the reason."""
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from conftest import AUTH, READ_TOKEN, TOKEN

READ = {"Authorization": f"Bearer {READ_TOKEN}"}
NOW = datetime(2026, 11, 27, 9, 0, tzinfo=timezone.utc).isoformat()


def setup(tmp_path):
    from app.main import create_app
    app = create_app(str(tmp_path / "s.db"), TOKEN, READ_TOKEN)
    lan = TestClient(app, client=("192.168.1.5", 1))
    mine = lan.post("/api/v1/admin/tokens", json={"label": "小明"}).json()["token"]
    me = {"Authorization": f"Bearer {mine}"}
    for cid, name, headers in (("A", "亞克", me), ("B", "起承", me), ("C", "別人", AUTH)):
        assert lan.put(f"/api/v1/characters/{cid}/snapshot", headers=headers, json={
            "captured_at": NOW, "device_id": "pc", "data": {"name": name, "level": 10}}).status_code == 200
        lan.post("/api/v1/events/batch", headers=headers, json={"schema_version": 1, "device_id": "pc", "events": [
            {"event_id": f"{cid}:1", "character_id": cid, "event_type": "MOB_KILLED", "timestamp": NOW,
             "payload": {"monster_name": "怪"}}]})
    # known only from its events: no snapshot yet
    lan.post("/api/v1/events/batch", headers=AUTH, json={"schema_version": 1, "device_id": "pc", "events": [
        {"event_id": "D:1", "character_id": "D", "event_type": "MOB_KILLED", "timestamp": NOW, "payload": {}}]})
    return lan, me


def error(r):
    return r.status_code, r.json()["detail"]["error"]


def test_a_default_character_is_kept_per_token_and_used_when_none_is_named(tmp_path):
    lan, me = setup(tmp_path)
    get = lambda url, h=me: lan.get("/api/v1" + url, headers=h)            # noqa: E731
    listed = get("/characters").json()
    assert [c["id"] for c in listed["items"]] == ["A", "B"] and listed["default_character_id"] is None
    assert not any(c["is_default"] for c in listed["items"])
    assert get("/characters/default").json() == {"character_id": None, "set": False, "reason": "not_set", "character": None}
    # only a character this token may read
    assert error(lan.put("/api/v1/characters/default", headers=me, json={"character_id": "C"})) == (403, "character_not_authorized")
    chosen = lan.put("/api/v1/characters/default", headers=me, json={"character_id": "B"}).json()
    assert (chosen["character_id"], chosen["set"], chosen["reason"], chosen["character"]["name"]) == ("B", True, "set", "起承")
    listed = get("/characters").json()
    assert listed["default_character_id"] == "B" and [c["id"] for c in listed["items"] if c["is_default"]] == ["B"]
    # named: that one, for this call only; named none: the default
    assert [e["character_id"] for e in get("/events").json()["items"]] == ["B"]
    assert [e["character_id"] for e in get("/events?character_id=A").json()["items"]] == ["A"]
    assert get("/characters/default").json()["character_id"] == "B"            # a one-off ask changes nothing
    # another token keeps its own
    assert get("/characters/default", READ).json()["reason"] == "not_set"
    lan.put("/api/v1/characters/default", headers=READ, json={"character_id": "C"})
    assert get("/characters/default", READ).json()["character_id"] == "C" and get("/characters/default").json()["character_id"] == "B"
    # forgotten
    assert lan.delete("/api/v1/characters/default", headers=me).json()["reason"] == "not_set"
    assert get("/characters").json()["default_character_id"] is None
    # a token with one character needs no choosing
    voice = lan.post("/api/v1/characters/A/voice-token", headers=AUTH).json()["token"]
    only = get("/characters/default", {"Authorization": f"Bearer {voice}"}).json()
    assert (only["character_id"], only["set"], only["reason"]) == ("A", False, "only_character")
    assert lan.get("/api/v1/characters/default").status_code == 401


def test_a_character_that_cannot_be_answered_about_is_refused_with_the_reason_never_swapped(tmp_path):
    lan, me = setup(tmp_path)
    get = lambda url, h=me: lan.get("/api/v1" + url, headers=h)            # noqa: E731
    for url in ("/characters/C", "/characters/C/snapshot", "/characters/C/items", "/characters/C/quests",
                "/events?character_id=C", "/stats/activity?character_id=C"):
        r = get(url)
        assert error(r) == (403, "character_not_authorized") and r.json()["detail"]["character_id"] == "C", url
    assert error(get("/characters/nobody/snapshot", READ)) == (404, "character_not_found")
    assert error(get("/events?character_id=nobody", READ)) == (404, "character_not_found")
    assert error(get("/characters/D/snapshot", READ)) == (404, "character_not_synced")
    assert error(get("/characters/D/items", READ)) == (404, "character_not_synced")
    assert error(lan.put("/api/v1/characters/default", headers=READ, json={"character_id": "D"})) == (404, "character_not_synced")
    assert error(lan.put("/api/v1/characters/default", headers=READ, json={"character_id": "nobody"})) == (404, "character_not_found")
    assert get("/characters/D", READ).status_code == 404 and get("/events?character_id=D", READ).status_code == 200


def test_the_docs_explain_several_characters(tmp_path):
    lan, _ = setup(tmp_path)
    spec = lan.get("/openapi.json").json()
    ops = spec["paths"]["/api/v1/characters/default"]
    assert {m: op["tags"] for m, op in ops.items()} == {m: ["角色 Characters"] for m in ("get", "put", "delete")}
    for word in ("characters/default", "character_not_authorized", "character_not_found", "character_not_synced"):
        assert word in spec["info"]["description"]
