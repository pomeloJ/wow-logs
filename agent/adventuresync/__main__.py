"""python -m adventuresync            opens the window (settings and sync status)
python -m adventuresync {run,status,test-connection,sync-once,validate,upload-map}   the same, without a window"""
import argparse
import logging
import sys
from pathlib import Path

from .core import (Queue, Uploader, crop_map, find_saved_variables, load_config,
                   read_stable, run_forever, scan)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="adventuresync")
    ap.add_argument("command", nargs="?", default="gui",
                    choices=["gui", "run", "status", "test-connection", "sync-once", "validate", "upload-map"])
    ap.add_argument("args", nargs="*", help="upload-map: <map_id> <image file> [name]")
    ap.add_argument("--config", default=None)
    args = ap.parse_args(argv)
    if args.command == "gui":
        from .gui import main as window
        return window(args.config)
    args.config = args.config or "config.toml"

    try:
        cfg = load_config(Path(args.config))
    except (OSError, ValueError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    queue = Queue(cfg.db_path)
    uploader = Uploader(cfg, queue)
    files = find_saved_variables(cfg.wow_path)

    if args.command == "validate":
        print(f"server url : {cfg.url}")
        print(f"device id  : {cfg.device_id}")
        print(f"queue db   : {Path(cfg.db_path).expanduser()}")
        if not files:
            print("SavedVariables: NOT FOUND (log in once with the addon enabled, then log out)")
            return 1
        for f in files:
            try:
                parsed = read_stable(f, settle=0.1)
                db = parsed.get("AdventureCollectorDB") or {}
                print(f"SavedVariables: {f}\n  events in file: {len(db.get('events') or [])}")
            except Exception as e:
                print(f"SavedVariables: {f}\n  PARSE ERROR: {e}")
                return 1
        return 0

    if args.command == "status":
        print(f"queue: {queue.counts()}")
        print(f"SavedVariables files: {[str(f) for f in files] or 'none found'}")
        return 0

    if args.command == "test-connection":
        try:
            info = uploader.test_connection()
        except Exception as e:
            print(f"connection FAILED: {e}")
            return 1
        print(f"server ok: {info['health']}")
        print("token ok" if info["token_ok"] else f"token REJECTED (HTTP {info['auth_status']})")
        return 0 if info["token_ok"] else 1

    if args.command == "upload-map":
        # Manually set a map background from any image that shows the whole map uncropped.
        if len(args.args) < 2:
            print("usage: upload-map <map_id> <image file> [name]", file=sys.stderr)
            return 2
        from datetime import datetime, timezone
        jpeg = crop_map(Path(args.args[1]).expanduser())
        state, detail = uploader.upload_map(
            int(args.args[0]), args.args[2] if len(args.args) > 2 else None,
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), jpeg)
        print("uploaded" if state == "ok" else f"FAILED: {detail}")
        return 0 if state == "ok" else 1

    if args.command == "sync-once":
        scan(cfg, queue, {}, uploader)
        res = uploader.sync_once()
        print(f"result: {res}\nqueue: {queue.counts()}")
        return 1 if res["error"] else 0

    try:
        run_forever(cfg, queue, uploader)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
