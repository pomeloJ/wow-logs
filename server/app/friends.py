"""Friends: two Google accounts that agreed to see each other's characters.

One account makes an invite link and passes it on (in a chat, say); whoever opens it while
signed in with Google, and accepts, becomes that account's friend. A link works once and for
seven days, and says nothing about who made it until someone signed in opens it: no account
can be looked up by its email.

Each side chooses what the other sees, separately: which parts (sections.py) and which
characters. A new friend sees every part but screenshots, and every character of the person's
token. An account bound to the server's own token (which sees everyone's characters) shares
none until it ticks the ones to share, so nobody else's characters are ever handed on.

A friend reads through the same checks as a sharing token: the page sends `X-Friend: <id>`
with the sign-in cookie, and `view(request, id)` says which characters and parts that is.
Chat is never shown, and nothing is ever uploaded this way.
"""
import hashlib
import json
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import sections as parts

INVITE_DAYS = 7
MAX_INVITES = 5                # links not used yet, per account
MAX_FRIENDS = 100
DEFAULT = [k for k, _ in parts.PARTS if k != "screenshots"]
NO_STORE = {"Cache-Control": "no-store"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS friend_invites (
    code TEXT PRIMARY KEY, sub TEXT NOT NULL, created_at TEXT NOT NULL, expires REAL NOT NULL
);
-- a < b, so each pair is one row
CREATE TABLE IF NOT EXISTS friendships (
    a TEXT NOT NULL, b TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (a, b)
);
-- what `sub` shows `friend`: sections as sections.dump (NULL: every part); characters a JSON
-- list of character ids, or NULL for every character of the person's token
CREATE TABLE IF NOT EXISTS friend_shares (
    sub TEXT NOT NULL, friend TEXT NOT NULL, sections TEXT, characters TEXT, PRIMARY KEY (sub, friend)
);
"""


class CodeIn(BaseModel):
    code: str = Field(min_length=4, max_length=64)


class PickIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)


class ShareIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    sections: List[str] = Field(max_length=20)
    characters: Optional[List[str]] = Field(None, max_length=200)     # None: every character of the token


def friend_id(sub: str) -> str:
    """How a friend is named to the page: never the Google account's own id."""
    return hashlib.sha256(("friend:" + sub).encode()).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def register(app, db, account_of: Callable[[Request], Optional[Dict[str, Any]]]) -> Callable[[Request, str], Optional[Tuple[List[str], Optional[frozenset]]]]:
    """Adds the friend routes; returns view(request, friend id) -> (characters, parts) that
    friend shows the signed-in account, or None when they are not friends."""
    with db() as conn:
        conn.executescript(SCHEMA)

    def link_of_sub(conn, sub: str) -> Optional[Dict[str, Any]]:
        """What an account is bound to, as auth.link_of has it; None when unbound."""
        row = conn.execute("SELECT l.kind, l.token FROM google_links l LEFT JOIN upload_tokens t ON t.token=l.token "
                           "WHERE l.sub=? AND (l.kind='all' OR t.token IS NOT NULL)", (sub,)).fetchone()
        return dict(row) if row else None

    def own_characters(conn, sub: str) -> Tuple[Optional[str], List[str]]:
        """(kind, the characters the account could share): its token's, or everyone's for 'all'."""
        link = link_of_sub(conn, sub)
        if not link:
            return None, []
        if link["kind"] == "personal":
            return "personal", [r["character_id"] for r in conn.execute(
                "SELECT character_id FROM token_characters WHERE token=? ORDER BY claimed_at, character_id", (link["token"],))]
        return "all", [r["id"] for r in conn.execute("SELECT id FROM characters ORDER BY updated_at DESC")]

    def shared(conn, sub: str, friend: str) -> Tuple[List[str], Optional[frozenset], Optional[List[str]]]:
        """What `sub` shows `friend`: (characters, parts, the list as chosen or None for all)."""
        row = conn.execute("SELECT sections, characters FROM friend_shares WHERE sub=? AND friend=?", (sub, friend)).fetchone()
        if not row:
            return [], frozenset(), []
        kind, mine = own_characters(conn, sub)
        picked = json.loads(row["characters"]) if row["characters"] is not None else None
        if picked is None:
            chars = mine if kind == "personal" else []         # the server's own token: only what was ticked
        else:
            chars = [c for c in mine if c in set(picked)]
        return chars, parts.parse(row["sections"]), picked

    def friends_of(conn, sub: str) -> List[str]:
        return [r["other"] for r in conn.execute(
            "SELECT b AS other, created_at FROM friendships WHERE a=? UNION SELECT a, created_at FROM friendships WHERE b=? "
            "ORDER BY created_at", (sub, sub))]

    def find(conn, sub: str, fid: str) -> str:
        for other in friends_of(conn, sub):
            if friend_id(other) == fid:
                return other
        raise HTTPException(404, "no such friend")

    def signed(request: Request) -> Dict[str, Any]:
        """The signed-in account; for a change, only from a page of this site."""
        if request.method != "GET":
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower():
                raise HTTPException(403, "not from this site")
        acct = account_of(request)
        if not acct:
            raise HTTPException(401, "sign in with Google first")
        with db() as conn:
            if not link_of_sub(conn, acct["sub"]):
                raise HTTPException(403, "bind this Google account to your token first")
        return acct

    def names(conn, ids: List[str]) -> List[Dict[str, Any]]:
        if not ids:
            return []
        rows = {r["id"]: r for r in conn.execute(
            f"SELECT id, name, class, level, realm FROM characters WHERE id IN ({','.join('?' * len(ids))})", ids)}
        return [{"id": c, "name": rows[c]["name"] if c in rows else c, "class": rows[c]["class"] if c in rows else None,
                 "level": rows[c]["level"] if c in rows else None} for c in ids]

    def listing(sub: str, made: Optional[str] = None) -> JSONResponse:
        keys = [k for k, _ in parts.PARTS]
        with db() as conn:
            conn.execute("DELETE FROM friend_invites WHERE expires<?", (time.time(),))
            kind, mine = own_characters(conn, sub)
            friends = []
            for other in friends_of(conn, sub):
                acct = conn.execute("SELECT email, name, picture FROM google_accounts WHERE sub=?", (other,)).fetchone()
                since = conn.execute("SELECT created_at FROM friendships WHERE a=? AND b=?", tuple(sorted((sub, other)))).fetchone()
                theirs, their_parts, _ = shared(conn, other, sub)
                ours, our_parts, picked = shared(conn, sub, other)
                friends.append({
                    "id": friend_id(other), "name": (acct["name"] if acct else None) or (acct["email"] if acct else "?"),
                    "email": acct["email"] if acct else None, "picture": acct["picture"] if acct else None,
                    "since": since["created_at"] if since else None,
                    "shown": {"characters": names(conn, theirs), "sections": [k for k in keys if their_parts is None or k in their_parts]},
                    "mine": {"characters": ours, "all": picked is None and kind == "personal",
                             "sections": [k for k in keys if our_parts is None or k in our_parts]}})
            invites = [{"code": r["code"], "created_at": r["created_at"],
                        "expires_at": datetime.fromtimestamp(r["expires"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
                       for r in conn.execute("SELECT * FROM friend_invites WHERE sub=? ORDER BY created_at", (sub,))]
            out = {"kind": kind, "friends": friends, "invites": invites, "my_characters": names(conn, mine),
                   "parts": [{"key": k, "name": n} for k, n in parts.PARTS], "max_invites": MAX_INVITES}
        if made:
            out["made"] = made
        return JSONResponse(out, headers=NO_STORE)

    @app.get("/api/v1/me/friends", include_in_schema=False)
    def my_friends(request: Request):
        """The signed-in account's friends: what each shows it, and what it shows each; its open invites."""
        return listing(signed(request)["sub"])

    @app.post("/api/v1/me/friends/invite", include_in_schema=False)
    def invite(request: Request):
        """A link that makes whoever accepts it a friend: once, within seven days."""
        sub = signed(request)["sub"]
        with db() as conn:
            conn.execute("DELETE FROM friend_invites WHERE expires<?", (time.time(),))
            if conn.execute("SELECT COUNT(*) FROM friend_invites WHERE sub=?", (sub,)).fetchone()[0] >= MAX_INVITES:
                raise HTTPException(409, f"at most {MAX_INVITES} open invites")
            code = secrets.token_urlsafe(12)
            conn.execute("INSERT INTO friend_invites VALUES (?,?,?,?)", (code, sub, _now_iso(), time.time() + INVITE_DAYS * 86400))
        return listing(sub, code)

    @app.post("/api/v1/me/friends/invite/delete", include_in_schema=False)
    def drop_invite(body: CodeIn, request: Request):
        sub = signed(request)["sub"]
        with db() as conn:
            conn.execute("DELETE FROM friend_invites WHERE code=? AND sub=?", (body.code, sub))
        return listing(sub)

    def open_invite(conn, code: str) -> Optional[Dict[str, Any]]:
        row = conn.execute("SELECT * FROM friend_invites WHERE code=? AND expires>?", (code, time.time())).fetchone()
        return dict(row) if row else None

    @app.get("/api/v1/me/friends/invite/{code}", include_in_schema=False)
    def look_at_invite(code: str, request: Request):
        """Who an invite is from, for the page to ask before accepting."""
        sub = signed(request)["sub"]
        with db() as conn:
            row = open_invite(conn, code)
            if not row:
                raise HTTPException(404, "this invite has been used or has expired")
            acct = conn.execute("SELECT email, name, picture FROM google_accounts WHERE sub=?", (row["sub"],)).fetchone()
            already = row["sub"] in friends_of(conn, sub)
        return JSONResponse({"from": {"name": (acct["name"] or acct["email"]) if acct else "?", "picture": acct["picture"] if acct else None},
                             "self": row["sub"] == sub, "already": already}, headers=NO_STORE)

    @app.post("/api/v1/me/friends/accept", include_in_schema=False)
    def accept(body: CodeIn, request: Request):
        """Become friends with the invite's maker. Each side starts out showing the other every
        part but screenshots; the link is used up."""
        sub = signed(request)["sub"]
        with db() as conn:
            row = open_invite(conn, body.code)
            if not row:
                raise HTTPException(404, "this invite has been used or has expired")
            other = row["sub"]
            if other == sub:
                raise HTTPException(409, "this is your own invite")
            if len(friends_of(conn, sub)) >= MAX_FRIENDS or len(friends_of(conn, other)) >= MAX_FRIENDS:
                raise HTTPException(409, f"at most {MAX_FRIENDS} friends")
            conn.execute("DELETE FROM friend_invites WHERE code=?", (body.code,))
            a, b = sorted((sub, other))
            conn.execute("INSERT OR IGNORE INTO friendships VALUES (?,?,?)", (a, b, _now_iso()))
            for x, y in ((sub, other), (other, sub)):
                conn.execute("INSERT OR IGNORE INTO friend_shares VALUES (?,?,?,NULL)", (x, y, parts.dump(DEFAULT)))
        out = listing(sub)
        return out

    @app.post("/api/v1/me/friends/share", include_in_schema=False)
    def share(body: ShareIn, request: Request):
        """What the signed-in account shows one friend: the parts, and which characters."""
        sub = signed(request)["sub"]
        if not set(body.sections) <= parts.KEYS:
            raise HTTPException(400, "unknown part")
        with db() as conn:
            other = find(conn, sub, body.id)
            _, mine = own_characters(conn, sub)
            picked = None if body.characters is None else [c for c in body.characters if c in set(mine)]
            conn.execute("INSERT INTO friend_shares VALUES (?,?,?,?) ON CONFLICT(sub, friend) DO UPDATE SET "
                         "sections=excluded.sections, characters=excluded.characters",
                         (sub, other, parts.dump(body.sections), None if picked is None else json.dumps(picked)))
        return listing(sub)

    @app.post("/api/v1/me/friends/remove", include_in_schema=False)
    def remove(body: PickIn, request: Request):
        """No longer friends: neither side sees the other's characters from now on."""
        sub = signed(request)["sub"]
        with db() as conn:
            other = find(conn, sub, body.id)
            conn.execute("DELETE FROM friendships WHERE a=? AND b=?", tuple(sorted((sub, other))))
            conn.execute("DELETE FROM friend_shares WHERE (sub=? AND friend=?) OR (sub=? AND friend=?)", (sub, other, other, sub))
        return listing(sub)

    def view(request: Request, fid: str) -> Optional[Tuple[List[str], Optional[frozenset]]]:
        acct = account_of(request)
        if not acct:
            return None
        with db() as conn:
            try:
                other = find(conn, acct["sub"], fid)
            except HTTPException:
                return None
            chars, shown, _ = shared(conn, other, acct["sub"])
        return chars, shown

    return view
