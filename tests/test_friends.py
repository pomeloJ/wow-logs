"""Friends: two Google accounts see each other's characters, each side choosing what it shows."""
from conftest import READ_TOKEN, TOKEN
from test_auth import GOOGLE, FakeGoogle, begin, full_record, good_claims, seen, upload


def site(tmp_path):
    """One server, and a way to open a browser signed in as a given Google account."""
    from fastapi.testclient import TestClient
    from app.main import create_app
    google = FakeGoogle()
    app = create_app(str(tmp_path / "f.db"), TOKEN, READ_TOKEN, google=dict(GOOGLE), google_exchange=google)
    lan = TestClient(app, client=("192.168.1.5", 1))

    def browser(sub, email, name):
        web = TestClient(app, follow_redirects=False, client=("192.168.1.5", 2))
        asked = begin(web)
        google.claims = good_claims(asked, sub=sub, email=email, name=name)
        assert web.get("/auth/google/callback", params={"state": asked["state"], "code": "c"}).status_code == 302
        return web
    return lan, browser


def person(lan, browser, sub, name, chars):
    """A player with their own token and characters, signed in and bound."""
    token = lan.post("/api/v1/admin/tokens", json={"label": name}).json()["token"]
    for c in chars:
        upload(lan, token, c)
    web = browser(sub, f"{sub}@gmail.com", name)
    assert web.post("/api/v1/me/link", json={"token": token}).status_code == 200
    return web, token


def befriend(a, b):
    code = a.post("/api/v1/me/friends/invite").json()["made"]
    assert b.get(f"/api/v1/me/friends/invite/{code}").json()["from"]["name"] == "小明"
    return b.post("/api/v1/me/friends/accept", json={"code": code})


def test_an_invite_makes_two_accounts_friends_who_see_each_others_characters(tmp_path):
    lan, browser = site(tmp_path)
    ming, ming_token = person(lan, browser, "s-ming", "小明", ["m1", "m2"])
    hua, _ = person(lan, browser, "s-hua", "小華", ["h1"])
    full_record(lan, ming_token, "m1")
    stranger, _ = person(lan, browser, "s-zz", "路人", ["z1"])
    assert ming.get("/api/v1/me/friends").json()["friends"] == []
    r = befriend(ming, hua)
    assert r.status_code == 200
    f, = r.json()["friends"]
    assert f["name"] == "小明" and [c["id"] for c in f["shown"]["characters"]] == ["m1", "m2"]
    assert f["shown"]["sections"] == ["location", "activity", "items", "quests", "character", "groups"]   # not the bank, trades or screenshots
    # 小華 reads 小明's characters through the friend header, with the sign-in cookie only
    fid = f["id"]
    assert seen(hua, **{"X-Friend": fid}) == ["m1", "m2"]
    snap = hua.get("/api/v1/characters/m1/snapshot", headers={"X-Friend": fid}).json()
    assert snap["data"]["money"] == 12345
    assert hua.get("/api/v1/screenshots?character_id=m1", headers={"X-Friend": fid}).status_code == 403
    assert hua.get("/api/v1/characters/h1/snapshot", headers={"X-Friend": fid}).status_code in (403, 404)    # never someone else's
    assert all(e["event_type"] != "CHAT_MESSAGE" for e in hua.get("/api/v1/events?character_id=m1", headers={"X-Friend": fid}).json()["items"])
    # and the other way round
    back, = ming.get("/api/v1/me/friends").json()["friends"]
    assert seen(ming, **{"X-Friend": back["id"]}) == ["h1"]
    # not friends: nothing, whatever id is tried
    assert seen(stranger, **{"X-Friend": fid}) == 403
    assert seen(lan, **{"X-Friend": fid}) == 403                    # no sign-in at all
    # the link is used up
    assert stranger.post("/api/v1/me/friends/accept", json={"code": "nope-nope"}).status_code == 404


