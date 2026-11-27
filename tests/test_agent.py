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


def test_newer_version_is_found_fetched_and_named_after_its_version(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from adventuresync import __version__, update
    assert update.as_numbers("0.10.2") > update.as_numbers("0.9") and update.as_numbers("v1.2") == (1, 2)
    assert update.as_numbers("1.2-beta") == () and update.as_numbers(None) == ()
    web = TestClient(create_app(db_path=str(tmp_path / "s.db"), write_token="w", read_token="r"))
    # run from source: only the source's own version counts, and the server has the same one
    assert update.check("http://hub", web) is None
    found = update.check("http://hub/", web, current="0.0.1")
    assert (found["kind"], found["version"], found["url"]) == ("source", __version__, f"http://hub/download/client-source.zip?v={__version__}")
    assert not update.can_fetch(found)
    # as the one-file Windows program: the build for this kind of computer, when it is newer
    monkeypatch.setattr(update.sys, "frozen", True, raising=False)
    monkeypatch.setattr(update.sys, "platform", "win32")
    monkeypatch.setattr(update.platform, "system", lambda: "Windows")
    monkeypatch.setattr(update.platform, "machine", lambda: "AMD64")
    put = lambda name, version, body: web.put(f"/api/v1/builds/{name}", content=body, headers={"Authorization": "Bearer w"},   # noqa: E731
                                              params={"version": version} if version else {})
    put("AdventureSync-windows-x64.exe", None, b"MZ old")                      # no version known: never offered
    put("AdventureSync-macos-arm64.zip", "9.9.9", b"PK")                       # another kind of computer
    assert update.check("http://hub", web, current="0.2.0") is None
    put("AdventureSync-windows-x64.exe", "0.10.0", b"MZ" + b"n" * 70000)
    assert update.check("http://hub", web, current="0.10.0") is None and update.check("http://hub", web, current="0.11") is None
    found = update.check("http://hub", web, current="0.9.5")
    assert (found["kind"], found["version"], found["size"]) == ("build", "0.10.0", 70002) and update.can_fetch(found)
    # the address names the version, so a copy of an older one kept along the way is never what arrives
    assert found["url"] == "http://hub/download/builds/AdventureSync-windows-x64.exe?v=0.10.0-70002"
    seen = []
    path = update.fetch(found, tmp_path, lambda got, size: seen.append((got, size)), client=web)
    assert path == tmp_path / "AdventureSync-windows-x64-0.10.0.exe" and path.read_bytes() == b"MZ" + b"n" * 70000
    assert seen[-1] == (70002, 70002) and not list(tmp_path.glob("*.part"))
    # a download that is cut short leaves nothing behind that looks like the program
    path.unlink()
    with pytest.raises(RuntimeError):
        update.fetch({**found, "size": 99999}, tmp_path, client=web)
    assert not path.exists() and not list(tmp_path.glob("*.part"))
    with pytest.raises(RuntimeError):
        update.fetch({**found, "url": "http://hub/download/builds/nope.exe"}, tmp_path, client=web)


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


def test_one_token_covers_every_character_on_a_computer(tmp_path):
    """Three characters, one personal token: all go up. A second person's token on the same
    computer covers only what is filled in for it, and a character that is someone else's waits."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
    from fastapi.testclient import TestClient
    from app.main import create_app
    from adventuresync.core import Config, Queue, Uploader, load_config, save_config
    app = create_app(db_path=str(tmp_path / "s.db"), write_token="owner", read_token="r")
    lan, web = TestClient(app, client=("192.168.1.5", 1)), TestClient(app, client=("203.0.113.9", 1))
    token = lambda **body: lan.post("/api/v1/admin/tokens", json=body).json()["token"]      # noqa: E731
    ming, hua = token(label="小明"), token(label="小華")

    def fill(q, who):
        q.add_events([{"event_id": f"{c}:{i}", "character_id": c, "event_type": "LEVEL_UP", "timestamp": "2026-11-22T12:00:00Z",
                       "payload": {}} for c in who for i in range(3)])
        for c in who:
            q.add_snapshot(c, "2026-11-22T12:00:00Z", {"name": "角色" + c, "level": 5})

    cfg = Config(url="http://testserver", api_token=ming, device_id="pc", db_path=str(tmp_path / "q.db"))
    q = Queue(cfg.db_path)
    fill(q, "ABC")
    up = Uploader(cfg, q, web)
    res = up.sync_once()
    assert (res["uploaded"], res["snapshots"], res["rejected"], res["error"], res["waiting"]) == (9, 3, 0, None, [])
    held, = [t for t in lan.get("/api/v1/admin/overview").json()["tokens"] if t["label"] == "小明"]
    assert sorted(c["id"] for c in held["characters"]) == ["A", "B", "C"]
    check, = up.test_connection()["tokens"]
    assert check["ok"] and check["kind"] == "personal" and sorted(c["name"] for c in check["characters"]) == ["角色A", "角色B", "角色C"]
    # a second person plays on the same computer: their character gets their own token
    shared = Config(**{**cfg.__dict__, "tokens": {"D": hua, "A": hua}})
    path = tmp_path / "config.toml"
    save_config(path, shared)
    assert load_config(path) == shared and "[characters]" in path.read_text()        # the extra tokens survive a save
    fill(q, "D")
    q.add_events([{"event_id": "A:late", "character_id": "A", "event_type": "LEVEL_UP", "timestamp": "2026-11-22T12:00:00Z", "payload": {}}])
    two = Uploader(shared, q, web)
    res = two.sync_once()
    # D is new and becomes 小華's; A is 小明's, so 小華's token is refused for it: kept and reported, not lost
    assert (res["uploaded"], res["snapshots"], res["rejected"], res["waiting"]) == (3, 1, 0, ["A"]) and q.counts()["pending"] == 1
    judged = {t["character_id"]: t["ok"] for t in two.test_connection()["tokens"]}
    assert judged == {None: True, "A": False, "D": True}
    assert two.upload_hits("A", "log", [[1, 1, "cast", "x", 0] + [None] * 23])[0] == "skip"
    assert Uploader(cfg, q, web).sync_once()["uploaded"] == 1 and q.counts()["pending"] == 0     # the right token again: it goes up
    # no token at all for a character: it waits
    none = Uploader(Config(**{**cfg.__dict__, "api_token": "", "tokens": {"D": hua}}), q, web)
    fill(q, "E")
    res = none.sync_once()
    assert (res["uploaded"], res["waiting"]) == (0, ["E"]) and q.counts()["pending"] == 3
    assert web.get("/api/v1/token", headers={"Authorization": "Bearer owner"}).json()["kind"] == "owner"
    assert web.get("/api/v1/token", headers={"Authorization": "Bearer nope"}).status_code == 403


def test_a_new_version_with_an_odd_name_is_not_taken(tmp_path, monkeypatch):
    """The file name comes from the server and is used in a path here: only a plain name will do."""
    import pytest
    from adventuresync import update
    monkeypatch.setattr(update.sys, "frozen", True, raising=False)
    monkeypatch.setattr(update, "build_prefix", lambda: "AdventureSync-windows-x64")

    class Web:
        def __init__(self, builds):
            self.builds = builds

        def get(self, path):
            import httpx
            return httpx.Response(200, json={"builds": self.builds, "source_version": None})

    good = {"name": "AdventureSync-windows-x64.exe", "version": "9.0", "size": 3}
    for bad in ("AdventureSync-windows-x64/../../evil.exe", "AdventureSync-windows-x64\\..\\evil.exe",
                "AdventureSync-windows-x64.exe/x", "AdventureSync-windows-x64 .exe", "AdventureSync-windows-x64.sh"):
        assert update.check("http://hub", Web([{**good, "name": bad}]), current="0.1") is None, bad
        with pytest.raises(RuntimeError):
            update.target({**good, "name": bad}, tmp_path)
    with pytest.raises(RuntimeError):
        update.target({**good, "version": "../9"}, tmp_path)
    found = update.check("http://hub", Web([good]), current="0.1")
    assert found["name"] == good["name"] and update.target(found, tmp_path).parent == tmp_path


def test_the_addon_in_the_game_is_updated_from_the_server(tmp_path):
    """The server has a newer addon than the game: it is offered, fetched and swapped in, in
    every game version that has an older copy, and nowhere else."""
    import io
    import sys
    import zipfile
    from pathlib import Path
    import pytest
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
    from fastapi.testclient import TestClient
    from app.main import create_app
    from adventuresync import update
    web = TestClient(create_app(db_path=str(tmp_path / "s.db"), write_token="w", read_token="r"))
    offered = web.get("/api/v1/setup").json()["addon_version"]
    assert update.as_numbers(offered)

    def install(flavor, version):
        folder = tmp_path / "game" / flavor / "Interface" / "AddOns"
        (folder / "AdventureCollector").mkdir(parents=True)
        (folder / "AdventureCollector" / "AdventureCollector.toc").write_text(f"## Interface: 1\n## Version: {version}\n")
        (folder / "AdventureCollector" / "Old.lua").write_text("-- from the old version")
        return folder

    old, same = install("_forever_", "0.0.9"), install("_ptr_", offered)
    empty = tmp_path / "game" / "_classic_" / "Interface" / "AddOns"
    empty.mkdir(parents=True)
    dirs = [old, same, empty]
    assert [(Path(a["folder"]).parent.parent.name, a["version"]) for a in update.installed_addons(dirs)] == \
        [("_forever_", "0.0.9"), ("_ptr_", offered)]
    found = update.check_addon("http://hub", dirs, web)
    assert (found["version"], found["installed"], found["folders"]) == (offered, "0.0.9", [str(old)])
    assert update.check_addon("http://hub", [same, empty], web) is None           # up to date, or not installed: nothing to offer
    done = update.fetch_addon(found, web)
    assert done == [old / "AdventureCollector"] and update.toc_version(done[0]) == offered
    assert (done[0] / "Core.lua").is_file() and not (done[0] / "Old.lua").exists()   # replaced whole, not mixed with the old
    assert not (old / ".AdventureCollector.new").exists() and not (empty / "AdventureCollector").exists()
    assert update.check_addon("http://hub", dirs, web) is None
    assert update.toc_version(tmp_path) is None

    # a download that is not the addon is refused before anything in the game is touched
    def zipped(names):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            for name in names:
                z.writestr(name, "x")
        return out.getvalue()
    good = ["AdventureCollector/AdventureCollector.toc", "AdventureCollector/Core.lua"]
    assert sorted(update.addon_files(zipped(good + ["AdventureCollector/Bindings.xml", "AdventureCollector/sub/x.lua"]))) == \
        ["AdventureCollector.toc", "Bindings.xml", "Core.lua", "sub/x.lua"]
    for bad in (["AdventureCollector/../evil.lua"], ["../AdventureCollector/Core.lua"], ["Other/Core.lua"], ["AdventureCollector/run.exe"],
                ["AdventureCollector/C:/x.lua"], ["AdventureCollector\\\\evil.lua"], ["/AdventureCollector/Core.lua"]):
        with pytest.raises(RuntimeError):
            update.addon_files(zipped(good + bad))
    with pytest.raises(RuntimeError):
        update.addon_files(zipped(["AdventureCollector/Core.lua"]))                # no .toc: not the addon

    class Broken:
        def get(self, path):
            import httpx
            return httpx.Response(200, content=b"<html>not a zip</html>")
    install("_broken_", "0.0.1")
    target = tmp_path / "game" / "_broken_" / "Interface" / "AddOns"
    with pytest.raises(RuntimeError):
        update.fetch_addon({"version": offered, "folders": [str(target)], "url": "x"}, Broken())
    assert (target / "AdventureCollector" / "Old.lua").is_file()                  # the old addon is still there


def test_screenshot_setting_is_read_from_and_written_to_the_server(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from adventuresync.core import Config, Queue, Uploader
    web = TestClient(create_app(db_path=str(tmp_path / "s.db"), write_token="w", read_token="r"))
    up = Uploader(Config(url="http://hub", api_token="w"), Queue(":memory:"), client=web)
    assert up.screenshots_on() is False
    assert up.set_screenshots(True) == [] and up.screenshots_on() is True
    # changed from another computer: noticed once the minute is up
    other = Uploader(Config(url="http://hub", api_token="w"), Queue(":memory:"), client=web)
    other.set_screenshots(False)
    assert up.screenshots_on() is True and up.settings(fresh=True)["upload_screenshots"] is False
    # a token the server refuses changes nothing, and says so
    bad = Uploader(Config(url="http://hub", api_token="nope"), Queue(":memory:"), client=web)
    assert bad.set_screenshots(True) and bad.screenshots_on() is False


def test_how_many_screenshots_to_keep_is_set_from_the_sync_program(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from adventuresync.core import Config, Queue, Uploader
    web = TestClient(create_app(db_path=str(tmp_path / "s.db"), write_token="w", read_token="r"))
    up = Uploader(Config(url="http://hub", api_token="w"), Queue(":memory:"), client=web)
    assert up.settings()["keep_screenshots"] == 50
    assert up.set_settings(keep_screenshots=20) == [] and up.settings()["keep_screenshots"] == 20
    assert up.settings()["upload_screenshots"] is False                   # the other choice untouched
    assert up.set_settings(keep_screenshots=9999)                         # refused by the server, and said so


def test_the_window_shows_the_servers_copy_of_a_character_when_it_is_newer(tmp_path):
    """A personal token brings every character it holds, played on this computer or another;
    the server's own token only the ones here. The cards show the newer copy, and either can be picked."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
    from fastapi.testclient import TestClient
    from app.main import create_app
    from adventuresync.core import Config, Queue, Uploader, epoch_of
    from adventuresync.gui import merge_cards
    app = create_app(db_path=str(tmp_path / "s.db"), write_token="owner", read_token="r")
    lan, web = TestClient(app, client=("192.168.1.5", 1)), TestClient(app, client=("203.0.113.9", 1))
    ming = lan.post("/api/v1/admin/tokens", json={"label": "小明"}).json()["token"]
    # the mac uploaded A (level 12, later) and B (only played there)
    mac = Config(url="http://testserver", api_token=ming, device_id="mac", db_path=str(tmp_path / "mac.db"))
    q = Queue(mac.db_path)
    q.add_snapshot("A", "2026-11-26T20:00:00Z", {"name": "小明德", "level": 12, "money": 500})
    q.add_snapshot("B", "2026-11-26T19:00:00Z", {"name": "小明法", "level": 3})
    assert Uploader(mac, q, web).sync_once()["snapshots"] == 2
    pc = Config(url="http://testserver", api_token=ming, device_id="windows-pc", db_path=str(tmp_path / "pc.db"))
    cloud = {c["id"]: c for c in Uploader(pc, Queue(pc.db_path), web).cloud_characters(["A"])}
    assert set(cloud) == {"A", "B"} and cloud["A"]["device_id"] == "mac" and cloud["A"]["data"]["level"] == 12
    assert cloud["A"]["captured_at"] == epoch_of("2026-11-26T20:00:00Z")
    # the server's own token: only what is on this computer, never everyone's
    owner = Config(**{**pc.__dict__, "api_token": "owner"})
    assert [c["id"] for c in Uploader(owner, Queue(pc.db_path), web).cloud_characters(["A"])] == ["A"]
    assert Uploader(Config(**{**pc.__dict__, "api_token": "nope"}), Queue(pc.db_path), web).cloud_characters(["A"]) == []

    # this computer last saved A at level 10, three hours earlier
    local = [{"id": "A", "name": "小明德", "level": 10, "captured_at": epoch_of("2026-11-26T17:00:00Z")}]
    shown = {e["id"]: e for e in merge_cards(local, list(cloud.values()), {})}
    assert shown["A"]["view"] == "cloud" and shown["A"]["card"]["level"] == 12 and shown["A"]["local"]["level"] == 10
    assert shown["B"]["view"] == "cloud" and shown["B"]["local"] is None
    picked = {e["id"]: e for e in merge_cards(local, list(cloud.values()), {"A": "local"})}
    assert picked["A"]["view"] == "local" and picked["A"]["card"]["level"] == 10                  # picked by hand
    # saved here after the mac: this computer's copy, until it goes up
    later = [{**local[0], "level": 13, "captured_at": epoch_of("2026-11-26T21:00:00Z")}]
    assert {e["id"]: e["view"] for e in merge_cards(later, list(cloud.values()), {})}["A"] == "local"
    assert merge_cards([], [], {}) == []


def test_the_window_shows_full_names_and_remembers_syncing(tmp_path):
    from adventuresync.gui import card_of, merge_cards, remember, remembered
    assert card_of("A", {"name": "亞克", "full_name": "亞克 斯迪"}, 1)["name"] == "亞克 斯迪"
    assert card_of("A", {"name": "亞克"}, 1)["name"] == "亞克" and card_of("A", {}, 1)["name"] == "A"
    # a file from an older addon lacks the full name: the server's copy has it
    here = card_of("A", {"name": "亞克", "level": 10}, 200)
    there = {"id": "A", "captured_at": 100, "device_id": "pc", "data": {"name": "亞克", "full_name": "亞克 斯迪", "level": 9}}
    shown, = merge_cards([here], [there], {})
    assert shown["view"] == "local" and shown["card"]["name"] == "亞克 斯迪" and shown["card"]["level"] == 10
    other = {**there, "data": {"name": "別人", "full_name": "別人 名字"}}       # a renamed character keeps its own name
    assert merge_cards([here], [other], {})[0]["card"]["name"] == "亞克"
    # whether syncing was on, beside the settings file
    path = tmp_path / "config.toml"
    assert remembered(path) == {}
    remember(path, auto=True)
    assert remembered(path) == {"auto": True}
    remember(path, auto=False)
    assert remembered(path)["auto"] is False
    (tmp_path / "window.json").write_text("not json", encoding="utf-8")
    assert remembered(path) == {}
