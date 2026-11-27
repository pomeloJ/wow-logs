import os
import time
from datetime import datetime, timezone

from PIL import Image

from adventuresync import extras
from adventuresync.core import Queue

ME = "Player-1-0000ABCD"


class FakeUploader:
    def __init__(self, result=("ok", ""), shots=True):
        self.result, self.sent, self.shots = result, [], shots

    def screenshots_on(self, character_id=None):
        return self.shots

    def upload_screenshot(self, shot_id, character_id, taken_at, width, height, full, thumb):
        self.sent.append((shot_id, character_id, taken_at, width, height, len(full) > len(thumb) > 0))
        return self.result

    def upload_hits(self, character_id, source, rows):
        if self.result[0] == "ok":
            self.sent.append((character_id, source, [list(r) for r in rows]))
        return self.result


def game(tmp_path):
    (tmp_path / "Screenshots").mkdir()
    (tmp_path / "Logs").mkdir()
    return tmp_path


def shot(folder, name, age=60, size=(2560, 1440)):
    path = folder / "Screenshots" / name
    Image.new("RGB", size, (40, 90, 60)).save(path, "JPEG")
    t = time.time() - age
    os.utime(path, (t, t))
    return t


DB = {"characters": {"Player-OLD": {"captured_at": 10}, ME: {"captured_at": 20}}, "events": []}


def on_long_ago(q):
    """Screenshot upload was turned on before any of this test's pictures were taken."""
    extras.kv_set(q, "screenshots_since", 0)
    return q


def test_screenshots_are_resized_uploaded_once_and_map_shots_skipped(tmp_path):
    g, q, up = game(tmp_path), on_long_ago(Queue(":memory:")), FakeUploader()
    taken = shot(g, "WoWScrnShot_112226_200500.jpg")
    map_time = shot(g, "WoWScrnShot_112126_130356.jpg", age=600)
    shot(g, "fresh.jpg", age=0)                                  # still being written
    (g / "Screenshots" / "notes.txt").write_text("x")
    db = {**DB, "events": [{"event_type": "MAP_SCREENSHOT", "payload": {"local_ts": map_time - 2}}]}
    assert extras.process_screenshots(g, db, q, up) == 1
    sid, cid, at, w, h, sizes_ok = up.sent[0]
    assert (sid, cid, w, h, sizes_ok) == ("WoWScrnShot_112226_200500", ME, 1920, 1080, True)   # newest character
    assert at == extras.iso(taken)
    assert extras.process_screenshots(g, db, q, up) == 0 and len(up.sent) == 1                  # not sent twice
    done = extras.kv_get(q, "screenshots")
    assert done["WoWScrnShot_112126_130356.jpg"] == "map" and "fresh.jpg" not in done


def test_screenshot_upload_failures(tmp_path):
    g, q = game(tmp_path), on_long_ago(Queue(":memory:"))
    shot(g, "a.jpg")
    shot(g, "b.jpg")
    down = FakeUploader(("retry", "network"))
    assert extras.process_screenshots(g, DB, q, down) == 0 and len(down.sent) == 1     # stops at the first failure
    up = FakeUploader()
    assert extras.process_screenshots(g, DB, q, up) == 0 and not up.sent               # not again straight away
    extras._retry_at.clear()                                                           # (the wait is over)
    assert extras.process_screenshots(g, DB, q, up) == 2                               # and tries again later
    (g / "Screenshots" / "broken.jpg").write_bytes(b"not an image")
    os.utime(g / "Screenshots" / "broken.jpg", (time.time() - 60,) * 2)
    assert extras.process_screenshots(g, DB, q, FakeUploader()) == 0
    assert extras.kv_get(q, "screenshots")["broken.jpg"].startswith("unreadable")
    assert extras.process_screenshots(g, {}, q, FakeUploader()) == 0                    # no character known yet


def test_screenshots_go_up_only_while_turned_on_and_never_the_ones_from_before(tmp_path):
    g, q = game(tmp_path), Queue(":memory:")
    shot(g, "before.jpg")
    off = FakeUploader(shots=False)
    assert extras.process_screenshots(g, DB, q, off) == 0 and not off.sent
    assert extras.kv_get(q, "screenshots")["before.jpg"] == "off"
    on = FakeUploader()
    assert extras.process_screenshots(g, DB, q, on) == 0 and not on.sent              # taken while off: never sent
    shot(g, "after.jpg", age=1.5)                                   # taken just now, after it was turned on
    assert extras.process_screenshots(g, DB, q, on) == 0            # (first look: size noted)
    assert extras.process_screenshots(g, DB, q, on) == 1 and on.sent[0][0] == "after"
    # turned on for the first time with pictures already there (it was off while nothing ran): those stay here
    (tmp_path / "other").mkdir()
    g2, q2 = game(tmp_path / "other"), Queue(":memory:")
    shot(g2, "old.jpg", age=3600)
    first = FakeUploader()
    assert extras.process_screenshots(g2, DB, q2, first) == 0 and extras.kv_get(q2, "screenshots")["old.jpg"] == "off"
    shot(g2, "new.jpg", age=1.5)
    assert extras.process_screenshots(g2, DB, q2, first) == 0
    assert extras.process_screenshots(g2, DB, q2, first) == 1 and [x[0] for x in first.sent] == ["new"]
    assert extras.process_screenshots(g, DB, q, None) == 0             # no uploader: nothing to do


