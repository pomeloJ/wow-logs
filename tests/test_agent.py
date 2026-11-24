import httpx
import pytest

from adventuresync import luaparser
from adventuresync.core import (Config, Queue, Uploader, epoch_to_iso, find_saved_variables,
                                ingest, load_config, normalize_event, read_stable)
from conftest import AUTH, TOKEN

SAMPLE = r'''
AdventureCollectorDB = {
["schemaVersion"] = 1,
["installationId"] = "abc",
["settings"] = {
["enabled"] = true,
["minMove"] = 0.5,
},
["empty"] = {
},
["events"] = {
{
["event_id"] = "abc:0000000001",
["character_id"] = "Player-1-A",
["event_type"] = "LOCATION_SAMPLE",
["ts"] = 1795000000,
["payload"] = {
["map_id"] = 1439,
["x"] = 53.8,
["zone_name"] = "黑海岸 \"引號\" 反斜線\\ 換行\n \229\141\151",
["neg"] = -1.5e-3,
},
}, -- [1]
},
}
Other = nil
'''


def test_parser_handles_wow_format():
    out = luaparser.loads(SAMPLE)
    db = out["AdventureCollectorDB"]
    assert db["schemaVersion"] == 1 and db["settings"] == {"enabled": True, "minMove": 0.5}
    assert db["empty"] == [] and out["Other"] is None
    p = db["events"][0]["payload"]
    assert p["zone_name"] == '黑海岸 "引號" 反斜線\\ 換行\n 南'
    assert p["neg"] == -0.0015 and p["map_id"] == 1439


def test_parser_misc_syntax():
    assert luaparser.loads("A = { 1, 'two', [3] = true, name = 0x10, }")["A"] == \
        {1: 1, 2: "two", 3: True, "name": 16}
    assert luaparser.loads("A = {1,2,3}")["A"] == [1, 2, 3]


@pytest.mark.parametrize("text", [
    "A = {", 'A = {["x"] = "unterminated', "A = os.execute('x')",
    "A = function() end", "A = {1 2}", "A = {[1] = }",
])
def test_parser_rejects_truncated_or_code(text):
    with pytest.raises(luaparser.LuaParseError):
        luaparser.loads(text)


def test_normalize_event():
    raw = luaparser.loads(SAMPLE)["AdventureCollectorDB"]["events"][0]
    e = normalize_event(raw)
    assert e["timestamp"] == epoch_to_iso(1795000000) and e["timestamp"].endswith("Z")
    assert normalize_event({**raw, "payload": []})["payload"] == {}
    for bad in ({**raw, "ts": "x"}, {**raw, "event_id": ""}, "nope"):
        with pytest.raises(ValueError):
            normalize_event(bad)


def test_discovery_and_stable_read(tmp_path):
    sv = tmp_path / "_forever_" / "WTF" / "Account" / "1234#1" / "SavedVariables"
    sv.mkdir(parents=True)
    f = sv / "AdventureCollector.lua"
    f.write_text(SAMPLE, encoding="utf-8")
    assert find_saved_variables(str(tmp_path)) == [f]
    assert find_saved_variables(str(tmp_path / "_forever_")) == [f]
    assert find_saved_variables(str(tmp_path / "missing")) == []
    assert "AdventureCollectorDB" in read_stable(f, settle=0.01)
    f.write_text(SAMPLE[:200], encoding="utf-8")            # half-written file
    with pytest.raises(RuntimeError):
        read_stable(f, settle=0.01, attempts=2)


def test_usual_places_to_look_for_the_game(monkeypatch):
    from adventuresync import core
    assert core.wow_roots("~/x")[0].name == "x" and len(core.wow_roots("~/x")) == 1     # a chosen folder: only that
    monkeypatch.setattr(core.sys, "platform", "win32")
    monkeypatch.setattr(core, "_windows_drives", lambda: ["C", "D"])
    monkeypatch.setattr(core, "_windows_registry", lambda: [r"D:\Games\World of Warcraft"])
    places = [str(p).replace("\\", "/") for p in core.wow_roots()]
    assert places[0] == "D:/Games/World of Warcraft"                  # what the installer wrote down comes first
    assert "D:/World of Warcraft" in places and "C:/Program Files (x86)/World of Warcraft" in places
    assert len(places) == len(set(p.lower() for p in places))         # each folder once

    def broken():
        raise OSError("no registry")
    monkeypatch.setattr(core, "_windows_registry", broken)
    assert [str(p).replace("\\", "/") for p in core.wow_roots()][1:] == core.DEFAULT_WOW_PATHS[1:]   # still the usual ones


