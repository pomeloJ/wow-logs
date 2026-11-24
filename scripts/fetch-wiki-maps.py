#!/usr/bin/env python3
"""Download Forever zone maps from Warcraft Wiki and set them as dashboard backgrounds.

The wiki's "coordinate-safe" maps are the game's map art at original size, so a
coordinate of (x, y) percent is x% from the left and y% from the top.

The images are copyrighted by Blizzard Entertainment and offered for home,
non-commercial, personal use only. They are cached in maps-cache/ (not committed)
and uploaded only to your own server.

Usage (from the repo root):
    .venv/bin/python scripts/fetch-wiki-maps.py            # all known zones
    .venv/bin/python scripts/fetch-wiki-maps.py 1436 1429  # only these map ids
"""
import io
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))
from adventuresync.core import Queue, Uploader, load_config  # noqa: E402

SOURCE = "https://warcraft.wiki.gg/wiki/Special:FilePath/WorldMap-{}_c60.jpg"

# uiMapID -> (wiki file token, English name). IDs are the Classic-era ones; 1415 and
# 1436 were confirmed against this client's own data, the rest are assumed to match.
MAPS = {
    1411: ("Durotar", "Durotar"), 1412: ("Mulgore", "Mulgore"), 1413: ("Barrens", "The Barrens"),
    1414: ("Kalimdor", "Kalimdor"), 1415: ("EasternKingdoms", "Eastern Kingdoms"),
    1416: ("Alterac", "Alterac Mountains"), 1417: ("Arathi", "Arathi Highlands"),
    1418: ("Badlands", "Badlands"), 1419: ("BlastedLands", "Blasted Lands"),
    1420: ("Tirisfal", "Tirisfal Glades"), 1421: ("Silverpine", "Silverpine Forest"),
    1422: ("WesternPlaguelands", "Western Plaguelands"),
    1423: ("EasternPlaguelands", "Eastern Plaguelands"), 1424: ("Hillsbrad", "Hillsbrad Foothills"),
    1425: ("Hinterlands", "The Hinterlands"), 1426: ("DunMorogh", "Dun Morogh"),
    1427: ("SearingGorge", "Searing Gorge"), 1428: ("BurningSteppes", "Burning Steppes"),
    1429: ("Elwynn", "Elwynn Forest"), 1430: ("DeadwindPass", "Deadwind Pass"),
    1431: ("Duskwood", "Duskwood"), 1432: ("LochModan", "Loch Modan"),
    1433: ("Redridge", "Redridge Mountains"), 1434: ("Stranglethorn", "Stranglethorn Vale"),
    1435: ("SwampofSorrows", "Swamp of Sorrows"), 1436: ("Westfall", "Westfall"),
    1437: ("Wetlands", "Wetlands"), 1438: ("Teldrassil", "Teldrassil"),
    1439: ("Darkshore", "Darkshore"), 1440: ("Ashenvale", "Ashenvale"),
    1441: ("ThousandNeedles", "Thousand Needles"),
    1442: ("StonetalonMountain", "Stonetalon Mountains"), 1443: ("Desolace", "Desolace"),
    1444: ("Feralas", "Feralas"), 1445: ("DustwallowMarsh", "Dustwallow Marsh"),
    1446: ("Tanaris", "Tanaris"), 1447: ("Azshara", "Azshara"), 1448: ("Felwood", "Felwood"),
    1449: ("UnGoroCrater", "Un'Goro Crater"), 1450: ("Moonglade", "Moonglade"),
    1451: ("Silithus", "Silithus"), 1452: ("Winterspring", "Winterspring"),
    1453: ("StormwindCity", "Stormwind City"), 1454: ("Orgrimmar", "Orgrimmar"),
    1455: ("Ironforge", "Ironforge"), 1456: ("ThunderBluff", "Thunder Bluff"),
    1457: ("Darnassus", "Darnassus"), 1458: ("Undercity", "Undercity"),
    1459: ("AlteracValley", "Alterac Valley"), 1460: ("WarsongGulch", "Warsong Gulch"),
    1461: ("ArathiBasin", "Arathi Basin"),
}


def main() -> int:
    wanted = [int(a) for a in sys.argv[1:]] or sorted(MAPS)
    cache = ROOT / "maps-cache"
    cache.mkdir(exist_ok=True)
    cfg = load_config(ROOT / "agent" / "config.toml")
    uploader = Uploader(cfg, Queue(cfg.db_path))
    web = httpx.Client(follow_redirects=True, timeout=30, headers={"User-Agent": "map-fetch-script"})
    ok = failed = 0
    for map_id in wanted:
        if map_id not in MAPS:
            print(f"{map_id}: unknown map id, skipped")
            continue
        token, name = MAPS[map_id]
        path = cache / f"{map_id}-{token}.jpg"
        if not path.exists():
            r = web.get(SOURCE.format(token))
            if r.status_code == 429:  # rate limited: wait as asked, then try once more
                wait = min(int(r.headers.get("Retry-After", "60") or 60), 300)
                print(f"  rate limited, waiting {wait}s")
                time.sleep(wait)
                r = web.get(SOURCE.format(token))
            time.sleep(5)  # be polite to the wiki
            if r.status_code != 200 or not r.content.startswith(b"\xff\xd8\xff"):
                print(f"{map_id} {name}: not available (HTTP {r.status_code})")
                failed += 1
                continue
            path.write_bytes(r.content)
        with Image.open(path) as img:
            w, h = img.size
            if abs(w / h - 1.5) > 0.02:  # map art is 3:2; anything else is not a bare map
                print(f"{map_id} {name}: unexpected size {w}x{h}, skipped")
                failed += 1
                continue
            out = io.BytesIO()
            img.convert("RGB").save(out, "JPEG", quality=90)
        state, detail = uploader.upload_map(
            map_id, name, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), out.getvalue())
        print(f"{map_id} {name}: {'uploaded' if state == 'ok' else 'UPLOAD FAILED ' + detail}")
        ok += state == "ok"
        failed += state != "ok"
    print(f"done: {ok} uploaded, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