def test_a_new_screenshot_is_taken_as_soon_as_the_game_is_done_writing_it(tmp_path):
    g, q, up = game(tmp_path), on_long_ago(Queue(":memory:")), FakeUploader()
    shot(g, "new.jpg", age=1.5)
    assert extras.process_screenshots(g, DB, q, up) == 0          # first look: its size is noted
    assert extras.process_screenshots(g, DB, q, up) == 1          # same size a moment later: finished
    path = g / "Screenshots" / "half.jpg"
    path.write_bytes(b"\xff\xd8\xff" + b"0" * 100)              # half written: unreadable for now
    os.utime(path, (time.time() - 5,) * 2)
    assert extras.process_screenshots(g, DB, q, up) == 0
    assert "half.jpg" not in extras.kv_get(q, "screenshots")      # looked at again later, not given up on


# ---- combat log
P = f'{ME},"小德-伺服器",0x511,0x0'
MATE = 'Player-1-00AAAAAA,"小鈴-伺服器",0x512,0x0'
MOB = 'Creature-0-1-0-1-547-000ABC,"雷鱗巫師",0x10a48,0x0'
STRANGER = 'Player-1-00BBBBBB,"路人-伺服器",0x548,0x0'
PET = 'Pet-0-1-0-1-999-0100,"小白",0x1112,0x0'


def write_log(g, lines, age=300, name="WoWCombatLog.txt", mode="w"):
    path = g / "Logs" / name
    with open(path, mode, encoding="utf-8") as f:
        f.write("".join(line + "\n" for line in lines))
    t = time.time() - age
    os.utime(path, (t, t))
    return path


def queued(q):
    return q.pending_events(100)


def test_combat_log_totals_per_minute(tmp_path):
    g, q = game(tmp_path), Queue(":memory:")
    write_log(g, [
        f"11/22/2026 20:05:10.123  SWING_DAMAGE,{P},{MOB},50,-1,1,0,0,0,nil,nil,nil",
        f"11/22/2026 20:05:11.500  SPELL_DAMAGE,{P},{MOB},8921,\"月火術\",0x40,120,-1,64,0,0,0,nil,nil,nil",
        f"11/22/2026 20:05:12.000  RANGE_DAMAGE,{MATE},{MOB},75,\"射擊\",0x1,80,-1,1,0,0,0,nil,nil,nil",
        f"11/22/2026 20:05:13.000  SPELL_SUMMON,{MATE},{PET},883,\"召喚寵物\",0x1",
        f"11/22/2026 20:05:14.000  SWING_DAMAGE,{PET},{MOB},30,-1,1,0,0,0,nil,nil,nil",
        f"11/22/2026 20:05:15.000  SPELL_HEAL,{P},{MATE},774,\"回春術\",0x8,200,60,0,nil",
        f"11/22/2026 20:05:16.000  SPELL_PERIODIC_DAMAGE,{STRANGER},{MOB},1,\"x\",0x1,999,-1,1,0,0,0,nil,nil,nil",
        f"11/22/2026 20:05:17.000  SWING_MISSED,{P},{MOB},MISS,nil",
        f"11/22/2026 20:05:18.000  SWING_DAMAGE,{MOB},{P},40,-1,1,0,0,0,nil,nil,nil",
        "garbage line without a timestamp",
        f"11/22/2026 20:06:02.000  SWING_DAMAGE,{P},{MOB},10,-1,1,0,0,0,nil,nil,nil",
    ])
    assert extras.process_combat_logs(g, DB, q) == 2
    ev = queued(q)
    first, second = ev[0], ev[1]
    assert first["event_type"] == "GROUP_METER" and first["character_id"] == ME
    assert first["payload"] == {
        "source": "combatlog", "damage": {"": 170, "小鈴": 110}, "healing": {"": 140}, "taken": {"": 40},
        "spells": {"": {"近戰": 50, "月火術": 120}, "小鈴": {"射擊": 80, "寵物近戰": 30}},
        "best": {"": [120, "月火術", "雷鱗巫師", False], "小鈴": [80, "射擊", "雷鱗巫師", False]}, "fight": 9,
        "hits": {"": [2, 0], "小鈴": [2, 0]}, "over": {"": 60}, "hurt": {"雷鱗巫師｜近戰": 40}}
    assert second["payload"]["damage"] == {"": 10}
    # the pet counts for its owner; strangers, misses and damage taken are left out
    assert first["timestamp"].endswith(":05:59Z") and first["event_id"] != second["event_id"]
    assert extras.process_combat_logs(g, DB, q) == 0 and len(queued(q)) == 2           # nothing new