def test_each_side_chooses_what_it_shows_and_removing_cuts_both_ways(tmp_path):
    lan, browser = site(tmp_path)
    ming, _ = person(lan, browser, "s-ming", "小明", ["m1", "m2"])
    hua, _ = person(lan, browser, "s-hua", "小華", ["h1"])
    befriend(ming, hua)
    to_hua, = ming.get("/api/v1/me/friends").json()["friends"]
    d = ming.post("/api/v1/me/friends/share", json={"id": to_hua["id"], "sections": ["location"], "characters": ["m2", "h1"]}).json()
    assert d["friends"][0]["mine"] == {"characters": ["m2"], "all": False, "sections": ["location"]}      # never another's character
    from_ming, = hua.get("/api/v1/me/friends").json()["friends"]
    assert [c["id"] for c in from_ming["shown"]["characters"]] == ["m2"] and from_ming["shown"]["sections"] == ["location"]
    assert seen(hua, **{"X-Friend": from_ming["id"]}) == ["m2"]
    assert hua.get("/api/v1/stats/activity?character_id=m2", headers={"X-Friend": from_ming["id"]}).status_code == 403
    # back to every character, including ones added later
    ming.post("/api/v1/me/friends/share", json={"id": to_hua["id"], "sections": ["location"], "characters": None})
    assert seen(hua, **{"X-Friend": from_ming["id"]}) == ["m1", "m2"]
    assert ming.post("/api/v1/me/friends/share", json={"id": to_hua["id"], "sections": ["bogus"]}).status_code == 400
    assert ming.post("/api/v1/me/friends/share", json={"id": "unknown", "sections": []}).status_code == 404
    # no longer friends: neither sees the other
    assert ming.post("/api/v1/me/friends/remove", json={"id": to_hua["id"]}).json()["friends"] == []
    assert hua.get("/api/v1/me/friends").json()["friends"] == []
    assert seen(hua, **{"X-Friend": from_ming["id"]}) == 403


def test_the_servers_own_account_shares_only_characters_it_ticks(tmp_path):
    lan, browser = site(tmp_path)
    upload(lan, TOKEN, "someone-elses")
    boss = browser("s-boss", "boss@gmail.com", "小明")
    assert boss.post("/api/v1/me/link", json={"token": TOKEN}).status_code == 200
    hua, _ = person(lan, browser, "s-hua", "小華", ["h1"])
    befriend(boss, hua)
    from_boss, = hua.get("/api/v1/me/friends").json()["friends"]
    assert from_boss["shown"]["characters"] == [] and seen(hua, **{"X-Friend": from_boss["id"]}) == 403
    to_hua, = boss.get("/api/v1/me/friends").json()["friends"]
    assert to_hua["mine"]["all"] is False
    boss.post("/api/v1/me/friends/share", json={"id": to_hua["id"], "sections": ["location"], "characters": ["h1"]})
    assert seen(hua, **{"X-Friend": from_boss["id"]}) == ["h1"]


