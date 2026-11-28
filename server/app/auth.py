"""Signing in to the web pages with a Google account.

It finds out who is at the browser (which Google account), and lets that account be bound,
once, to a token: the person pastes their own upload token and from then on signing in is
enough. Bound to a person's token, the account sees that person's characters; bound to the
server's read or owner token, every character. A signed-in browser only ever reads: uploading
still takes a token, so the sync program and the share links are untouched by any of this.

The flow is Google's "authorization code" one, run entirely by the server:

* /auth/google/start sends the browser to Google with a one-time `state` (also kept in a
  short-lived cookie, so the answer must come back to the same browser), a `nonce`, and a PKCE
  challenge whose secret half never leaves the server.
* Google sends the browser back to /auth/google/callback with a code. The server trades it for
  the account's details straight with Google over HTTPS, checks them, and starts a session.
* The session is a random id in an HttpOnly cookie that page scripts cannot read; only its
  SHA-256 is stored, so a copy of the database signs no one in.

Settings, from the environment (on the server: /etc/adventure-hub/google.env, which deploying
never touches): AH_GOOGLE_CLIENT_ID, AH_GOOGLE_CLIENT_SECRET, and AH_PUBLIC_URL, the address the
site is reached at, which Google must know (Console: "Authorized redirect URIs" holds
AH_PUBLIC_URL + /auth/google/callback). Without them, signing in with Google is simply off.
"""
import base64
import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlencode, urlsplit

from fastapi import HTTPException, Request

