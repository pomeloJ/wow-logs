"""CLI: python -m world_importer import --source norgannondb --path <dir> [--out worlddata/world.db]

Builds a fresh world database file. It is written to a temporary name and swapped in
only when the whole import succeeded, so a failed run leaves the old file intact.
"""
import argparse
import os
import sys
from pathlib import Path

from . import create
from .norgannon import import_into

SOURCES = {"norgannondb": import_into}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="world_importer")
    ap.add_argument("command", choices=["import"])
    ap.add_argument("--source", required=True, choices=sorted(SOURCES))
    ap.add_argument("--path", required=True, help="folder holding the downloaded source data")
    ap.add_argument("--out", default="worlddata/world.db")
    args = ap.parse_args(argv)
    src, out = Path(args.path), Path(args.out)
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
