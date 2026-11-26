"""CLI:
    python -m world_importer import --source norgannondb --path <dir> [--out worlddata/world.db]
    python -m world_importer uimaps (--build <client build> | --dir <csv folder>) [--out worlddata/world.db]

`import` builds a fresh world database file. It is written to a temporary name and swapped in
only when the whole import succeeded, so a failed run leaves the old file intact.
`uimaps` adds (or replaces) the map geometry table in an existing world database: see uimaps.py.
"""
import argparse
import os
import sys
from pathlib import Path

from . import create
from . import uimaps
from .norgannon import import_into

SOURCES = {"norgannondb": import_into}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="world_importer")
    ap.add_argument("command", choices=["import", "uimaps"])
    ap.add_argument("--source", choices=sorted(SOURCES))
    ap.add_argument("--path", help="folder holding the downloaded source data")
    ap.add_argument("--build", help="uimaps: the client build to fetch from wago.tools, e.g. 1.60.1.71139")
    ap.add_argument("--dir", help="uimaps: a folder with UiMap.csv and UiMapAssignment.csv instead of fetching")
    ap.add_argument("--out", default="worlddata/world.db")
    args = ap.parse_args(argv)
    out = Path(args.out)
    if args.command == "uimaps":
        if not args.build and not args.dir:
            print("uimaps needs --build or --dir", file=sys.stderr)
            return 2
        try:
            counts = uimaps.run(out, args.build, Path(args.dir) if args.dir else None)
        except Exception as e:
            print(f"uimaps FAILED, database left as it was: {e}", file=sys.stderr)
            return 1
        print(f"map geometry imported into {out}: {counts}")
        return 0
    if not args.source or not args.path:
        print("import needs --source and --path", file=sys.stderr)
        return 2
    src = Path(args.path)
    if not src.is_dir():
        print(f"not a folder: {src}", file=sys.stderr)
        return 2
    tmp = out.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    conn = create(str(tmp))
    failed = None
    try:
        counts = SOURCES[args.source](conn, src)
    except Exception as e:
        failed = str(e)
    if failed is not None:
        # Outside the except block on purpose: while the exception is alive its traceback keeps
        # the importer's cursor, and with it the file, open. Windows cannot delete an open file.
        conn.close()
        tmp.unlink(missing_ok=True)
        print(f"import FAILED, existing database left untouched: {failed}", file=sys.stderr)
        return 1
    conn.execute("VACUUM")
    conn.close()
    os.replace(tmp, out)
    print(f"imported into {out} ({out.stat().st_size / 1e6:.1f} MB): {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