def test_combat_log_is_read_incrementally(tmp_path):
    g, q = game(tmp_path), Queue(":memory:")
    line = f"11/22/2026 20:05:10.000  SWING_DAMAGE,{P},{MOB},50,-1,1,0,0,0,nil,nil,nil"
    path = write_log(g, [line], age=5)                           # just written: the minute may not be over
    assert extras.process_combat_logs(g, DB, q) == 0
    with open(path, "a", encoding="utf-8") as f:                 # a half line, then the rest of it later
        f.write(f"11/22/2026 20:05:20.000  SWING_DAMAGE,{P},{MOB},25")
    assert extras.process_combat_logs(g, DB, q) == 0
    with open(path, "a", encoding="utf-8") as f:
        f.write(f",-1,1,0,0,0,nil,nil,nil\n11/22/2026 20:07:00.000  SWING_DAMAGE,{P},{MOB},5,-1,1,0,0,0,nil,nil,nil\n")
    os.utime(path, (time.time() - 5,) * 2)
    assert extras.process_combat_logs(g, DB, q) == 1             # 20:05 is complete once 20:07 appears
    assert queued(q)[0]["payload"]["damage"] == {"": 75}
    os.utime(path, (time.time() - 300,) * 2)                     # the log went quiet: close the last minute
    assert extras.process_combat_logs(g, DB, q) == 1
    assert [e["payload"]["damage"] for e in queued(q)] == [{"": 75}, {"": 5}]
    write_log(g, [line.replace("20:05:10", "21:00:00")], age=300)   # a new, shorter file under the same name
    assert extras.process_combat_logs(g, DB, q) == 1 and len(queued(q)) == 3


