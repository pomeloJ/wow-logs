#!/usr/bin/env python3
"""Build the sync program into one file for the system this runs on, and optionally put it
on your server's setup page.

Run it on the kind of computer the program is for: Windows makes AdventureSync-windows-x64.exe,
macOS makes AdventureSync-macos-<arch>.zip (the app, zipped). A build cannot be made for
another system.

    python scripts/build-client.py                # build into dist/
    python scripts/build-client.py --upload       # build, then upload using agent/config.toml
    python scripts/build-client.py --upload --url https://my.host --token ...

Needs PyInstaller in the Python that runs this:  python -m pip install pyinstaller httpx pillow
"""
import argparse
import platform
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def build() -> Path:
    system = {"Windows": "windows", "Darwin": "macos"}.get(platform.system())
    if not system:
        sys.exit("builds are made on Windows or macOS")
    arch = {"AMD64": "x64", "x86_64": "x64", "arm64": "arm64", "ARM64": "arm64"}.get(platform.machine(), platform.machine().lower())
    sep = ";" if system == "windows" else ":"
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--onefile",
                    "--name", "AdventureSync", "--paths", str(ROOT / "agent"),
                    "--add-data", f"{ROOT / 'addon' / 'AdventureCollector'}{sep}addon/AdventureCollector",
                    "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"), "--specpath", str(ROOT / "build"),
                    str(ROOT / "agent" / "AdventureSync.pyw")], check=True)
    if system == "windows":
        out = ROOT / "dist" / f"AdventureSync-windows-{arch}.exe"
        shutil.move(ROOT / "dist" / "AdventureSync.exe", out)
    else:                                            # the .app is a folder: hand it out zipped
        out = Path(shutil.make_archive(str(ROOT / "dist" / f"AdventureSync-macos-{arch}"), "zip",
                                       ROOT / "dist", "AdventureSync.app"))
    print(f"built {out} ({out.stat().st_size / 1048576:.1f} MB)")
    return out


def upload(path: Path, url: str, token: str) -> None:
    import httpx
    with open(path, "rb") as f:
        r = httpx.put(f"{url.rstrip('/')}/api/v1/builds/{path.name}", content=f, timeout=600,
                      headers={"Authorization": f"Bearer {token}", "User-Agent": "AdventureSync/0.1"})
    if r.status_code != 200:
        sys.exit(f"upload failed: HTTP {r.status_code} {r.text[:200]}")
    print(f"uploaded: {url.rstrip('/')}/setup now offers {path.name}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--upload", action="store_true", help="put the build on the server's setup page")
    ap.add_argument("--url", help="server address (default: from agent/config.toml)")
    ap.add_argument("--token", help="write token (default: from agent/config.toml)")
    args = ap.parse_args()
    out = build()
    if args.upload:
        url, token = args.url, args.token
        if not (url and token):
            with open(ROOT / "agent" / "config.toml", "rb") as f:
                server = tomllib.load(f)["server"]
            url, token = url or server["url"], token or server["api_token"]
        upload(out, url, token)


if __name__ == "__main__":
    main()