from . import sections as parts
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ISSUERS = ("accounts.google.com", "https://accounts.google.com")
CALLBACK = "/auth/google/callback"
SESSION_COOKIE = "ah_session"
STATE_COOKIE = "ah_google_state"
SESSION_DAYS = 30
PENDING_SECONDS = 600              # time allowed on Google's pages before the attempt lapses
NO_STORE = {"Cache-Control": "no-store"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS google_accounts (
    sub TEXT PRIMARY KEY, email TEXT NOT NULL, name TEXT, picture TEXT,
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS google_pending (
    state TEXT PRIMARY KEY, nonce TEXT NOT NULL, verifier TEXT NOT NULL, next TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS web_sessions (
    id_hash TEXT PRIMARY KEY, sub TEXT NOT NULL, created_at TEXT NOT NULL, expires REAL NOT NULL
);
-- kind 'personal': sees what that upload token sees (token holds it); kind 'all': every character.
CREATE TABLE IF NOT EXISTS google_links (
    sub TEXT PRIMARY KEY, kind TEXT NOT NULL, token TEXT, linked_at TEXT NOT NULL
);
-- Tokens that only read, made by a person to share what they recorded. owner is that person's
-- upload token, or '*' for an account bound to the server's own (every character). sections
-- is kept for choosing what a token shows; NULL means everything a reader sees.
CREATE TABLE IF NOT EXISTS view_tokens (
    token TEXT PRIMARY KEY, owner TEXT NOT NULL, label TEXT, sections TEXT,
    created_at TEXT NOT NULL, last_used_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_view_tokens_owner ON view_tokens(owner);
"""


def settings_from_env() -> Dict[str, str]:
    return {"client_id": os.environ.get("AH_GOOGLE_CLIENT_ID", "").strip(),
            "client_secret": os.environ.get("AH_GOOGLE_CLIENT_SECRET", "").strip(),
            "public_url": os.environ.get("AH_PUBLIC_URL", "").strip().rstrip("/")}


def enabled(s: Dict[str, str]) -> bool:
    """All three settings given, and the secret is not the placeholder of the settings file."""
    secret = s.get("client_secret", "")
    return bool(s.get("client_id") and secret and s.get("public_url")) and not secret.upper().startswith("PASTE")


def exchange_code(s: Dict[str, str], code: str, verifier: str) -> Dict[str, Any]:
    """Trade the code Google handed the browser for the account's details, straight with Google."""
    import httpx                    # only needed once Google sign-in is switched on
    r = httpx.post(TOKEN_URL, timeout=15, data={
        "code": code, "client_id": s["client_id"], "client_secret": s["client_secret"],
        "redirect_uri": s["public_url"] + CALLBACK, "grant_type": "authorization_code", "code_verifier": verifier})
    if r.status_code != 200:
        raise ValueError(f"Google refused the code (HTTP {r.status_code})")
    return r.json()


def claims_of(id_token: str) -> Dict[str, Any]:
    """The middle part of the ID token. Its signature is not checked: the token came straight
    from Google's token endpoint over HTTPS, which OpenID Connect allows in place of it."""
    try:
        part = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError) as e:
        raise ValueError("unreadable ID token") from e


class LinkIn(BaseModel):
    token: str = Field(min_length=1, max_length=200)


class LabelIn(BaseModel):
    label: str = Field("", max_length=40)


class PickIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)        # a token's fingerprint, never the token itself


class SectionsIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    sections: List[str] = Field(max_length=20)          # the parts it shows (sections.py); all of them: no limit


class NewViewIn(BaseModel):
    label: str = Field("", max_length=40)
    sections: Optional[List[str]] = Field(None, max_length=20)     # left out: parts.SHARE_DEFAULT
    characters: Optional[List[str]] = Field(None, max_length=200)  # left out: all of them, later ones too


class ShownIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    characters: Optional[List[str]] = Field(None, max_length=200)  # null: all of them, later ones too


MAX_SYNC_TOKENS = 3            # the person's own token and two more, for more computers
MAX_VIEW_TOKENS = 20
MAX_VOICE_TOKENS = 5
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"          # as for the tokens /admin makes: no look-alikes


def new_sync_secret() -> str:
    """32 characters, twice as long as the first tokens (which keep working)."""
    return "".join(secrets.choice(ALPHABET) for _ in range(32))


def new_view_secret() -> str:
    return "v" + secrets.token_urlsafe(24)


def new_voice_secret() -> str:
    """Short enough to say or type into an assistant's settings, and only the letters that cannot
    be mistaken for one another. Longer than a character's own voice code (12), so never the same."""
    return "".join(secrets.choice(ALPHABET) for _ in range(16))


def fingerprint(token: str) -> str:
    return hashlib.sha256(("token:" + token).encode()).hexdigest()[:16]


def _hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_next(value: Optional[str]) -> str:
    """Only a path on this site: never somewhere else to be sent after signing in."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def _page(title: str, text: str, status: int) -> HTMLResponse:
    from html import escape
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)}</title><body style='font:16px system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 16px'>"
        f"<h1 style='font-size:20px'>{escape(title)}</h1><p>{escape(text)}</p><p><a href='/'>回到首頁</a></p></body>",
        status_code=status, headers=NO_STORE)


def register(app, db, settings: Optional[Dict[str, str]] = None,
             exchange: Optional[Callable[[Dict[str, str], str, str], Dict[str, Any]]] = None,
             judge: Optional[Callable[[str], Optional[str]]] = None,
             person: Optional[Callable[[str], Optional[str]]] = None):
    """Adds the sign-in routes. judge(token) says what a pasted token is: "all", "personal",
    "voice" or None; person(token), for a sync token, the person it belongs to (their first token). Returns (account_of, link_of): for a request, the signed-in Google account,
    and what it is bound to ({"kind", "token"}); None for either when there is none."""
    s = settings if settings is not None else settings_from_env()
    trade = exchange or exchange_code
    secure = s.get("public_url", "").startswith("https://")
    with db() as conn:
        conn.executescript(SCHEMA)
        # which of the maker's characters a sharing token shows: a JSON list, or NULL for all of
        # them, those added later included (what every token made before this column means)
        if "characters" not in {r[1] for r in conn.execute("PRAGMA table_info(view_tokens)")}:
            conn.execute("ALTER TABLE view_tokens ADD COLUMN characters TEXT")
        # 'voice': an account's token for voice assistants (the /voice page and the API), which
        # shows every part; NULL: a sharing token (every token made before this column)
        if "kind" not in {r[1] for r in conn.execute("PRAGMA table_info(view_tokens)")}:
            conn.execute("ALTER TABLE view_tokens ADD COLUMN kind TEXT")
        parts.split_items(conn, "view_tokens")       # trades and the bank became parts of their own

    def account_of(request: Request) -> Optional[Dict[str, Any]]:
        sid = request.cookies.get(SESSION_COOKIE)
        if not sid:
            return None
        with db() as conn:
            row = conn.execute("SELECT a.sub, a.email, a.name, a.picture FROM web_sessions w JOIN google_accounts a ON a.sub=w.sub "
                               "WHERE w.id_hash=? AND w.expires>?", (_hash(sid), time.time())).fetchone()
        return dict(row) if row else None

    def link_of(request: Request) -> Optional[Dict[str, Any]]:
        """What the signed-in account is bound to. A personal token that has since been deleted
        binds to nothing: the account is asked to bind again."""
        sid = request.cookies.get(SESSION_COOKIE)
        if not sid:
            return None
        with db() as conn:
            row = conn.execute(
                "SELECT l.kind, l.token FROM web_sessions w JOIN google_links l ON l.sub=w.sub "
                "LEFT JOIN upload_tokens t ON t.token=l.token "
                "WHERE w.id_hash=? AND w.expires>? AND (l.kind='all' OR t.token IS NOT NULL)", (_hash(sid), time.time())).fetchone()
        return dict(row) if row else None

    def described(request: Request) -> Dict[str, Any]:
        acct = account_of(request)
        link = link_of(request) if acct else None
        if link:
            names = []
            if link["kind"] == "personal":
                with db() as conn:
                    names = [r["full_name"] or r["name"] or r["character_id"] for r in conn.execute(
                        "SELECT o.character_id, c.name, c.full_name FROM token_characters o LEFT JOIN characters c ON c.id=o.character_id "
                        "WHERE o.token=? ORDER BY o.claimed_at", (link["token"],))]
            link = {"kind": link["kind"], "characters": names}
        return {"google": enabled(s), "account": {k: acct[k] for k in ("email", "name", "picture")} if acct else None,
                "link": link}

    @app.get("/api/v1/me", include_in_schema=False)
    def me(request: Request):
        """For the pages: whether Google sign-in is on, who is signed in, and what that account is
        bound to (`link`: kind "all" or "personal", with the characters). Needs no token."""
        return JSONResponse(described(request), headers=NO_STORE)

    @app.post("/api/v1/me/link", include_in_schema=False)
    def link(body: LinkIn, request: Request):
        """Bind the signed-in account to the token pasted here (the one in the sync program)."""
        acct = account_of(request)
        if not acct:
            raise HTTPException(401, "sign in with Google first")
        token = body.token.strip()
        kind = judge(token) if judge else None
        if kind == "voice":
            raise HTTPException(400, "this is a character's code for the voice page, not an upload token")
        if kind not in ("all", "personal"):
            raise HTTPException(403, "invalid token")
        with db() as conn:
            # the server's own tokens are not stored: "all" stays right when they are changed
            conn.execute("INSERT INTO google_links VALUES (?,?,?,?) ON CONFLICT(sub) DO UPDATE SET "
                         "kind=excluded.kind, token=excluded.token, linked_at=excluded.linked_at",
                         (acct["sub"], kind, ((person(token) if person else None) or token) if kind == "personal" else None,
                          _now_iso()))
        return JSONResponse(described(request), headers=NO_STORE)

    # ---- the signed-in person's own tokens: only once the account is bound to one
    def bound(request: Request) -> Dict[str, Any]:
        """The signed-in account's binding, refusing otherwise; and, for a change, a request
        that came from a page of this site (a form elsewhere cannot use the sign-in cookie)."""
        if request.method != "GET":
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower():
                raise HTTPException(403, "not from this site")
        if not account_of(request):
            raise HTTPException(401, "sign in with Google first")
        link = link_of(request)
        if not link:                    # a Google account alone makes no tokens
            raise HTTPException(403, "bind this Google account to your token first")
        return link

    def owner_key(link: Dict[str, Any]) -> str:
        return link["token"] if link["kind"] == "personal" else "*"

    def tokens_of(link: Dict[str, Any]) -> Dict[str, Any]:
        """Everything the panel shows. Each token comes with an id (a fingerprint of it) that the
        changes below use, so a token itself is never put in an address, nor in a server's log."""
        with db() as conn:
            sync, names = [], []
            if link["kind"] == "personal":
                me = link["token"]
                row = conn.execute("SELECT label, created_at, last_used_at FROM upload_tokens WHERE token=?", (me,)).fetchone()
                if row:
                    sync.append({"id": fingerprint(me), "token": me, "label": row["label"], "first": True,
                                 "created_at": row["created_at"], "last_used_at": row["last_used_at"]})
                sync += [{"id": fingerprint(r["token"]), "token": r["token"], "label": r["label"], "first": False,
                          "created_at": r["created_at"], "last_used_at": r["last_used_at"]} for r in conn.execute(
                    "SELECT token, label, created_at, last_used_at FROM sync_aliases WHERE owner=? ORDER BY created_at", (me,))]
                names = [r["full_name"] or r["name"] or r["character_id"] for r in conn.execute(
                    "SELECT o.character_id, c.name, c.full_name FROM token_characters o LEFT JOIN characters c ON c.id=o.character_id "
                    "WHERE o.token=? ORDER BY o.claimed_at", (me,))]
            owned = owned_by(conn, link)
            view, voice = [], []
            for r in conn.execute("SELECT token, label, sections, characters, kind, created_at, last_used_at FROM view_tokens "
                                  "WHERE owner=? ORDER BY created_at", (owner_key(link),)):
                shown = parts.parse(r["sections"])
                (voice if r["kind"] == "voice" else view).append({
                    "id": fingerprint(r["token"]), "token": r["token"], "label": r["label"],
                    "created_at": r["created_at"], "last_used_at": r["last_used_at"],
                    "sections": [k for k, _ in parts.PARTS if shown is None or k in shown],
                    "characters": json.loads(r["characters"]) if r["characters"] else None})
        return {"kind": link["kind"], "sync": sync if link["kind"] == "personal" else None, "characters": names,
                "owned": owned, "view": view, "voice": voice, "max_voice": MAX_VOICE_TOKENS,
                "max_sync": MAX_SYNC_TOKENS, "max_view": MAX_VIEW_TOKENS,
                "parts": [{"key": k, "name": n} for k, n in parts.PARTS]}

    def owned_by(conn, link: Dict[str, Any]) -> List[Dict[str, Any]]:
        """The characters a sharing token of this account can show: the person's, or every one
        for an account bound to the server's own token. Newest played first."""
        if link["kind"] == "personal":
            rows = conn.execute("SELECT o.character_id id, c.name, c.full_name, c.class, c.level FROM token_characters o "
                                "LEFT JOIN characters c ON c.id=o.character_id WHERE o.token=? "
                                "ORDER BY c.updated_at DESC", (link["token"],))
        else:
            rows = conn.execute("SELECT id, name, full_name, class, level FROM characters ORDER BY updated_at DESC")
        return [dict(r) for r in rows]

    def picked(conn, link: Dict[str, Any], ids: Optional[List[str]]) -> Optional[str]:
        """What is stored for the characters a sharing token shows: NULL for all of them, else a
        non-empty list of this account's own characters."""
        if ids is None:
            return None
        mine = [c["id"] for c in owned_by(conn, link)]
        wanted = [c for c in mine if c in set(ids)]              # in the account's order, each once
        if not wanted or len(set(ids)) != len(wanted):
            raise HTTPException(400, "choose at least one of your own characters")
        return json.dumps(wanted)

    def answer(link: Dict[str, Any], made: Optional[str] = None) -> JSONResponse:
        out = tokens_of(link)
        if made:
            out["made"] = made
        return JSONResponse(out, headers=NO_STORE)

    def pick(rows: List[Dict[str, Any]], which: str) -> Dict[str, Any]:
        for r in rows:
            if r["id"] == which:
                return r
        raise HTTPException(404, "no such token")

    @app.get("/api/v1/me/tokens", include_in_schema=False)
    def my_tokens(request: Request):
        """The tokens of the person signed in: the ones their sync programs upload with (up to
        three), and the read-only ones they made to share. Only for an account bound to a token."""
        return answer(bound(request))

    # sync tokens: the first stands for the person; up to two more upload and read as that person
    def personal(request: Request) -> Dict[str, Any]:
        link = bound(request)
        if link["kind"] != "personal":
            raise HTTPException(409, "this account is bound to the server's own token, which is not changed here")
        return link

    @app.post("/api/v1/me/sync-tokens", include_in_schema=False)
    def new_sync_token(body: LabelIn, request: Request):
        """Another sync token for the same person: for a second computer, say."""
        link = personal(request)
        with db() as conn:
            if 1 + conn.execute("SELECT COUNT(*) FROM sync_aliases WHERE owner=?", (link["token"],)).fetchone()[0] >= MAX_SYNC_TOKENS:
                raise HTTPException(409, f"at most {MAX_SYNC_TOKENS} sync tokens")
            token = new_sync_secret()
            conn.execute("INSERT INTO sync_aliases (token, owner, label, created_at) VALUES (?,?,?,?)",
                         (token, link["token"], body.label.strip() or None, _now_iso()))
        return answer(link, token)

    @app.post("/api/v1/me/sync-tokens/regenerate", include_in_schema=False)
    def regenerate_sync_token(body: PickIn, request: Request):
        """A new token in place of one of the person's sync tokens. The old one stops at once:
        a sync program that used it has to be given the new one."""
        link = personal(request)
        old = pick(tokens_of(link)["sync"], body.id)
        new = new_sync_secret()
        with db() as conn:
            if old["first"]:
                # The first one stands for the person: everything kept under it moves to the new one,
                # in one go, so the characters, the binding, the shared tokens and the settings stay.
                for table, column in (("upload_tokens", "token"), ("token_characters", "token"), ("google_links", "token"),
                                      ("view_tokens", "owner"), ("sync_aliases", "owner"), ("sync_settings", "token")):
                    conn.execute(f"UPDATE {table} SET {column}=? WHERE {column}=?", (new, old["token"]))
                link = {**link, "token": new}
            else:
                conn.execute("UPDATE sync_aliases SET token=?, last_used_at=NULL WHERE token=?", (new, old["token"]))
        return answer(link, new)

    @app.post("/api/v1/me/sync-tokens/delete", include_in_schema=False)
    def delete_sync_token(body: PickIn, request: Request):
        """Stops one of the person's further sync tokens. The first cannot be deleted, only replaced."""
        link = personal(request)
        old = pick(tokens_of(link)["sync"], body.id)
        if old["first"]:
            raise HTTPException(409, "the first sync token cannot be deleted, only regenerated")
        with db() as conn:
            conn.execute("DELETE FROM sync_aliases WHERE token=?", (old["token"],))
        return answer(link)

    # read-only tokens to share
    def chosen(names: Optional[List[str]]) -> Optional[str]:
        if names is None:
            return None
        if not set(names) <= parts.KEYS:
            raise HTTPException(400, "unknown part: " + ", ".join(sorted(set(names) - parts.KEYS)))
        return parts.dump(names)

    @app.post("/api/v1/me/tokens", include_in_schema=False)
    def new_view_token(body: NewViewIn, request: Request):
        """A new read-only token: it sees what its maker's token sees (or only the parts chosen),
        never chat, and uploads nothing."""
        link = bound(request)
        with db() as conn:
            if conn.execute("SELECT COUNT(*) FROM view_tokens WHERE owner=? AND kind IS NULL",
                            (owner_key(link),)).fetchone()[0] >= MAX_VIEW_TOKENS:
                raise HTTPException(409, f"at most {MAX_VIEW_TOKENS} sharing tokens")
            token = new_view_secret()
            conn.execute("INSERT INTO view_tokens (token, owner, label, sections, characters, created_at) VALUES (?,?,?,?,?,?)",
                         (token, owner_key(link), body.label.strip() or None,
                          chosen(parts.SHARE_DEFAULT if body.sections is None else body.sections),
                          picked(conn, link, body.characters), _now_iso()))
        return answer(link, token)

    @app.post("/api/v1/me/voice-tokens", include_in_schema=False)
    def new_voice_token(body: NewViewIn, request: Request):
        """A token for a voice assistant: /voice?t=<it>&c=<character> and the API both read with
        it. It sees every part of the characters chosen (null: all of them, later ones too), never
        chat, and uploads nothing. Changed and stopped with the same calls as a sharing token."""
        link = bound(request)
        with db() as conn:
            if conn.execute("SELECT COUNT(*) FROM view_tokens WHERE owner=? AND kind='voice'",
                            (owner_key(link),)).fetchone()[0] >= MAX_VOICE_TOKENS:
                raise HTTPException(409, f"at most {MAX_VOICE_TOKENS} voice tokens")
            token = new_voice_secret()
            conn.execute("INSERT INTO view_tokens (token, owner, label, sections, characters, kind, created_at) "
                         "VALUES (?,?,?,NULL,?,'voice',?)",
                         (token, owner_key(link), body.label.strip() or None, picked(conn, link, body.characters), _now_iso()))
        return answer(link, token)

    def either(link: Dict[str, Any], which: str) -> Dict[str, Any]:
        """One of the account's read-only tokens: a sharing one or a voice one."""
        held = tokens_of(link)
        return pick(held["view"] + held["voice"], which)

    @app.post("/api/v1/me/tokens/characters", include_in_schema=False)
    def set_view_characters(body: ShownIn, request: Request):
        """Which of the maker's characters a sharing (or voice) token shows: some of them, or
        (null) all of them, those added later too. Takes effect at its next request."""
        link = bound(request)
        old = either(link, body.id)
        with db() as conn:
            conn.execute("UPDATE view_tokens SET characters=? WHERE token=?", (picked(conn, link, body.characters), old["token"]))
        return answer(link)

    @app.post("/api/v1/me/tokens/sections", include_in_schema=False)
    def set_view_sections(body: SectionsIn, request: Request):
        """Which parts a sharing token shows (see sections.py). Takes effect at its next request."""
        link = bound(request)
        old = pick(tokens_of(link)["view"], body.id)
        with db() as conn:
            conn.execute("UPDATE view_tokens SET sections=? WHERE token=?", (chosen(body.sections), old["token"]))
        return answer(link)

    @app.post("/api/v1/me/tokens/regenerate", include_in_schema=False)
    def regenerate_view_token(body: PickIn, request: Request):
        """A new address for a sharing (or voice) token: the old one stops working, the name stays."""
        link = bound(request)
        old = either(link, body.id)
        new = new_voice_secret() if old in tokens_of(link)["voice"] else new_view_secret()
        with db() as conn:
            conn.execute("UPDATE view_tokens SET token=?, last_used_at=NULL WHERE token=?", (new, old["token"]))
        return answer(link, new)

    @app.post("/api/v1/me/tokens/delete", include_in_schema=False)
    def delete_view_token(body: PickIn, request: Request):
        """Stops one of the person's read-only tokens (sharing or voice) at once."""
        link = bound(request)
        old = either(link, body.id)
        with db() as conn:
            conn.execute("DELETE FROM view_tokens WHERE token=?", (old["token"],))
        return answer(link)

    @app.post("/api/v1/me/unlink", include_in_schema=False)
    def unlink(request: Request):
        acct = account_of(request)
        if not acct:
            raise HTTPException(401, "sign in with Google first")
        with db() as conn:
            conn.execute("DELETE FROM google_links WHERE sub=?", (acct["sub"],))
        return JSONResponse(described(request), headers=NO_STORE)

    @app.get("/auth/google/start", include_in_schema=False)
    def start(request: Request, next: Optional[str] = None):
        if not enabled(s):
            raise HTTPException(404, "Google sign-in is not set up on this server")
        target = _safe_next(next)
        # Google sends the browser back to the public address only; the state cookie has to be
        # set there too, so an attempt begun anywhere else (the LAN address) moves over first.
        public = urlsplit(s["public_url"])
        if request.headers.get("host", "").lower() != public.netloc.lower():
            return RedirectResponse(s["public_url"] + "/auth/google/start?" + urlencode({"next": target}), 302, headers=NO_STORE)
        state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        with db() as conn:
            conn.execute("DELETE FROM google_pending WHERE created<?", (time.time() - PENDING_SECONDS,))
            conn.execute("INSERT INTO google_pending VALUES (?,?,?,?,?)", (state, nonce, verifier, target, time.time()))
        url = AUTHORIZE_URL + "?" + urlencode({
            "client_id": s["client_id"], "redirect_uri": s["public_url"] + CALLBACK, "response_type": "code",
            "scope": "openid email profile", "state": state, "nonce": nonce,
            "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "select_account"})
        r = RedirectResponse(url, 302, headers=NO_STORE)
        r.set_cookie(STATE_COOKIE, state, max_age=PENDING_SECONDS, path="/auth/google", httponly=True, secure=secure, samesite="lax")
        return r

    @app.get(CALLBACK, include_in_schema=False)
    def callback(request: Request, state: str = "", code: str = "", error: str = ""):
        if not enabled(s):
            raise HTTPException(404, "Google sign-in is not set up on this server")
        with db() as conn:              # each attempt can be answered once
            row = conn.execute("SELECT * FROM google_pending WHERE state=?", (state,)).fetchone() if state else None
            if row:
                conn.execute("DELETE FROM google_pending WHERE state=?", (state,))
        if error:
            return _page("沒有登入", "Google 登入已取消或沒有完成。", 400)
        if not row or row["created"] < time.time() - PENDING_SECONDS:
            return _page("登入逾時", "這次登入已經過期或已經用過，請回首頁重新按一次「用 Google 登入」。", 400)
        if request.cookies.get(STATE_COOKIE) != state:
            return _page("無法登入", "登入要在同一個瀏覽器裡完成，請回首頁重新按一次「用 Google 登入」。", 400)
        try:
            answer = trade(s, code, row["verifier"])
            c = claims_of(answer.get("id_token", ""))
        except ValueError:
            return _page("無法登入", "沒辦法向 Google 確認這次登入，請稍後再試。", 502)
        except Exception:               # Google unreachable from the server
            return _page("無法登入", "伺服器連不上 Google，請稍後再試。", 502)
        aud = c.get("aud")
        if (c.get("iss") not in ISSUERS or (aud != s["client_id"] and not (isinstance(aud, list) and s["client_id"] in aud))
                or not isinstance(c.get("exp"), (int, float)) or c["exp"] < time.time() - 60
                or c.get("nonce") != row["nonce"] or not c.get("sub")):
            return _page("無法登入", "Google 回傳的登入資料不正確，請重新登入。", 400)
        if not c.get("email") or c.get("email_verified") is not True:
            return _page("無法登入", "這個 Google 帳號的 email 還沒有驗證。", 403)
        sid, now = secrets.token_urlsafe(32), _now_iso()
        with db() as conn:
            conn.execute("INSERT INTO google_accounts VALUES (?,?,?,?,?,?) ON CONFLICT(sub) DO UPDATE SET "
                         "email=excluded.email, name=excluded.name, picture=excluded.picture, last_seen=excluded.last_seen",
                         (c["sub"], c["email"], c.get("name"), c.get("picture"), now, now))
            conn.execute("DELETE FROM web_sessions WHERE expires<?", (time.time(),))
            conn.execute("INSERT INTO web_sessions VALUES (?,?,?,?)", (_hash(sid), c["sub"], now, time.time() + SESSION_DAYS * 86400))
        r = RedirectResponse(row["next"], 302, headers=NO_STORE)
        r.set_cookie(SESSION_COOKIE, sid, max_age=SESSION_DAYS * 86400, path="/", httponly=True, secure=secure, samesite="lax")
        r.delete_cookie(STATE_COOKIE, path="/auth/google")
        return r

    @app.post("/auth/logout", include_in_schema=False)
    def logout(request: Request):
        sid = request.cookies.get(SESSION_COOKIE)
        if sid:
            with db() as conn:
                conn.execute("DELETE FROM web_sessions WHERE id_hash=?", (_hash(sid),))
        r = JSONResponse({"success": True}, headers=NO_STORE)
        r.delete_cookie(SESSION_COOKIE, path="/", secure=secure, httponly=True, samesite="lax")
        return r

    return account_of, link_of