def test_combat_log_formats(tmp_path):
    # no year in the timestamp, and "advanced combat logging" with its extra block before the amount
    adv = "Player-1-0000ABCD,0000000000000000,950,1000,120,40,300,0,1,80,100,0,-1234.56,789.01,1439,3.14,23"
    year = time.localtime().tm_year
    t1 = extras.parse_line(f"11/22 20:05:10.123  SWING_DAMAGE,{P},{MOB},{adv},50,-1,1,0,0,0,nil,nil,nil", year)
    assert t1 and time.localtime(t1[0])[:5] == (year, 11, 22, 20, 5)
    m = extras.Meter()
    m.feed(*t1)
    m.feed(*extras.parse_line(f"11/22 20:05:11.000  SPELL_DAMAGE,{P},{MOB},8921,\"月火術\",0x40,{adv},120,-1,64,0,0,0,nil", year))
    m.feed(*extras.parse_line(f"11/22 20:05:12.000  SPELL_HEAL,{P},{P},774,\"回春術\",0x8,{adv},200,200,60,0,nil", year))
    m.close()
    assert m.done[0][:3] == (int(t1[0] // 60), {"": 170}, {"": 140}) and m.me == ME
    assert m.done[0][3]["spells"] == {"": {"近戰": 50, "月火術": 120}}
    assert extras.parse_line("not a log line", year) is None
    assert extras.amount_index(["SWING_DAMAGE"] * 9 + ["Player-1", "x", "y"], 9) is None     # unrecognised layout: skipped


def test_no_log_folder_or_unknown_owner(tmp_path):
    q = Queue(":memory:")
    assert extras.process_combat_logs(tmp_path, DB, q) == 0
    g = game(tmp_path)
    write_log(g, [f"11/22/2026 20:05:12.000  RANGE_DAMAGE,{MATE},{MOB},75,\"射擊\",0x1,80,-1,1,0,0,0,nil,nil,nil"])
    assert extras.process_combat_logs(g, {}, q) == 0             # whose log is it? keep it until known
    assert extras.process_combat_logs(g, DB, q) == 1             # the newest character on this computer
    assert queued(q)[0]["character_id"] == ME and queued(q)[0]["payload"]["damage"] == {"小鈴": 80}


def test_the_rest_of_a_minute_written_late_is_counted_too(tmp_path):
    # Seen in the game: the game writes its file in chunks. A minute closed after two quiet minutes
    # went on in the next chunk, and the rest of it was dropped as that same minute again.
    g, q = game(tmp_path), Queue(":memory:")
    write_log(g, [f"11/22/2026 20:05:10.000  SWING_DAMAGE,{P},{MOB},53,-1,1,0,0,0,nil,nil,nil"], age=300)
    assert extras.process_combat_logs(g, DB, q) == 1                      # quiet for a while: the minute is over
    write_log(g, [f"11/22/2026 20:05:40.000  SWING_DAMAGE,{P},{MOB},104,-1,1,0,0,0,nil,nil,nil",
                  f"11/22/2026 20:06:05.000  SWING_DAMAGE,{P},{MOB},7,-1,1,0,0,0,nil,nil,nil"], age=300, mode="a")
    assert extras.process_combat_logs(g, DB, q) == 2
    minutes = sorted((e["event_id"], e["payload"]["damage"][""]) for e in queued(q))
    first = minutes[0][0]
    assert [d for _, d in minutes if _.startswith(first)] == [53, 104]     # both parts of 20:05 kept
    assert sum(d for _, d in minutes) == 53 + 104 + 7 and len({i for i, _ in minutes}) == 3
    assert any(i == first + ":2" for i, _ in minutes)


def test_a_second_character_in_the_same_log_file_gets_its_own_fights(tmp_path):
    # Seen in the game: the log stays on across logging out, the same file goes on, and a second
    # character's play was filed under the first one.
    g, q, up = game(tmp_path), Queue(":memory:"), FakeUploader()
    alt_id = "Player-1-0000EEEE"
    alt = f'{alt_id},"小號-伺服器",0x511,0x0'
    write_log(g, [
        f"11/22/2026 20:05:10.000  SWING_DAMAGE,{P},{MOB},50,-1,1,0,0,0,nil,nil,nil",
        f"11/22/2026 20:05:11.000  SWING_DAMAGE,{MOB},{P},20,-1,1,0,0,0,nil,nil,nil",
        f"11/22/2026 20:05:40.000  SWING_DAMAGE,{alt},{MOB},70,-1,1,0,0,0,nil,nil,nil",       # the same minute, after the switch
        f"11/22/2026 20:06:05.000  SWING_DAMAGE,{alt},{MOB},30,-1,1,0,0,0,nil,nil,nil",
        f"11/22/2026 20:06:06.000  PARTY_KILL,{alt},{MOB},0",
    ])
    kill_time = extras.parse_line("11/22/2026 20:06:06.000  X,1", 2026)[0]
    assert extras.process_combat_logs(g, DB, q, saved_at=kill_time + 60, uploader=up) == 4
    assert [(who, len(rows)) for who, _, rows in up.sent] == [(ME, 2), (alt_id, 2)]    # each one's hits under its own name
    assert max(r[1] for r in up.sent[0][2]) < min(r[1] for r in up.sent[1][2])
    meters = sorted((e["character_id"], e["timestamp"][14:16], e["payload"]["damage"])
                    for e in queued(q) if e["event_type"] == "GROUP_METER")
    assert meters == sorted([(ME, "05", {"": 50}), (alt_id, "05", {"": 70}), (alt_id, "06", {"": 30})])
    kill, = [e for e in queued(q) if e["event_type"] == "MOB_KILLED"]
    assert kill["character_id"] == alt_id
    assert extras.process_combat_logs(g, DB, q, saved_at=kill_time + 90, uploader=up) == 0 and len(up.sent) == 2
    # the next character is told apart on a later pass too
    write_log(g, [f"11/22/2026 20:09:00.000  SWING_DAMAGE,{P},{MOB},5,-1,1,0,0,0,nil,nil,nil"], mode="a")
    extras.process_combat_logs(g, DB, q, uploader=up)
    assert up.sent[-1][0] == ME


def test_kills_come_from_the_log_when_the_addon_missed_them(tmp_path):
    g, q = game(tmp_path), Queue(":memory:")
    adv = "0000000000000000,950,1000,120,40,300,0,0,0,1,80,100,0,-1234.56,789.01,1439,3.14,23"
    mob2, mob3 = MOB.replace("000ABC", "000DEF"), MOB.replace("000ABC", "000123")
    old_pet = 'Pet-0-1-0-1-999-0200,"豹",0x1112,0x0'              # out before logging began: never summoned
    path = write_log(g, [
        '11/22/2026 20:05:00.000  ZONE_CHANGE,43,"哀嚎洞穴",0',
        f"11/22/2026 20:05:10.000  SWING_DAMAGE,{old_pet},{MOB},Pet-0-1-0-1-999-0200,Player-1-00AAAAAA,{adv[17:]},30,-1,1,0,0,0,nil",
        f"11/22/2026 20:05:11.000  RANGE_DAMAGE,{MATE},{MOB},75,\"射擊\",0x1,Creature-0-1-0-1-547-000ABC,{adv},80,-1,1,0,0,0,nil",
        f"11/22/2026 20:05:12.000  SWING_DAMAGE,{old_pet},{MOB},Pet-0-1-0-1-999-0200,Player-1-00AAAAAA,{adv[17:]},30,-1,1,0,0,0,nil",
        f"11/22/2026 20:05:20.000  PARTY_KILL,{MATE},{MOB},0",
        f"11/22/2026 20:05:30.000  PARTY_KILL,{P},{mob2},0",
        f"11/22/2026 20:05:40.000  PARTY_KILL,{old_pet},{mob3},0",
        f"11/22/2026 20:05:50.000  PARTY_KILL,{STRANGER},{MOB.replace('000ABC', '000999')},0",
        f"11/22/2026 20:05:55.000  PARTY_KILL,{P},Player-1-00CCCCCC,\"敵人\",0x548,0x0,0",
    ])
    kill_time = extras.parse_line("11/22/2026 20:05:30.000  X,1", 2026)[0]
    # the addon reported the second kill itself (it sees a scrambled unit id, two seconds later)
    q.add_events([{"event_id": "a:1", "character_id": ME, "event_type": "MOB_KILLED", "timestamp": extras.iso(kill_time + 2),
                   "payload": {"monster_guid": "Creature-0-9-9-9-547-FFFFFF", "monster_id": 547}}])
    assert extras.process_combat_logs(g, DB, q) == 1                       # addon not saved yet: damage only
    assert extras.process_combat_logs(g, DB, q, saved_at=kill_time - 60) == 0   # saved before the kills
    assert extras.process_combat_logs(g, DB, q, saved_at=kill_time + 60) == 2
    assert extras.process_combat_logs(g, DB, q, saved_at=kill_time + 90) == 0   # once only
    ev = {e["event_id"]: e for e in queued(q)}
    meter = next(e for e in ev.values() if e["event_type"] == "GROUP_METER")
    assert meter["payload"]["damage"] == {"小鈴": 140}                   # the pet's hit after its owner is known
    kills = [e for e in ev.values() if e["event_type"] == "MOB_KILLED" and e["event_id"].startswith("combatlog:")]
    assert sorted((k["payload"]["monster_id"], k["payload"].get("killed_by"), k["payload"]["zone_name"],
                   k["payload"]["monster_name"]) for k in kills) == [(547, "小鈴", "哀嚎洞穴", "雷鱗巫師")] * 2
    assert all(k["character_id"] == ME and k["payload"]["source"] == "combatlog" for k in kills)
    assert path.exists()


def test_boss_fights_and_hardest_hits(tmp_path):
    g, q = game(tmp_path), Queue(":memory:")
    adv = "Creature-0-1-0-1-547-000ABC,0000000000000000,950,1000,120,40,300,0,0,0,1,80,100,0,-1234.56,789.01,1439,3.14,23"
    write_log(g, [
        f"11/22/2026 20:05:01.000  SWING_DAMAGE,{P},{MOB},{adv},40,40,-1,1,0,0,0,nil,nil,nil",       # before the fight
        '11/22/2026 20:05:02.000  ENCOUNTER_START,585,"安娜科德拉",1,5,43',
        f"11/22/2026 20:05:03.000  RANGE_DAMAGE,{MATE},{MOB},75,\"瞄準射擊\",0x1,{adv},300,210,-1,1,0,0,0,1,nil,nil,ST",
        f"11/22/2026 20:05:04.000  DAMAGE_SHIELD,{P},{MOB},782,\"荊棘術\",0x8,{adv},9,9,-1,8,0,0,0,nil,nil,nil,AOE",
        f"11/22/2026 20:05:05.000  SWING_DAMAGE,{MOB},{MATE},{adv},66,66,-1,1,0,0,0,nil,nil,nil",
        f"11/22/2026 20:05:06.000  SPELL_HEAL,{P},{MATE},774,\"回春術\",0x8,{adv},90,90,20,0,nil",
        '11/22/2026 20:05:32.000  ENCOUNTER_END,585,"安娜科德拉",1,5,1,30063',
        f"11/22/2026 20:05:40.000  SWING_DAMAGE,{P},{MOB},{adv},5,5,-1,1,0,0,0,nil,nil,nil",         # after it
    ])
    assert extras.process_combat_logs(g, DB, q) == 2
    ev = {e["event_type"]: e for e in queued(q)}
    boss = ev["GROUP_ENCOUNTER"]
    assert boss["payload"] == {"source": "combatlog", "name": "安娜科德拉", "success": True, "duration": 30,
                               "damage": {"小鈴": 300, "": 9}, "healing": {"": 70}, "taken": {"小鈴": 66}}
    assert boss["timestamp"].endswith(":05:32Z") and boss["character_id"] == ME
    p = ev["GROUP_METER"]["payload"]
    assert p["best"]["小鈴"] == [300, "瞄準射擊", "雷鱗巫師", True]             # a critical hit
    assert p["spells"][""] == {"近戰": 45, "荊棘術": 9} and p["taken"] == {"小鈴": 66}
    assert p["hits"] == {"": [2, 0], "小鈴": [1, 1]} and p["over"] == {"": 20} and p["hurt"] == {"雷鱗巫師｜近戰": 66}
    assert p["fight"] == 6            # 20:05:01-05 in a row, then one more hit after a long pause


def test_dispels_and_interrupts_are_counted(tmp_path):
    g, q = game(tmp_path), Queue(":memory:")
    write_log(g, [
        f"11/22/2026 20:05:01.000  SPELL_DISPEL,{MATE},{P},2893,\"驅毒術\",0x8,744,\"毒藥\",8,DEBUFF",
        f"11/22/2026 20:05:02.000  SPELL_INTERRUPT,{P},{MOB},6552,\"拳擊\",0x1,403,\"閃電箭\",8",
        f"11/22/2026 20:05:03.000  SPELL_DISPEL,{STRANGER},{STRANGER},1152,\"純淨術\",0x2,6907,\"疾病\",8,DEBUFF",
    ])
    assert extras.process_combat_logs(g, DB, q) == 1
    assert queued(q)[0]["payload"]["cc"] == {"小鈴": [1, 0], "": [0, 1]}


def test_every_hit_miss_and_heal_is_sent_as_a_row(tmp_path):
    g, q = game(tmp_path), Queue(":memory:")
    mob = "Creature-0-1-0-1-547-000ABC"
    target = f"{mob},0000000000000000,662,860,74,0,403,0,0,0,1,0,0,0,2558.76,-1215.10,1440,0.3656,21"
    me_unit = f"{ME},0000000000000000,766,965,354,0,1622,0,0,0,1,132,1000,0,-42.59,209.80,0,5.5481,11"
    path = write_log(g, [
        f"11/22/2026 20:05:01.250  RANGE_DAMAGE,{MATE},{MOB},19434,\"瞄準射擊\",0x1,{target},276,300,-1,1,0,0,0,1,nil,nil,ST",
        f"11/22/2026 20:05:02.000  SWING_DAMAGE,{P},{MOB},{me_unit},52,69,-1,1,0,0,0,nil,1,nil",
        f"11/22/2026 20:05:03.000  SWING_MISSED,{P},{MOB},DODGE,nil",
        f"11/22/2026 20:05:04.000  SPELL_MISSED,{MOB},{P},403,\"閃電箭\",0x8,RESIST,nil,ST",
        f"11/22/2026 20:05:05.000  SWING_DAMAGE,{MOB},{MATE},{mob},0000000000000000,4,1716,81,0,651,0,0,0,1,0,0,0,-76.31,218.74,0,2.5,19,36,63,-1,1,0,5,0,nil,nil,nil",
        f"11/22/2026 20:05:06.000  SPELL_HEAL,{P},{MATE},774,\"回春術\",0x8,{target},90,90,20,0,1",
        f"11/22/2026 20:05:07.000  SWING_DAMAGE,{STRANGER},{MOB},{me_unit},99,99,-1,1,0,0,0,nil,nil,nil",
        f"11/22/2026 20:05:08.000  SWING_MISSED,{STRANGER},{MOB},MISS,nil",
        f"11/22/2026 20:05:09.000  SPELL_SUMMON,{MATE},{PET},883,\"召喚寵物\",0x1",
        f"11/22/2026 20:05:10.000  SWING_DAMAGE,{MOB},{PET},{mob},0000000000000000,4,1716,81,0,651,0,0,0,1,0,0,0,-76.31,218.74,0,2.5,19,22,30,-1,1,0,0,0,nil,nil,nil",
        f"11/22/2026 20:05:10.500  SPELL_CAST_START,{MATE},0000000000000000,nil,0x80000000,0x80000000,19434,\"瞄準射擊\",0x1",
        f"11/22/2026 20:05:11.000  SPELL_CAST_SUCCESS,{MATE},{MOB},19434,\"瞄準射擊\",0x1,Player-1-00AAAAAA,0000000000000000,933,933,529,5,638,0,0,0,0,1270,1380,75,-568.27,-2282.13,1414,5.3881,18",
        f"11/22/2026 20:05:11.200  SPELL_AURA_APPLIED,{MATE},{MOB},1978,\"毒蛇釘刺\",0x8,DEBUFF",
        f"11/22/2026 20:05:11.300  SPELL_AURA_APPLIED,{P},{MATE},1126,\"野性印記\",0x8,BUFF,10",
        f"11/22/2026 20:05:11.400  SPELL_AURA_REMOVED,{STRANGER},{STRANGER},1,\"x\",0x1,BUFF",
        f"11/22/2026 20:05:11.500  UNIT_DIED,0000000000000000,nil,0x80000000,0x80000000,{PET},0",
        f"11/22/2026 20:05:11.600  UNIT_DIED,0000000000000000,nil,0x80000000,0x80000000,{MOB},0",
        f"11/22/2026 20:05:12.000  SPELL_CAST_SUCCESS,{STRANGER},{MOB},1,\"x\",0x1,Player-1-00BBBBBB,0000000000000000,933,933,529,5,638,0,0,0,0,1270,1380,75,-568.27,-2282.13,1414,5.3881,18",
    ])
    down = FakeUploader(("retry", "offline"))
    assert extras.process_combat_logs(g, DB, q, uploader=down) == 0 and not queued(q)   # nothing is remembered
    up = FakeUploader()
    assert extras.process_combat_logs(g, DB, q, uploader=up) == 1
    (character, source, rows), = up.sent
    assert (character, source, len(rows)) == (ME, "WoWCombatLog.txt", 11)
    names = ("pos", "ts", "way", "who", "pet", "unit", "enemy", "npc_id", "spell_id", "spell", "school", "kind",
             "amount", "raw", "overkill", "resisted", "blocked", "absorbed", "crit", "glancing", "crushing", "miss",
             "t_hp", "t_maxhp", "t_armor", "t_level", "s_ap", "guid")
    shot, swing, dodge, resist, taken, heal, bitten, cast, sting, mark, died = [dict(zip(names, r)) for r in sorted(rows, key=lambda r: (r[2] in ("aura", "death"), r[0]))]
    assert cast["raw"] == 500                                          # half a second from start to finish
    assert (sting["way"], sting["who"], sting["spell"], sting["kind"], sting["miss"], sting["enemy"], sting["guid"]) == \
        ("aura", "小鈴", "毒蛇釘刺", "APPLIED", "DEBUFF", "雷鱗巫師", mob)
    assert (mark["who"], mark["unit"], mark["spell"], mark["miss"], mark["enemy"]) == ("小鈴", "小德-伺服器", "野性印記", "BUFF", None)
    assert (died["way"], died["who"], died["pet"], died["unit"]) == ("death", "小鈴", 1, "小白")
    assert shot["guid"] == mob and resist["guid"] == mob and heal["guid"] == "Player-1-00AAAAAA"
    assert (bitten["way"], bitten["who"], bitten["pet"], bitten["unit"], bitten["amount"]) == ("in", "小鈴", 1, "小白", 22)
    assert (cast["way"], cast["who"], cast["spell"], cast["amount"], cast["kind"], cast["enemy"], cast["guid"]) == \
        ("cast", "小鈴", "瞄準射擊", 75, "0", "雷鱗巫師", mob)
    assert all(len(r) == len(names) for r in rows) and shot["pos"] == 0 and swing["pos"] > 0
    assert shot["ts"] % 1000 == 250 and swing["ts"] - shot["ts"] == 750
    assert (shot["way"], shot["who"], shot["enemy"], shot["npc_id"], shot["spell_id"], shot["spell"], shot["kind"]) == \
        ("out", "小鈴", "雷鱗巫師", 547, 19434, "瞄準射擊", "range")
    assert (shot["amount"], shot["raw"], shot["crit"], shot["t_hp"], shot["t_maxhp"], shot["t_armor"], shot["t_level"]) == \
        (276, 300, 1, 662, 860, 403, 21)
    assert (swing["who"], swing["spell"], swing["kind"], swing["amount"], swing["raw"], swing["crit"], swing["glancing"]) == \
        ("", "近戰", "swing", 52, 69, 0, 1)
    assert swing["s_ap"] == 354 and swing["t_armor"] is None          # that line describes the attacker
    assert (dodge["miss"], dodge["amount"], dodge["spell"], dodge["way"]) == ("DODGE", 0, "近戰", "out")
    assert (resist["way"], resist["who"], resist["enemy"], resist["miss"], resist["spell"]) == ("in", "", "雷鱗巫師", "RESIST", "閃電箭")
    assert (taken["way"], taken["who"], taken["enemy"], taken["amount"], taken["blocked"]) == ("in", "小鈴", "雷鱗巫師", 36, 5)
    assert (heal["way"], heal["who"], heal["amount"], heal["raw"], heal["crit"], heal["kind"]) == ("heal", "", 70, 90, 1, "heal")
    assert extras.process_combat_logs(g, DB, q, uploader=up) == 0 and len(up.sent) == 1     # not sent twice
    # how far the file has got: the newest line of all, someone else's included
    assert extras.kv_get(q, extras.READ) == time.mktime((2026, 11, 22, 20, 5, 12, 0, 0, -1))
    assert path.exists()


def test_the_sync_program_says_whether_the_players_fights_come_through_the_combat_log():
    import json as js
    from adventuresync.gui import combat_text
    q = Queue(":memory:")
    now = 1795700000.0

    def fight(end, seconds, n):
        at = datetime.fromtimestamp(end, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        q.add_events([{"event_id": f"a:{n}", "character_id": "Player-1-1", "event_type": "COMBAT", "timestamp": at,
                       "payload": {"duration": seconds}}])

    def mine(*times):                                   # rows read from the log: [pos, ms, way, who, ...]
        extras.note_seen(q, [[0, int(t * 1000), "out", ""] for t in times] + [[0, int(times[0] * 1000), "out", "小鈴"]])

    fight(now - 9000, 20, 0)                            # before this version kept track: not held against anyone
    c = extras.combat_log_check(q, now - 5000)
    assert c["state"] == "waiting" and combat_text(c)["banner"] is None     # nothing fought yet: nothing to say
    fight(now - 3000, 20, 1)                            # the addon saw a fight; the log had nothing then
    c = extras.combat_log_check(q, now)
    assert (c["state"], c["missed"]) == ("missing", 1) and combat_text(c)["row"] == "✗ 沒收到"
    assert "/adventure combatlog on" in combat_text(c)["banner"]   # "auto" switches; "on" only ever switches on
    mine(now - 1200)                                    # switched on, and the player hit something
    fight(now - 1190, 15, 2)
    c = extras.combat_log_check(q, now)
    assert (c["state"], c["fights"], c["missed"]) == ("ok", 2, 1)                     # the newest fight is covered
    assert combat_text(c)["row"].startswith("✓ 有收到") and combat_text(c)["banner"] is None
    mine(now - 60)                                      # in the middle of a fight, before any save: proof enough
    fight(now - 600, 20, 3)                             # a fight the log missed, yet something came in since
    assert extras.combat_log_check(q, now)["state"] == "ok"
    assert extras.combat_log_check(q, now + 3600)["state"] == "missing"               # but later, that fight decides
    # only the player's own rows count, and they are kept as 5-second steps
    assert all(t % 5 == 0 for t in js.loads(q.conn.execute("SELECT value FROM kv WHERE key='combatseen'").fetchone()[0]))
    # fights from long ago say nothing about today's play
    assert extras.combat_log_check(q, now + 2 * 86400)["state"] == "waiting"
    # Seen in the game: the game writes the file minutes late. A fresh fight with nothing in the
    # file yet is not held against the log until the file has got past it, or a good while passed.
    later = now + 5 * 86400
    fight(later - 120, 10, 4)
    c = extras.combat_log_check(q, later)
    assert c["state"] == "checking" and c["missed"] == 0 and combat_text(c)["banner"] is None
    extras.kv_set(q, extras.READ, later - 60)          # the file has caught up past it: no fight in it
    assert extras.combat_log_check(q, later)["state"] == "missing"
    extras.kv_set(q, extras.READ, later - 200)
    assert extras.combat_log_check(q, later + extras.SETTLE)["state"] == "missing"


def test_an_unchanged_folder_costs_one_look(tmp_path, monkeypatch):
    g, q, up = game(tmp_path), on_long_ago(Queue(":memory:")), FakeUploader()
    shot(g, "a.jpg")
    assert extras.process_screenshots(g, DB, q, up) == 1
    reads = []
    monkeypatch.setattr(extras, "kv_get", lambda *a, **k: reads.append(a) or {})
    for _ in range(5):
        assert extras.process_screenshots(g, DB, q, up) == 0
    assert reads == []                                   # nothing was read: the folder had not changed
    monkeypatch.undo()
    time.sleep(0.05)
    shot(g, "b.jpg", age=1.5)                            # a new picture changes the folder
    assert extras.process_screenshots(g, DB, q, up) == 0  # (first look: size noted)
    assert extras.process_screenshots(g, DB, q, up) == 1 and up.sent[-1][0] == "b"
