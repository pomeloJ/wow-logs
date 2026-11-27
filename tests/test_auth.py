"""Signing in with Google, with Google itself played by a fake: the flow, and what it refuses."""
import base64
import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import READ_TOKEN, TOKEN

GOOGLE = {"client_id": "cid.apps.googleusercontent.com", "client_secret": "GOCSPX-test", "public_url": "http://testserver"}


def id_token(**claims):
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return "eyJhbGciOiJSUzI1NiJ9." + body + ".sig"


class FakeGoogle:
    """Answers the server's code exchange with whatever this test wants Google to say."""
    def __init__(self):
        self.calls, self.claims = [], {}

    def __call__(self, settings, code, verifier):
        self.calls.append((code, verifier))
        return {"id_token": id_token(**self.claims)}


@pytest.fixture
def site(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    google = FakeGoogle()
    app = create_app(str(tmp_path / "s.db"), TOKEN, READ_TOKEN, google=dict(GOOGLE), google_exchange=google)
    return TestClient(app, follow_redirects=False), google


def begin(web, next_path="/"):
    """Press the button: returns what the server sent to Google."""
    r = web.get("/auth/google/start", params={"next": next_path})
    assert r.status_code == 302
    sent = urlsplit(r.headers["location"])
    assert sent.netloc == "accounts.google.com"
    return {k: v[0] for k, v in parse_qs(sent.query).items()}


def good_claims(asked, **change):
    claims = {"iss": "https://accounts.google.com", "aud": GOOGLE["client_id"], "sub": "1234567890",
              "email": "player@gmail.com", "email_verified": True, "name": "玩家", "picture": "https://lh3/p.jpg",
              "nonce": asked["nonce"], "exp": int(time.time()) + 3600}
    claims.update(change)
    return claims


def test_signing_in_and_out(site):
    web, google = site
    assert web.get("/api/v1/me").json() == {"google": True, "account": None, "link": None}
    asked = begin(web, "/#map")
    assert asked["redirect_uri"] == "http://testserver/auth/google/callback" and asked["client_id"] == GOOGLE["client_id"]
    assert asked["scope"] == "openid email profile" and asked["code_challenge_method"] == "S256"
    google.claims = good_claims(asked)
    r = web.get("/auth/google/callback", params={"state": asked["state"], "code": "the-code"})
    assert r.status_code == 302 and r.headers["location"] == "/#map"
    cookie = r.headers["set-cookie"]
    assert "ah_session=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    # the server proved it started this attempt: the secret half of the challenge went to Google
    code, verifier = google.calls[0]
    import hashlib
    assert code == "the-code" and asked["code_challenge"] == base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert web.get("/api/v1/me").json()["account"] == {"email": "player@gmail.com", "name": "玩家", "picture": "https://lh3/p.jpg"}
    # signing in does not open the data: that waits for accounts to be tied to tokens
    assert web.get("/api/v1/characters").status_code == 401
    # the same answer cannot be used twice
    again = web.get("/auth/google/callback", params={"state": asked["state"], "code": "the-code"})
    assert again.status_code == 400 and len(google.calls) == 1
    assert web.post("/auth/logout").json() == {"success": True}
    assert web.get("/api/v1/me").json()["account"] is None


def test_only_the_session_hash_is_stored(site, tmp_path):
    import sqlite3
    web, google = site
    asked = begin(web)
    google.claims = good_claims(asked)
    web.get("/auth/google/callback", params={"state": asked["state"], "code": "c"})
    sid = web.cookies.get("ah_session")
    stored = [r[0] for r in sqlite3.connect(tmp_path / "s.db").execute("SELECT id_hash FROM web_sessions")]
    assert sid and stored and sid not in stored


@pytest.mark.parametrize("change", [
    {"aud": "someone-else.apps.googleusercontent.com"},       # meant for another site
    {"iss": "https://evil.example"},
    {"nonce": "not-the-one-sent"},                          # an answer from another attempt
    {"exp": int(time.time()) - 3600},
    {"email_verified": False},
    {"sub": ""},
])
def test_answers_that_are_refused(site, change):
    web, google = site
    asked = begin(web)
    google.claims = good_claims(asked, **change)
    r = web.get("/auth/google/callback", params={"state": asked["state"], "code": "c"})
    assert r.status_code in (400, 403) and "ah_session=" not in r.headers.get("set-cookie", "")
    assert web.get("/api/v1/me").json()["account"] is None


def test_an_answer_must_come_back_to_the_browser_that_asked(site):
    web, google = site
    asked = begin(web)
    google.claims = good_claims(asked)
    web.cookies.clear()                                      # another browser holding the link
    r = web.get("/auth/google/callback", params={"state": asked["state"], "code": "c"})
    assert r.status_code == 400 and not google.calls
    assert web.get("/auth/google/callback", params={"state": "made-up", "code": "c"}).status_code == 400
    assert web.get("/auth/google/callback", params={"error": "access_denied"}).status_code == 400


def test_never_sent_anywhere_else_after_signing_in(site):
    web, google = site
    for target in ("//evil.example/x", "https://evil.example", "/\\evil.example"):
        asked = begin(web, target)
        google.claims = good_claims(asked)
        r = web.get("/auth/google/callback", params={"state": asked["state"], "code": "c"})
        assert r.headers["location"] == "/"


def test_started_elsewhere_moves_to_the_public_address_first(site):
    web, _ = site
    r = web.get("/auth/google/start", params={"next": "/"}, headers={"host": "192.168.1.121:8080"})
    assert r.status_code == 302 and r.headers["location"] == "http://testserver/auth/google/start?next=%2F"
    assert "ah_google_state" not in r.headers.get("set-cookie", "")


def test_off_without_settings(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    for google in ({}, {**GOOGLE, "client_secret": "PASTE_CLIENT_SECRET_HERE"}):
        web = TestClient(create_app(str(tmp_path / "off.db"), TOKEN, READ_TOKEN, google=google), follow_redirects=False)
        assert web.get("/api/v1/me").json() == {"google": False, "account": None, "link": None}
        assert web.get("/auth/google/start").status_code == 404
        assert web.get("/auth/google/callback", params={"state": "x", "code": "y"}).status_code == 404


# ---------------------------------------------------------------- binding an account to a token
def signed_in(tmp_path, client=("192.168.1.5", 50000)):
    """A site on the LAN (so /admin answers), signed in with Google, not yet bound."""
    from fastapi.testclient import TestClient
    from app.main import create_app
    google = FakeGoogle()
    app = create_app(str(tmp_path / "b.db"), TOKEN, READ_TOKEN, google=dict(GOOGLE), google_exchange=google)
    web = TestClient(app, follow_redirects=False, client=client)
    asked = begin(web)
    google.claims = good_claims(asked)
    assert web.get("/auth/google/callback", params={"state": asked["state"], "code": "c"}).status_code == 302
    return web


def upload(web, token, cid):
    r = web.put(f"/api/v1/characters/{cid}/snapshot", headers={"Authorization": f"Bearer {token}"}, json={
        "captured_at": "2026-11-21T10:00:00+08:00", "device_id": "pc", "data": {"name": cid, "realm": "R", "class": "DRUID", "level": 20}})
    assert r.status_code == 200, r.text


def seen(web, **headers):
    r = web.get("/api/v1/characters", headers=headers)
    return sorted(c["id"] for c in r.json()["items"]) if r.status_code == 200 else r.status_code


def test_bound_to_a_persons_token_sees_that_persons_characters(tmp_path):
    web = signed_in(tmp_path)
    mine = web.post("/api/v1/admin/tokens", json={"label": "小明"}).json()["token"]
    upload(web, mine, "char-mine")
    upload(web, TOKEN, "char-other")                     # someone else's, uploaded with the owner's token
    assert seen(web) == 401                              # signed in, not bound: nothing
    assert web.get("/api/v1/me").json()["link"] is None
    assert web.post("/api/v1/me/link", json={"token": "wrong"}).status_code == 403
    r = web.post("/api/v1/me/link", json={"token": f"  {mine} "})
    assert r.status_code == 200 and r.json()["link"] == {"kind": "personal", "characters": ["char-mine"]}
    assert seen(web) == ["char-mine"]
    assert web.get("/api/v1/characters/char-other/snapshot").status_code in (403, 404)
    # a token sent along wins over the sign-in
    assert seen(web, Authorization=f"Bearer {READ_TOKEN}") == ["char-mine", "char-other"]
    assert seen(web, Authorization="Bearer wrong") == 403
    # signed in, the browser still only reads: uploading takes a token
    assert web.post("/api/v1/events/batch", json={"schema_version": 1, "device_id": "pc", "events": []}).status_code == 401
    # the admin page shows it, and can undo it
    listed, = web.get("/api/v1/admin/overview").json()["google"]
    assert (listed["email"], listed["kind"], listed["token_label"]) == ("player@gmail.com", "personal", "小明")
    assert web.delete(f"/api/v1/admin/google/{listed['sub']}").status_code == 200
    assert seen(web) == 401 and web.get("/api/v1/admin/overview").json()["google"][0]["kind"] is None


def test_bound_to_the_servers_own_token_sees_everything(tmp_path):
    web = signed_in(tmp_path)
    upload(web, TOKEN, "char-a")
    for server_token in (READ_TOKEN, TOKEN):
        r = web.post("/api/v1/me/link", json={"token": server_token})
        assert r.json()["link"] == {"kind": "all", "characters": []}
        assert seen(web) == ["char-a"]
    import sqlite3                                      # the server's own tokens are never stored
    stored = [r[0] for r in sqlite3.connect(tmp_path / "b.db").execute("SELECT token FROM google_links")]
    assert stored == [None]
    assert web.post("/api/v1/me/unlink").json()["link"] is None and seen(web) == 401


def test_deleting_the_token_cuts_off_the_account(tmp_path):
    web = signed_in(tmp_path)
    mine = web.post("/api/v1/admin/tokens", json={"label": "小明"}).json()["token"]
    upload(web, mine, "char-mine")
    web.post("/api/v1/me/link", json={"token": mine})
    assert seen(web) == ["char-mine"]
    assert web.delete(f"/api/v1/admin/tokens/{mine}").status_code == 200
    assert seen(web) == 401 and web.get("/api/v1/me").json()["link"] is None


def test_what_cannot_be_bound(tmp_path):
    import sqlite3
    web = signed_in(tmp_path)
    upload(web, TOKEN, "char-a")
    db = sqlite3.connect(tmp_path / "b.db")
    with db:
        db.execute("INSERT INTO voice_tokens VALUES ('voicecode1', 'char-a', '2026-11-26T00:00:00Z')")
    assert web.post("/api/v1/me/link", json={"token": "voicecode1"}).status_code == 400     # the voice page's code
    assert web.post("/api/v1/me/link", json={"token": ""}).status_code == 422
    web.post("/auth/logout")
    assert web.post("/api/v1/me/link", json={"token": READ_TOKEN}).status_code == 401       # signed out
    assert seen(web) == 401


# ---------------------------------------------------------------- a bound account's own tokens
def test_a_bound_account_manages_its_sharing_tokens(tmp_path):
    web = signed_in(tmp_path)
    mine = web.post("/api/v1/admin/tokens", json={"label": "小明"}).json()["token"]
    upload(web, mine, "char-mine")
    upload(web, TOKEN, "char-other")
    # signed in but not bound: a Google account alone makes no tokens
    assert web.get("/api/v1/me/tokens").status_code == 403
    assert web.post("/api/v1/me/tokens", json={"label": "x"}).status_code == 403
    web.post("/api/v1/me/link", json={"token": mine})
    held = web.get("/api/v1/me/tokens").json()
    assert [s["token"] for s in held["sync"]] == [mine] and held["characters"] == ["char-mine"] and held["view"] == []
    made = web.post("/api/v1/me/tokens", json={"label": "給朋友"}).json()
    share = made["made"]
    assert [(v["token"], v["label"]) for v in made["view"]] == [(share, "給朋友")]
    # a sharing token reads its maker's characters only, never chat, and uploads nothing
    reader = {"Authorization": f"Bearer {share}"}
    assert seen(web, **reader) == ["char-mine"]
    assert web.get("/api/v1/characters/char-other/snapshot", headers=reader).status_code in (403, 404)
    # a new one shows everything but group sessions and screenshots until its maker chooses otherwise
    assert web.get("/api/v1/token", headers=reader).json() == {"kind": "read", "characters": [{"id": "char-mine", "name": "char-mine"}],
                                                              "can_upload": False, "sections": ["location", "activity", "items", "quests", "character"]}
    assert web.put("/api/v1/characters/char-mine/snapshot", headers=reader, json={
        "captured_at": "2026-11-26T10:00:00+08:00", "device_id": "x", "data": {"name": "n"}}).status_code == 403
    # a change from a page of another site is refused
    assert web.post("/api/v1/me/tokens", json={"label": "x"}, headers={"origin": "https://evil.example"}).status_code == 403
    # only its own can be deleted; deleting stops it at once
    assert web.post("/api/v1/me/tokens/delete", json={"id": "0" * 16}).status_code == 404
    which = made["view"][0]["id"]
    assert share not in which                                           # rows are named by a fingerprint, never the token
    assert web.post("/api/v1/me/tokens/delete", json={"id": which}).json()["view"] == []
    assert seen(web, **reader) == 403


def test_existing_tokens_work_exactly_as_before_alongside_sharing_tokens(tmp_path):
    import sqlite3
    web = signed_in(tmp_path)
    mine = web.post("/api/v1/admin/tokens", json={"label": "小明"}).json()["token"]
    upload(web, mine, "char-mine")
    upload(web, TOKEN, "char-other")
    db = sqlite3.connect(tmp_path / "b.db")
    with db:
        db.execute("INSERT INTO voice_tokens VALUES ('voicecode1', 'char-mine', '2026-11-26T00:00:00Z')")
    before = {name: seen(web, Authorization=f"Bearer {t}") for name, t in
              (("owner", TOKEN), ("read", READ_TOKEN), ("personal", mine), ("voice", "voicecode1"), ("bad", "nope"))}
    web.post("/api/v1/me/link", json={"token": mine})
    share = web.post("/api/v1/me/tokens", json={}).json()["made"]
    after = {name: seen(web, Authorization=f"Bearer {t}") for name, t in
             (("owner", TOKEN), ("read", READ_TOKEN), ("personal", mine), ("voice", "voicecode1"), ("bad", "nope"))}
    assert before == after == {"owner": ["char-mine", "char-other"], "read": ["char-mine", "char-other"],
                               "personal": ["char-mine"], "voice": ["char-mine"], "bad": 403}
    # the personal token still uploads; deleting it in /admin takes its sharing tokens along
    upload(web, mine, "char-mine")
    assert web.delete(f"/api/v1/admin/tokens/{mine}").status_code == 200
    assert seen(web, Authorization=f"Bearer {share}") == 403


def test_an_account_bound_to_the_servers_token_shares_every_character(tmp_path):
    web = signed_in(tmp_path)
    upload(web, TOKEN, "char-a")
    web.post("/api/v1/me/link", json={"token": READ_TOKEN})
    held = web.get("/api/v1/me/tokens").json()
    assert held["kind"] == "all" and held["sync"] is None              # the server's own token is never shown
    share = web.post("/api/v1/me/tokens", json={"label": "全部"}).json()["made"]
    assert seen(web, Authorization=f"Bearer {share}") == ["char-a"]


def test_up_to_three_sync_tokens_each_uploading_as_the_same_person(tmp_path):
    web = signed_in(tmp_path)
    first = web.post("/api/v1/admin/tokens", json={"label": "小明"}).json()["token"]
    upload(web, first, "char-mine")
    web.post("/api/v1/me/link", json={"token": first})
    second = web.post("/api/v1/me/sync-tokens", json={"label": "筆電"}).json()["made"]
    third = web.post("/api/v1/me/sync-tokens", json={}).json()["made"]
    assert len(second) == len(third) == 32 and len(first) == 32
    assert web.post("/api/v1/me/sync-tokens", json={}).status_code == 409           # three at most
    held = web.get("/api/v1/me/tokens").json()
    assert [(s["token"], s["first"], s["label"]) for s in held["sync"]] == [(first, True, "小明"), (second, False, "筆電"), (third, False, None)]
    # each uploads and reads as the same person: a character uploaded with one belongs to all of them
    upload(web, second, "char-laptop")
    for t in (first, second, third):
        assert seen(web, Authorization=f"Bearer {t}") == ["char-laptop", "char-mine"]
        assert web.get("/api/v1/token", headers={"Authorization": f"Bearer {t}"}).json()["kind"] == "personal"
    assert web.get("/api/v1/me/tokens").json()["characters"] == ["char-mine", "char-laptop"]
    # a further one can be deleted; the first cannot
    ids = {s["token"]: s["id"] for s in held["sync"]}
    assert web.post("/api/v1/me/sync-tokens/delete", json={"id": ids[first]}).status_code == 409
    assert web.post("/api/v1/me/sync-tokens/delete", json={"id": ids[third]}).status_code == 200
    assert seen(web, Authorization=f"Bearer {third}") == 403
    # pasting a further sync token binds the account to the person, not to that token
    web.post("/api/v1/me/unlink")
    assert web.post("/api/v1/me/link", json={"token": second}).json()["link"] == {"kind": "personal", "characters": ["char-mine", "char-laptop"]}


def test_regenerating_a_sync_token_stops_only_that_one_and_keeps_everything(tmp_path):
    import sqlite3
    web = signed_in(tmp_path)
    first = web.post("/api/v1/admin/tokens", json={"label": "小明"}).json()["token"]
    upload(web, first, "char-mine")
    web.post("/api/v1/me/link", json={"token": first})
    second = web.post("/api/v1/me/sync-tokens", json={"label": "筆電"}).json()["made"]
    share = web.post("/api/v1/me/tokens", json={"label": "朋友"}).json()["made"]
    web.put("/api/v1/sync-settings", headers={"Authorization": f"Bearer {first}"}, json={"upload_screenshots": True})
    ids = {s["token"]: s["id"] for s in web.get("/api/v1/me/tokens").json()["sync"]}
    # the first: everything kept under it moves to the new one
    new_first = web.post("/api/v1/me/sync-tokens/regenerate", json={"id": ids[first]}).json()["made"]
    assert new_first != first and len(new_first) == 32
    assert seen(web, Authorization=f"Bearer {first}") == 403                         # the old one stops at once
    assert seen(web, Authorization=f"Bearer {new_first}") == ["char-mine"]          # the characters stayed
    assert seen(web, Authorization=f"Bearer {second}") == ["char-mine"]             # the other sync token is untouched
    assert seen(web, Authorization=f"Bearer {share}") == ["char-mine"]              # and so is what was shared
    assert seen(web) == ["char-mine"]                                               # the Google sign-in still works
    assert web.get("/api/v1/sync-settings", headers={"Authorization": f"Bearer {new_first}"}).json()["upload_screenshots"] is True
    upload(web, new_first, "char-mine")                                             # uploads as before
    # a further one: only that one changes
    held = web.get("/api/v1/me/tokens").json()
    sid = next(s["id"] for s in held["sync"] if s["token"] == second)
    new_second = web.post("/api/v1/me/sync-tokens/regenerate", json={"id": sid}).json()["made"]
    assert seen(web, Authorization=f"Bearer {second}") == 403 and seen(web, Authorization=f"Bearer {new_second}") == ["char-mine"]
    assert seen(web, Authorization=f"Bearer {new_first}") == ["char-mine"]
    # a sharing token gets a new address; the old link stops, the name stays
    vid = held["view"][0]["id"]
    new_share = web.post("/api/v1/me/tokens/regenerate", json={"id": vid}).json()
    assert seen(web, Authorization=f"Bearer {share}") == 403
    assert seen(web, Authorization=f"Bearer {new_share['made']}") == ["char-mine"] and new_share["view"][0]["label"] == "朋友"
    # nothing of the old first token is left anywhere
    db = sqlite3.connect(tmp_path / "b.db")
    for table, column in (("upload_tokens", "token"), ("token_characters", "token"), ("google_links", "token"),
                          ("view_tokens", "owner"), ("sync_aliases", "owner"), ("sync_settings", "token")):
        assert not db.execute(f"SELECT 1 FROM {table} WHERE {column}=?", (first,)).fetchone(), table


def test_tokens_made_before_keep_working_and_short_ones_are_still_accepted(tmp_path):
    import sqlite3
    web = signed_in(tmp_path)
    db = sqlite3.connect(tmp_path / "b.db")
    with db:                                             # a 16-character token from before
        db.execute("INSERT INTO upload_tokens VALUES ('abcdefghjkmnpqrs', 'old', NULL, '2026-11-21T00:00:00Z', NULL, NULL)")
    upload(web, "abcdefghjkmnpqrs", "char-old")
    assert seen(web, Authorization="Bearer abcdefghjkmnpqrs") == ["char-old"]
    web.post("/api/v1/me/link", json={"token": "abcdefghjkmnpqrs"})
    second = web.post("/api/v1/me/sync-tokens", json={}).json()["made"]
    assert seen(web, Authorization=f"Bearer {second}") == ["char-old"]
    assert seen(web, Authorization="Bearer abcdefghjkmnpqrs") == ["char-old"]      # adding one changes nothing for it


# ---------------------------------------------------------------- what a sharing token shows
def full_record(web, token, cid="char-mine"):
    r = web.put(f"/api/v1/characters/{cid}/snapshot", headers={"Authorization": f"Bearer {token}"}, json={
        "captured_at": "2026-11-26T10:00:00+08:00", "device_id": "pc", "data": {
            "name": "小明", "class": "DRUID", "level": 20, "xp": 100, "xp_max": 900, "money": 12345, "map_id": 1439, "x": 40.0, "y": 50.0,
            "zone_name": "黑海岸", "equipment": [{"item_id": 1}], "bags": {"items": []}, "quests": [{"id": 5}], "quests_done": [1, 2],
            "stats": {"strength": 30}, "professions": [], "reputations": [], "played_total": 999}})
    assert r.status_code == 200, r.text
    ev = lambda i, t, p: {"event_id": f"{cid}:{i:010d}", "character_id": cid, "event_type": t,   # noqa: E731
                          "timestamp": f"2026-11-26T10:0{i}:00+08:00", "payload": p}
    r = web.post("/api/v1/events/batch", headers={"Authorization": f"Bearer {token}"}, json={"schema_version": 1, "device_id": "pc", "events": [
        ev(1, "LOCATION_SAMPLE", {"map_id": 1439, "x": 40.0, "y": 50.0}),
        ev(2, "MOB_KILLED", {"monster_name": "狼", "map_id": 1439, "x": 41.0, "y": 51.0, "zone_name": "黑海岸"}),
        ev(3, "ITEM_LOOTED", {"item_id": 2, "item_name": "狼皮", "map_id": 1439}),
        ev(4, "QUEST_COMPLETED", {"quest_id": 5, "quest_name": "狼群"}),
        ev(5, "MONEY_CHANGED", {"delta": 500, "money": 12345})]})
    assert r.status_code == 200, r.text


def test_a_sharing_token_shows_only_the_parts_chosen(tmp_path):
    web = signed_in(tmp_path)
    mine = web.post("/api/v1/admin/tokens", json={"label": "小明"}).json()["token"]
    full_record(web, mine)
    web.post("/api/v1/me/link", json={"token": mine})
    made = web.post("/api/v1/me/tokens", json={"label": "只看動態", "sections": ["activity"]}).json()
    assert [p["key"] for p in made["parts"]] == ["location", "activity", "items", "quests", "character", "groups", "screenshots"]
    assert made["view"][0]["sections"] == ["activity"]
    h = {"Authorization": f"Bearer {made['made']}"}
    get = lambda path: web.get(path, headers=h)     # noqa: E731
    # always: who the character is
    assert [c["id"] for c in get("/api/v1/characters").json()["items"]] == ["char-mine"]
    data = get("/api/v1/characters/char-mine/snapshot").json()["data"]
    assert (data["name"], data["level"], data["xp"]) == ("小明", 20, 100) and data["played_total"] == 999
    for hidden_field in ("money", "map_id", "x", "zone_name", "equipment", "bags", "quests", "quests_done", "stats"):
        assert hidden_field not in data, hidden_field
    # whole routes of a hidden part are refused, and say which part
    for path, part in (("/api/v1/locations?character_id=char-mine", "location"), ("/api/v1/characters/char-mine/items", "items"),
                       ("/api/v1/characters/char-mine/quests", "quests"), ("/api/v1/screenshots", "screenshots"),
                       ("/api/v1/characters/char-mine/groups", "groups"), ("/api/v1/characters/char-mine/screenshots/latest", "screenshots")):
        r = get(path)
        assert r.status_code == 403 and r.json()["detail"]["hidden"] == part, path
    # the parts shown are trimmed: no loot, money, quests or positions in the events and totals
    events = get("/api/v1/events?character_id=char-mine").json()["items"]
    assert [e["event_type"] for e in events] == ["MOB_KILLED"] and events[0]["payload"] == {"monster_name": "狼"}
    act = get("/api/v1/stats/activity?character_id=char-mine").json()
    assert act["kills"] == 1 and (act["money_in"], act["items_looted"], act["top_items"]) == (0, 0, [])
    assert set(act["counts"]) == {"MOB_KILLED"}
    assert set(get("/api/v1/stats/summary").json()["by_type"]) == {"MOB_KILLED"}
    assert web.get("/api/v1/token", headers=h).json()["sections"] == ["activity"]
    # changed in the panel: in effect at the next request
    tid = made["view"][0]["id"]
    assert web.post("/api/v1/me/tokens/sections", json={"id": tid, "sections": ["activity", "location", "items"]}).status_code == 200
    assert get("/api/v1/locations?character_id=char-mine").status_code == 200
    data = get("/api/v1/characters/char-mine/snapshot").json()["data"]
    assert data["money"] == 12345 and data["map_id"] == 1439 and "quests" not in data
    assert web.post("/api/v1/me/tokens/sections", json={"id": tid, "sections": ["nonsense"]}).status_code == 400


def test_a_sharing_token_with_every_part_or_none_chosen_shows_everything(tmp_path):
    import sqlite3
    web = signed_in(tmp_path)
    mine = web.post("/api/v1/admin/tokens", json={"label": "小明"}).json()["token"]
    full_record(web, mine)
    web.post("/api/v1/me/link", json={"token": mine})
    made = web.post("/api/v1/me/tokens", json={"label": "全部", "sections": ["location", "activity", "items", "quests", "character",
                                                                         "groups", "screenshots"]}).json()
    every = [p["key"] for p in made["parts"]]
    assert made["view"][0]["sections"] == every
    h = {"Authorization": f"Bearer {made['made']}"}
    assert web.get("/api/v1/characters/char-mine/snapshot", headers=h).json()["data"]["money"] == 12345
    assert len(web.get("/api/v1/events?character_id=char-mine", headers=h).json()["items"]) == 5
    assert "sections" not in web.get("/api/v1/token", headers=h).json()
    # choosing every part stores no limit at all, the same as a token made before parts existed
    web.post("/api/v1/me/tokens/sections", json={"id": made["view"][0]["id"], "sections": every})
    assert sqlite3.connect(tmp_path / "b.db").execute("SELECT sections FROM view_tokens").fetchone()[0] is None


def test_every_read_route_belongs_to_a_part_or_is_always_open(tmp_path):
    """A route added later must be placed in sections.py, or a limited sharing token is refused it."""
    from app import sections
    from app.main import create_app
    app = create_app(str(tmp_path / "r.db"), TOKEN, READ_TOKEN)
    apart = ("/api/v1/token", "/api/v1/health", "/api/v1/setup", "/api/v1/admin/", "/api/v1/me", "/api/v1/sync-settings",
             "/api/v1/shared/party/")
    unplaced = [r.path for r in app.routes if "GET" in (getattr(r, "methods", None) or ()) and r.path.startswith("/api/v1")
                and not r.path.startswith(apart) and sections.route_part(r.path) is None]
    assert unplaced == []


def test_a_sharing_token_can_show_only_some_characters(tmp_path):
    web = signed_in(tmp_path)
    mine = web.post("/api/v1/admin/tokens", json={"label": "小明"}).json()["token"]
    for cid in ("char-a", "char-b", "char-c"):
        upload(web, mine, cid)
    upload(web, TOKEN, "char-other")                     # someone else's
    web.post("/api/v1/me/link", json={"token": mine})
    made = web.post("/api/v1/me/tokens", json={"label": "朋友"}).json()
    share, = made["view"]
    assert share["characters"] is None and sorted(c["id"] for c in made["owned"]) == ["char-a", "char-b", "char-c"]
    S = {"Authorization": f"Bearer {made['made']}"}
    assert seen(web, **S) == ["char-a", "char-b", "char-c"]          # all of them, as every token so far
    # only some
    r = web.post("/api/v1/me/tokens/characters", json={"id": share["id"], "characters": ["char-b", "char-a"]})
    assert r.status_code == 200 and sorted(r.json()["view"][0]["characters"]) == ["char-a", "char-b"]
    assert seen(web, **S) == ["char-a", "char-b"]
    left = web.get("/api/v1/characters/char-c/snapshot", headers=S)
    assert left.status_code == 403 and left.json()["detail"]["error"] == "character_not_authorized"
    assert sorted(c["id"] for c in web.get("/api/v1/token", headers=S).json()["characters"]) == ["char-a", "char-b"]
    # never none, never someone else's
    for wrong in ([], ["char-other"], ["char-a", "nobody"]):
        assert web.post("/api/v1/me/tokens/characters", json={"id": share["id"], "characters": wrong}).status_code == 400, wrong
    assert seen(web, **S) == ["char-a", "char-b"]
    # back to all, later ones included
    web.post("/api/v1/me/tokens/characters", json={"id": share["id"], "characters": None})
    upload(web, mine, "char-d")
    assert seen(web, **S) == ["char-a", "char-b", "char-c", "char-d"]
    # chosen when made
    one = web.post("/api/v1/me/tokens", json={"label": "一隻", "characters": ["char-c"]}).json()["made"]
    assert seen(web, Authorization=f"Bearer {one}") == ["char-c"]
    assert web.post("/api/v1/me/tokens/characters", json={"id": share["id"], "characters": ["char-a"]},
                    headers={"origin": "https://evil.example"}).status_code == 403
