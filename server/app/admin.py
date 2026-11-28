"""The owner's page: every character and every personal upload token, and a button to make more.

It asks for no password and shows tokens in the clear, so it answers only to computers on
the same network as the server. A request that came through a proxy is judged by the
addresses the proxy reports: the proxy itself sits on the local network, the visitor may not.
"""
import ipaddress
import os
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

HERE = os.path.dirname(os.path.abspath(__file__))
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"          # no look-alikes (0/o, 1/l/i)
TOKEN_LEN = 32                                        # longer than the first ones (16), which keep working
LOCAL = "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,169.254.0.0/16,fc00::/7,fe80::/10"
FORWARDED = ("x-forwarded-for", "x-real-ip", "cf-connecting-ip", "true-client-ip", "forwarded")


def networks() -> List[Any]:
    return [ipaddress.ip_network(n.strip()) for n in (os.environ.get("AH_ADMIN_NETS") or LOCAL).split(",") if n.strip()]


def is_local(peer: Optional[str], headers) -> bool:
    """True only when the computer that connected, and every address a proxy says the request
    passed through, is on an allowed network. Anything that cannot be read counts as outside."""
    seen = [peer or ""]
    for name in FORWARDED:
        value = headers.get(name)
        if value is None:
            continue
        parts = re.findall(r'for=\s*"?\[?([^\]";,\s]+)', value, re.I) if name == "forwarded" else value.split(",")
        if not parts:
            return False
        seen += parts
    nets = networks()
    for text in seen:
        text = text.strip().strip("[]")
        if text.count(":") == 1 and "." in text:        # 203.0.113.9:4711
            text = text.split(":")[0]
        try:
            ip = ipaddress.ip_address(text)
        except ValueError:
            return False
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if not any(ip.version == n.version and ip in n for n in nets):
            return False
    return True


def local_only(request: Request) -> None:
    if not is_local(request.client.host if request.client else None, request.headers):
        raise HTTPException(404, "Not Found")           # from outside it looks as if nothing is here