def test_invites_are_one_use_limited_and_never_from_another_site(tmp_path):
    lan, browser = site(tmp_path)
    ming, _ = person(lan, browser, "s-ming", "小明", ["m1"])
    hua, _ = person(lan, browser, "s-hua", "小華", ["h1"])
    code = ming.post("/api/v1/me/friends/invite").json()["made"]
    assert ming.post("/api/v1/me/friends/accept", json={"code": code}).status_code == 409          # one's own
    assert ming.get(f"/api/v1/me/friends/invite/{code}").json()["self"] is True
    assert hua.post("/api/v1/me/friends/accept", json={"code": code}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert hua.post("/api/v1/me/friends/accept", json={"code": code}).status_code == 200
    assert hua.post("/api/v1/me/friends/accept", json={"code": code}).status_code == 404            # used up
    for _ in range(4):
        ming.post("/api/v1/me/friends/invite")
    assert len(ming.get("/api/v1/me/friends").json()["invites"]) == 4
    assert ming.post("/api/v1/me/friends/invite").status_code == 200
    assert ming.post("/api/v1/me/friends/invite").status_code == 409
    first = ming.get("/api/v1/me/friends").json()["invites"][0]["code"]
    assert len(ming.post("/api/v1/me/friends/invite/delete", json={"code": first}).json()["invites"]) == 4
    # an account not bound to a token, or not signed in, does none of this
    loner = browser("s-loner", "loner@gmail.com", "路人")
    assert loner.post("/api/v1/me/friends/invite").status_code == 403
    assert lan.get("/api/v1/me/friends").status_code == 401


def test_what_each_side_shows_is_chosen_before_becoming_friends(tmp_path):
    import sqlite3
    lan, browser = site(tmp_path)
    ming, ming_token = person(lan, browser, "s-ming", "小明", ["m1", "m2"])
    hua, _ = person(lan, browser, "s-hua", "小華", ["h1", "h2"])
    full_record(lan, ming_token, "m1")
    # the invite carries what its maker will show: here one character, its position only
    made = ming.post("/api/v1/me/friends/invite", json={"sections": ["location"], "characters": ["m1", "h1"]}).json()
    code = made["made"]
    assert (made["invites"][0]["sections"], made["invites"][0]["characters"]) == (["location"], ["m1"])   # not someone else's
    assert ming.post("/api/v1/me/friends/invite", json={"sections": ["nonsense"]}).status_code == 400
    # the one invited sees that before accepting, and what is needed to choose their own
    look = hua.get(f"/api/v1/me/friends/invite/{code}").json()
    assert look["shows_you"]["sections"] == ["location"] and [c["id"] for c in look["shows_you"]["characters"]] == ["m1"]
    assert [c["id"] for c in look["my_characters"]] == ["h1", "h2"] and "screenshots" not in look["default"]
    assert not {"bank", "trades"} & set(look["default"])
    # until someone accepts, its maker can change what it shows; nobody else can
    assert hua.post("/api/v1/me/friends/invite/edit", json={"code": code, "sections": ["quests"]}).status_code == 404
    assert ming.post("/api/v1/me/friends/invite/edit", json={"code": code, "sections": ["nonsense"]}).status_code == 400
    edited = ming.post("/api/v1/me/friends/invite/edit", json={"code": code, "sections": ["location", "bank"], "characters": ["m1"]}).json()
    assert (edited["invites"][0]["sections"], edited["invites"][0]["characters"]) == (["location", "bank"], ["m1"])
    assert hua.get(f"/api/v1/me/friends/invite/{code}").json()["shows_you"]["sections"] == ["location", "bank"]
    ming.post("/api/v1/me/friends/invite/edit", json={"code": code, "sections": ["location"], "characters": ["m1"]})
    # accepting carries what they show back
    f, = hua.post("/api/v1/me/friends/accept", json={"code": code, "sections": ["items", "quests"], "characters": None}).json()["friends"]
    assert f["shown"]["sections"] == ["location"] and [c["id"] for c in f["shown"]["characters"]] == ["m1"]
    assert f["mine"]["sections"] == ["items", "quests"] and f["mine"]["all"]
    # and the server holds to it: 小華 reads 小明's m1 position, not its items or m2
    fid = f["id"]
    assert seen(hua, **{"X-Friend": fid}) == ["m1"]
    assert hua.get("/api/v1/locations?character_id=m1", headers={"X-Friend": fid}).status_code == 200
    assert hua.get("/api/v1/characters/m1/items", headers={"X-Friend": fid}).status_code == 403

    # an invite made before choices existed still gives what it always did
    old = ming.post("/api/v1/me/friends/invite").json()["made"]
    db = sqlite3.connect(tmp_path / "f.db")
    db.execute("UPDATE friend_invites SET shares=NULL WHERE code=?", (old,))
    db.commit()
    zz, _ = person(lan, browser, "s-zz", "路人", ["z1"])
    assert zz.get(f"/api/v1/me/friends/invite/{old}").json()["shows_you"]["sections"] == look["default"]
    g, = zz.post("/api/v1/me/friends/accept", json={"code": old}).json()["friends"]
    assert g["shown"]["sections"] == look["default"] and g["mine"]["sections"] == look["default"]
