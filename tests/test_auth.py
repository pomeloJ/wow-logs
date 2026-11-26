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
    assert held["sync"]["token"] == mine and held["sync"]["characters"] == ["char-mine"] and held["view"] == []
    made = web.post("/api/v1/me/tokens", json={"label": "給朋友"}).json()
    share = made["made"]
    assert [(v["token"], v["label"]) for v in made["view"]] == [(share, "給朋友")]
    # a sharing token reads its maker's characters only, never chat, and uploads nothing
    reader = {"Authorization": f"Bearer {share}"}
    assert seen(web, **reader) == ["char-mine"]
    assert web.get("/api/v1/characters/char-other/snapshot", headers=reader).status_code in (403, 404)
    assert web.get("/api/v1/token", headers=reader).json() == {"kind": "read", "characters": [{"id": "char-mine", "name": "char-mine"}], "can_upload": False}
    assert web.put("/api/v1/characters/char-mine/snapshot", headers=reader, json={
        "captured_at": "2026-11-26T10:00:00+08:00", "device_id": "x", "data": {"name": "n"}}).status_code == 403
    # a change from a page of another site is refused
    assert web.post("/api/v1/me/tokens", json={"label": "x"}, headers={"origin": "https://evil.example"}).status_code == 403
    # only its own can be deleted; deleting stops it at once
    assert web.delete("/api/v1/me/tokens/vsomeoneelse").status_code == 404
    assert web.delete(f"/api/v1/me/tokens/{share}").json()["view"] == []
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