class NewToken(BaseModel):
    label: Optional[str] = Field(None, max_length=60)
    character_id: Optional[str] = Field(None, max_length=200)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def register(app, db) -> None:
    only = [Depends(local_only)]

    @app.get("/admin", include_in_schema=False, dependencies=only)
    def admin_page():
        return FileResponse(os.path.join(HERE, "admin.html"), media_type="text/html", headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/admin/overview", include_in_schema=False, dependencies=only)
    def overview() -> Dict[str, Any]:
        with db() as conn:
            chars = [dict(r) for r in conn.execute(
                "SELECT c.id, c.name, c.class, c.level, c.updated_at, o.token, t.label token_label, "
                "(SELECT COUNT(*) FROM events e WHERE e.character_id=c.id) events FROM characters c "
                "LEFT JOIN token_characters o ON o.character_id=c.id LEFT JOIN upload_tokens t ON t.token=o.token "
                "ORDER BY c.updated_at DESC")]
            tokens = [dict(r) for r in conn.execute(
                "SELECT token, label, created_at, last_used_at, (SELECT COUNT(*) FROM view_tokens v WHERE v.owner=upload_tokens.token) "
                "shared, (SELECT COUNT(*) FROM sync_aliases a WHERE a.owner=upload_tokens.token) more_sync "
                "FROM upload_tokens ORDER BY created_at DESC")]
            for t in tokens:
                t["characters"] = [{"id": r["character_id"], "name": r["name"]} for r in conn.execute(
                    "SELECT o.character_id, c.name FROM token_characters o LEFT JOIN characters c ON c.id=o.character_id "
                    "WHERE o.token=? ORDER BY o.claimed_at", (t["token"],))]
            # Google accounts that signed in to the pages, and what each is bound to
            google = [dict(r) for r in conn.execute(
                "SELECT a.sub, a.email, a.name, a.last_seen, l.kind, l.token, t.label token_label FROM google_accounts a "
                "LEFT JOIN google_links l ON l.sub=a.sub LEFT JOIN upload_tokens t ON t.token=l.token "
                "ORDER BY a.last_seen DESC")]
            for g in google:                    # a deleted personal token binds to nothing
                if g["kind"] == "personal" and g["token_label"] is None and not conn.execute(
                        "SELECT 1 FROM upload_tokens WHERE token=?", (g["token"],)).fetchone():
                    g["kind"] = g["token"] = None
            people = person_details(conn, tokens, google)
        return {"characters": chars, "tokens": tokens, "google": google, "people": people}

    def person_details(conn, tokens: List[Dict[str, Any]], google: List[Dict[str, Any]]) -> Dict[str, Any]:
        """What each person has set up, for the page to show one card per person: keyed by their
        upload token, and '*' for accounts bound to every character. Sharing and voice tokens are
        named, never shown; friends and invites are counted by the Google accounts bound."""
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        pictures = {r["sub"]: r["picture"] for r in conn.execute("SELECT sub, picture FROM google_accounts")}
        out: Dict[str, Any] = {}
        for owner in [t["token"] for t in tokens] + ["*"]:
            subs = [g["sub"] for g in google if (g["kind"] == "personal" and g["token"] == owner) or (owner == "*" and g["kind"] == "all")]
            shares, voice = [], []
            if "view_tokens" in tables:
                for r in conn.execute("SELECT label, sections, kind, created_at, last_used_at FROM view_tokens WHERE owner=? "
                                      "ORDER BY created_at", (owner,)):
                    (voice if r["kind"] == "voice" else shares).append(
                        {"label": r["label"], "created_at": r["created_at"], "last_used_at": r["last_used_at"],
                         "all": r["sections"] is None})
            sync = [dict(r) for r in conn.execute("SELECT label, created_at, last_used_at FROM sync_aliases WHERE owner=? "
                                                  "ORDER BY created_at", (owner,))] if owner != "*" else []
            friends = invites = 0
            if subs and "friendships" in tables:
                marks = ",".join("?" * len(subs))
                friends = conn.execute(f"SELECT COUNT(*) FROM friendships WHERE a IN ({marks}) OR b IN ({marks})", subs + subs).fetchone()[0]
                invites = conn.execute(f"SELECT COUNT(*) FROM friend_invites WHERE sub IN ({marks}) AND expires>?",
                                       subs + [time.time()]).fetchone()[0]
            out[owner] = {"accounts": [{**{k: g[k] for k in ("sub", "email", "name", "last_seen")}, "picture": pictures.get(g["sub"])}
                                       for g in google if g["sub"] in subs],
                          "shares": shares, "voice": voice, "sync": sync, "friends": friends, "invites": invites}
        return out

    @app.delete("/api/v1/admin/google/{sub}", include_in_schema=False, dependencies=only)
    def unbind_google(sub: str):
        """The account stays able to sign in, but sees nothing until it is bound again."""
        with db() as conn:
            conn.execute("DELETE FROM google_links WHERE sub=?", (sub,))
        return {"success": True}

    @app.post("/api/v1/admin/tokens", include_in_schema=False, dependencies=only)
    def new_token(body: NewToken):
        """A new personal token for one person. It takes on each character it is first to upload;
        with character_id, an existing character is handed to it straight away."""
        token = "".join(secrets.choice(ALPHABET) for _ in range(TOKEN_LEN))
        with db() as conn:
            if body.character_id and not conn.execute("SELECT 1 FROM characters WHERE id=?", (body.character_id,)).fetchone():
                raise HTTPException(404, "character not found")
            conn.execute("INSERT INTO upload_tokens VALUES (?,?,?,?,?,?)",
                         (token, (body.label or "").strip() or None, None, _now(), None, None))
            if body.character_id:
                conn.execute("INSERT OR REPLACE INTO token_characters VALUES (?,?,?)", (body.character_id, token, _now()))
        return {"token": token}

    @app.post("/api/v1/admin/characters/{character_id}/token", include_in_schema=False, dependencies=only)
    def assign(character_id: str, token: Optional[str] = None):
        """Hand a character to a token, or (with no token) to nobody: the next personal token
        to upload it takes it. Its recorded data is not touched either way."""
        with db() as conn:
            if not conn.execute("SELECT 1 FROM characters WHERE id=?", (character_id,)).fetchone():
                raise HTTPException(404, "character not found")
            conn.execute("DELETE FROM token_characters WHERE character_id=?", (character_id,))
            if token:
                if not conn.execute("SELECT 1 FROM upload_tokens WHERE token=?", (token,)).fetchone():
                    raise HTTPException(404, "no such token")
                conn.execute("INSERT INTO token_characters VALUES (?,?,?)", (character_id, token, _now()))
        return {"success": True}

    @app.delete("/api/v1/admin/tokens/{token}", include_in_schema=False, dependencies=only)
    def delete_token(token: str):
        """The token stops working at once. Its characters keep their data and belong to nobody."""
        with db() as conn:
            if not conn.execute("DELETE FROM upload_tokens WHERE token=?", (token,)).rowcount:
                raise HTTPException(404, "no such token")
            conn.execute("DELETE FROM token_characters WHERE token=?", (token,))
            conn.execute("DELETE FROM google_links WHERE token=?", (token,))      # its Google account loses access too
            conn.execute("DELETE FROM view_tokens WHERE owner=?", (token,))       # and so do the tokens shared from it
            conn.execute("DELETE FROM sync_aliases WHERE owner=?", (token,))      # and the person's further sync tokens
        return {"success": True}
