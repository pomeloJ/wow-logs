from datetime import datetime, timedelta, timezone

from conftest import AUTH, READ_TOKEN
from test_items import snapshot

READ = {"Authorization": f"Bearer {READ_TOKEN}"}
T0 = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
UNITS = [{"guid": "Player-1-0000AAAA", "name": ""}, {"guid": "Player-1-0000B0B0", "name": "波波"},
         {"guid": "Pet-0-1-1-1-1-00000C", "name": "小狼", "pet": True}]
# the character opens, 波波 taunts at 4 s and keeps it, the character takes it back at 9 s
SAMPLES = ";".join([
    "0.0,1,100,100,100,3", "0.0,2,20,20,18,0",
    "2.0,1,300,100,100,3", "2.0,2,200,66,60,0", "2.0,3,30,10,9,0",
    "4.0,2,400,100,100,3", "4.0,1,380,95,95,1",
    "6.0,2,500,100,100,3", "6.0,1,450,90,90,0",
    "9.0,1,700,100,100,3", "9.0,2,600,85,86,1",
    "bad,row",
])


def seed(client, cid="c1", payload=None, minute=0, n=0):
    p = payload or {"mob_guid": "Creature-0-1-1-1-1234-00000A", "mob_name": "灰霧戰士",
                    "started": int(T0.timestamp()), "duration": 10, "units": UNITS, "samples": SAMPLES}
    r = client.post("/api/v1/events/batch", headers=AUTH, json={"schema_version": 1, "device_id": "mac", "events": [
        {"event_id": f"{cid}:t{n}", "character_id": cid, "event_type": "THREAT",
         "timestamp": (T0 + timedelta(minutes=minute)).isoformat(), "payload": p}]})
    assert r.json()["accepted"] == 1


def test_threat_per_fight_from_the_games_figures(client):
    snapshot(client)
    seed(client)
    r = client.get("/api/v1/characters/c1/threat", headers=READ).json()
    [f] = r["fights"]
    assert (f["mob_name"], f["duration"], f["started"], f["cut"]) == ("灰霧戰士", 10.0, "2026-11-20T12:00:00Z", False)
    by = {u["name"] if not u["is_me"] else "me": u for u in f["units"]}
    me, bobo, pet = by["me"], by["波波"], by["小狼"]
    assert [u["rank"] for u in (me, bobo, pet)] == [1, 2, 3] and me["name"] == "小德"
    assert (me["final_threat"], me["peak_threat"], me["avg_tps"], me["top_percent"]) == (700, 700, 70.0, 100)
    assert me["tanking_seconds"] == 4.0 + 1.0         # 0 to 4 s, then from 9 s to the end at 10 s
    assert bobo["tanking_seconds"] == 5.0 and bobo["final_threat"] == 600
    assert me["peak_tps"] == round((700 - 450) / 3, 1)   # best stretch of at least 3 s: 6 to 9 s
    assert pet["pet"] is True and pet["peak_tps"] is None  # a single reading has no rate
    assert [(c["t"], c["from"], c["to"]) for c in f["aggro_changes"]] == [(4.0, me["name"], "波波"), (9.0, "波波", me["name"])]
    assert (f["lost_aggro"], f["gained_aggro"]) == (1, 1)
    assert "series" not in me
    s = client.get("/api/v1/characters/c1/threat?series=true", headers=READ).json()["fights"][0]["units"]
    assert next(u for u in s if u["is_me"])["series"][0] == [0.0, 100, 100, 3]


def test_threat_filters_access_and_stays_out_of_timelines(client):
    snapshot(client, "c1")
    snapshot(client, "c2")
    seed(client, minute=0, n=0)
    seed(client, minute=30, n=1)
    seed(client, "c2", n=2)
    got = client.get("/api/v1/characters/c1/threat?start_time=2026-11-20T12:10:00Z", headers=READ).json()["fights"]
    assert [f["timestamp"] for f in got] == ["2026-11-20T12:30:00Z"]
    assert len(client.get("/api/v1/characters/c1/threat?limit=1", headers=READ).json()["fights"]) == 1
    tok = client.post("/api/v1/characters/c1/voice-token", headers=AUTH).json()["token"]
    S = {"Authorization": f"Bearer {tok}"}
    assert client.get("/api/v1/characters/c1/threat", headers=S).status_code == 200
    assert client.get("/api/v1/characters/c2/threat", headers=S).status_code == 403
    assert client.get("/api/v1/characters/c1/threat").status_code == 401
    schema = client.get("/openapi.json").json()
    assert "/api/v1/characters/{character_id}/threat" in schema["paths"]
    voice = client.get(f"/voice?t={tok}").text
    assert "灰霧戰士" not in voice


def test_a_hidden_enemy_and_odd_payloads_still_answer(client):
    seed(client, payload={"units": [{"name": ""}], "samples": "0.0,1,50,100,100,3;1.5,1,80,100,100,3", "cut": True})
    [f] = client.get("/api/v1/characters/c1/threat", headers=READ).json()["fights"]
    assert f["mob_name"] is None and f["cut"] is True and f["duration"] == 1.5
    assert f["units"][0]["final_threat"] == 80 and f["units"][0]["avg_tps"] == 53.3
    seed(client, payload={"samples": 5}, minute=1, n=1)
    assert client.get("/api/v1/characters/c1/threat", headers=READ).json()["fights"][0]["units"] == []
