"""Map geometry from the game client's own tables, into an existing world database.

UiMap (every map: name, parent, kind) and UiMapAssignment (where each map lies in its
continent, in yards) are read from the CSV exports wago.tools offers for every client build;
for World of Warcraft: Forever the product is `wowf`. Only numbers and names are kept, in one
table (`world_uimaps`) that is replaced on every run, so the rest of the world database is
left alone and the import can be repeated whenever the game is patched.

World coordinates: X grows to the north, Y to the west, in yards; `instance_id` is the
continent (0 Eastern Kingdoms, 1 Kalimdor, ...). A map's picture runs from (x_max, y_max) at
its top-left corner to (x_min, y_min) at its bottom-right.

    python -m world_importer uimaps --build 1.60.1.71139 --out worlddata/world.db
    python -m world_importer uimaps --dir <folder with UiMap.csv, UiMapAssignment.csv> --out ...
"""
import csv
import io
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

URL = "https://wago.tools/db2/{table}/csv?build={build}"
SCHEMA = """
CREATE TABLE IF NOT EXISTS world_uimaps (
  ui_map_id INTEGER PRIMARY KEY, name TEXT, parent_ui_map_id INTEGER, type INTEGER,
  instance_id INTEGER, area_id INTEGER, x_min REAL, x_max REAL, y_min REAL, y_max REAL,
  build TEXT, source TEXT, imported_at TEXT);
"""


def rows_from(uimap_csv: str, assignment_csv: str) -> List[Tuple]:
    """One row per map; the rectangle is the assignment that covers the whole map picture
    (UiMin 0,0 to UiMax 1,1) with the lowest OrderIndex. Maps with none get no rectangle."""
    rects: Dict[int, Tuple] = {}
    for r in csv.DictReader(io.StringIO(assignment_csv)):
        if (float(r["UiMin_0"]), float(r["UiMin_1"]), float(r["UiMax_0"]), float(r["UiMax_1"])) != (0, 0, 1, 1):
            continue
        uid, order = int(r["UiMapID"]), int(r["OrderIndex"])
        if uid in rects and rects[uid][0] <= order:
            continue
        x_min, y_min, x_max, y_max = (float(r[k]) for k in ("Region_0", "Region_1", "Region_3", "Region_4"))
        if x_max <= x_min or y_max <= y_min:
            continue
        rects[uid] = (order, int(r["MapID"]), int(r["AreaID"]) or None, x_min, x_max, y_min, y_max)
    out = []
    for r in csv.DictReader(io.StringIO(uimap_csv)):
        uid = int(r["ID"])
        rect = rects.get(uid)
        out.append((uid, r["Name_lang"], int(r["ParentUiMapID"]) or None, int(r["Type"]),
                    *(rect[1:] if rect else (None,) * 6)))
    return out


def fetch(build: str) -> Tuple[str, str]:
    import httpx
    with httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": "world-importer"}) as web:
        got = []
        for table in ("UiMap", "UiMapAssignment"):
            r = web.get(URL.format(table=table, build=build))
            r.raise_for_status()
            got.append(r.text)
    return got[0], got[1]


def import_into(path: str, uimap_csv: str, assignment_csv: str, build: Optional[str], source: str) -> Dict[str, int]:
    rows = rows_from(uimap_csv, assignment_csv)
    if not rows:
        raise ValueError("no maps in the files")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.executescript(SCHEMA)
            conn.execute("DELETE FROM world_uimaps")
            conn.executemany("INSERT INTO world_uimaps VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             [(*r, build, source, now) for r in rows])
    finally:
        conn.close()
    return {"maps": len(rows), "with_rect": sum(r[6] is not None for r in rows)}


def run(out: Path, build: Optional[str], folder: Optional[Path]) -> Dict[str, int]:
    if not out.is_file():
        raise FileNotFoundError(f"no world database at {out}: import one first")
    if folder:
        texts = ((folder / "UiMap.csv").read_text(encoding="utf-8-sig"),
                 (folder / "UiMapAssignment.csv").read_text(encoding="utf-8-sig"))
        source = "wago.tools CSV export (local files)"
    else:
        texts = fetch(build)
        source = "wago.tools CSV export"
    return import_into(str(out), *texts, build, source)
