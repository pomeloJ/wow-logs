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
    assert web.get("/api/v1/me").json() == {"google": True, "account": None}
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
        assert web.get("/api/v1/me").json() == {"google": False, "account": None}
        assert web.get("/auth/google/start").status_code == 404
        assert web.get("/auth/google/callback", params={"state": "x", "code": "y"}).status_code == 404