def test_config(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[server]\nurl = "http://x:1/"\napi_token = "t"\n[client]\ndevice_id = "d"\n')
    cfg = load_config(p)
    assert cfg.url == "http://x:1" and cfg.batch_size == 100 and cfg.device_id == "d"
    p.write_text('[server]\nurl = "http://x"\n')
    with pytest.raises(ValueError):
        load_config(p)


def cfg(**kw):
    return Config(url="http://testserver", api_token=TOKEN, device_id="mac-test", **kw)


def test_queue_dedup_and_ingest():
    q = Queue(":memory:")
    parsed = luaparser.loads(SAMPLE)
    assert ingest(parsed, q)[0] == 1
    assert ingest(parsed, q)[0] == 0                         # re-reading adds nothing
    assert q.counts()["pending"] == 1


def test_bad_event_is_isolated_not_blocking(client):
    q = Queue(":memory:")
    good = [{"event_id": f"a:{i}", "character_id": "c", "event_type": "LOCATION_SAMPLE",
             "timestamp": "2026-11-21T02:00:00Z", "payload": {}} for i in range(3)]
    bad = {**good[0], "event_id": "a:bad", "event_type": "lower case!"}
    q.add_events(good[:2] + [bad] + good[2:])
    res = Uploader(cfg(), q, client).sync_once()
    assert res["uploaded"] == 3 and res["rejected"] == 1 and res["error"] is None
    assert q.counts() == {"pending": 0, "uploaded": 3, "rejected": 1, "pending_snapshots": 0}
    assert len(client.get("/api/v1/events", headers=AUTH).json()["items"]) == 3


def test_network_and_auth_failures_keep_data_pending(client):
    q = Queue(":memory:")
    q.add_events([{"event_id": "a:1", "character_id": "c", "event_type": "X",
                   "timestamp": "2026-11-21T02:00:00Z", "payload": {}}])

    def down(request):
        raise httpx.ConnectError("offline")
    offline = httpx.Client(transport=httpx.MockTransport(down), base_url="http://testserver")
    res = Uploader(cfg(), q, offline).sync_once()
    assert res["error"] and q.counts()["pending"] == 1

    wrong = Config(url="http://testserver", api_token="wrong", device_id="d")
    res = Uploader(wrong, q, client).sync_once()
    assert "403" in res["error"] and q.counts()["pending"] == 1   # never dropped as 'rejected'

    assert Uploader(cfg(), q, client).sync_once()["uploaded"] == 1


def test_answers_from_something_in_front_of_the_server_never_reject_data():
    import httpx
    from adventuresync.core import USER_AGENT, _outcome
    assert _outcome(httpx.Response(200, json={"success": True})) == ("ok", "")
    # the server's own verdicts
    assert _outcome(httpx.Response(422, json={"detail": "bad row"}))[0] == "invalid"
    state, detail = _outcome(httpx.Response(403, json={"detail": "invalid token"}))
    assert state == "retry" and "invalid token" in detail
    # a block page, a size limit or an outage page written by a proxy: say so, keep the data
    for status, body in ((403, "<html>Attention Required</html>"), (413, "<html>too large</html>"), (502, ""), (403, '["x"]')):
        state, detail = _outcome(httpx.Response(status, text=body))
        assert state == "retry" and "did not come from the Adventure Hub server" in detail and USER_AGENT in detail


def test_settings_file_round_trip_and_addon_install(tmp_path):
    from adventuresync import gui
    from adventuresync.core import Config, load_config, save_config
    cfg = Config(url="https://hub.example", api_token='se"cret\\x', device_id="windows-pc",
                 wow_path=r"D:\Games\World of Warcraft", db_path=str(tmp_path / "q.db"))
    path = tmp_path / "nested" / "config.toml"
    save_config(path, cfg)
    assert load_config(path) == cfg                                 # quotes and backslashes survive
    assert gui.read_settings(tmp_path / "missing.toml").url == ""  # no file yet: an empty form, not a crash
    # the addon goes into every game version under the chosen folder
    game = tmp_path / "World of Warcraft"
    (game / "_forever_" / "Interface" / "AddOns" / "AdventureCollector").mkdir(parents=True)
    (game / "_forever_" / "Interface" / "AddOns" / "AdventureCollector" / "old.lua").write_text("stale")
    (game / "_classic_" / "Interface" / "AddOns").mkdir(parents=True)
    (game / "Data").mkdir()
    done = gui.install_addon(str(game))
    assert sorted(p.parent.parent.parent.name for p in done) == ["_classic_", "_forever_"]
    assert (done[0] / "AdventureCollector.toc").is_file() and (done[0] / "Core.lua").is_file()
    assert not (game / "_forever_" / "Interface" / "AddOns" / "AdventureCollector" / "old.lua").exists()
    import pytest
    with pytest.raises(RuntimeError):
        gui.install_addon(str(tmp_path / "nowhere"))


def test_each_character_uploads_with_its_own_token(tmp_path):
    """Two characters on one computer: each has a personal token; one without a token waits."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
    from fastapi.testclient import TestClient
    from app.main import create_app
    from adventuresync.core import Config, Queue, Uploader, load_config, save_config
    app = create_app(db_path=str(tmp_path / "s.db"), write_token="owner", read_token="r")
    lan, web = TestClient(app, client=("192.168.1.5", 1)), TestClient(app, client=("203.0.113.9", 1))
    token = lambda **body: lan.post("/api/v1/admin/tokens", json=body).json()["token"]      # noqa: E731
    ta, tb = token(label="A"), token(label="B")

    def fill(q):
        q.add_events([{"event_id": f"{c}:{i}", "character_id": c, "event_type": "LEVEL_UP", "timestamp": "2026-11-22T12:00:00Z",
                       "payload": {}} for c in ("A", "B", "C") for i in range(3)])
        for c in ("A", "B", "C"):
            q.add_snapshot(c, "2026-11-22T12:00:00Z", {"name": "角色" + c, "level": 5})

    cfg = Config(url="http://testserver", api_token="", device_id="pc", db_path=str(tmp_path / "q.db"), tokens={"A": ta, "B": tb})
    path = tmp_path / "config.toml"
    save_config(path, cfg)
    assert load_config(path) == cfg and "[characters]" in path.read_text()        # the tokens survive a save
    q = Queue(cfg.db_path)
    fill(q)
    up = Uploader(cfg, q, web)
    res = up.sync_once()
    assert (res["uploaded"], res["snapshots"], res["rejected"], res["error"], res["waiting"]) == (6, 2, 0, None, ["C"])
    assert q.counts()["pending"] == 3                                               # C's events are kept, not thrown away
    seen = {t["label"]: t["character_id"] for t in lan.get("/api/v1/admin/overview").json()["tokens"]}
    assert seen == {"A": "A", "B": "B"}                                             # each token became its character's
    check = {t["character_id"]: t for t in up.test_connection()["tokens"]}
    assert check["A"]["ok"] and check["A"]["owner"] == "A" and check["B"]["owner_name"] == "角色B"
    # the wrong token for a character: nothing is lost, it just waits and is reported
    mixed = Config(**{**cfg.__dict__, "tokens": {"A": ta, "B": tb, "C": ta}})
    wrong = Uploader(mixed, q, web)
    res = wrong.sync_once()
    assert (res["uploaded"], res["rejected"], res["waiting"]) == (0, 0, ["C"]) and q.counts()["pending"] == 3
    bad = {t["character_id"]: t for t in wrong.test_connection()["tokens"]}
    assert not bad["C"]["ok"] and bad["C"]["owner"] == "A" and bad["A"]["ok"]
    assert wrong.upload_hits("C", "log", [[1, 1, "cast", "x", 0] + [None] * 23])[0] == "skip"
    # with a general token as well, a character without its own uses that one
    owner = Uploader(Config(**{**cfg.__dict__, "api_token": "owner"}), q, web)
    res = owner.sync_once()
    assert (res["uploaded"], res["snapshots"], res["waiting"]) == (3, 1, [])
    assert {c["id"] for c in web.get("/api/v1/characters", headers={"Authorization": "Bearer r"}).json()["items"]} == {"A", "B", "C"}
    assert web.get("/api/v1/token", headers={"Authorization": "Bearer owner"}).json()["kind"] == "owner"
    assert web.get("/api/v1/token", headers={"Authorization": "Bearer nope"}).status_code == 403
