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
import re
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
    assets = ROOT / "agent" / "adventuresync" / "assets"
    # Windows wants an .ico; on macOS PyInstaller turns the PNG into the app's icon itself (with Pillow).
    icon = assets / ("icon.ico" if system == "windows" else "icon.png")
    extra = ["--version-file", str(version_file(f"AdventureSync-windows-{arch}.exe"))] if system == "windows" else []
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--onefile", *extra,
                    "--name", "AdventureSync", "--paths", str(ROOT / "agent"), "--icon", str(icon),
                    "--add-data", f"{ROOT / 'addon' / 'AdventureCollector'}{sep}addon/AdventureCollector",
                    "--add-data", f"{assets}{sep}adventuresync/assets",
                    "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"), "--specpath", str(ROOT / "build"),
                    str(ROOT / "agent" / "AdventureSync.pyw")], check=True)
    if system == "windows":
        out = ROOT / "dist" / f"AdventureSync-windows-{arch}.exe"
        shutil.move(ROOT / "dist" / "AdventureSync.exe", out)
    else:                                            # the .app is a folder: hand it out zipped
        out = Path(shutil.make_archive(str(ROOT / "dist" / f"AdventureSync-macos-{arch}"), "zip",
                                       ROOT / "dist", "AdventureSync.app"))
    print(f"built {out} (version {version()}, {out.stat().st_size / 1048576:.1f} MB)")
    return out


PUBLISHER = "Adventure Hub"
PRODUCT = "AdventureSync"


def version_file(filename: str) -> Path:
    """The details Windows shows under the program's Properties > Details (publisher, product,
    version). A program without them looks more suspicious to virus scanners."""
    numbers = tuple(int(n) for n in re.findall(r"\d+", version()))[:4]
    numbers += (0,) * (4 - len(numbers))
    strings = {"CompanyName": PUBLISHER, "FileDescription": "AdventureSync - sync program for Adventure Hub",
               "FileVersion": version(), "InternalName": PRODUCT, "LegalCopyright": f"(c) {PUBLISHER}",
               "OriginalFilename": filename, "ProductName": PRODUCT, "ProductVersion": version()}
    table = ",\n".join(f"            StringStruct({k!r}, {v!r})" for k, v in strings.items())
    path = ROOT / "build" / "version-info.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers}, prodvers={numbers}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
{table}
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [0x0409, 1200])])
  ]
)
""", encoding="utf-8")
    return path


def version() -> str:
    """The version written in the program's source: uploaded with the build, so that copies
    already handed out can tell their users a newer one is on the server."""
    source = (ROOT / "agent" / "adventuresync" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__\s*=\s*"([^"]+)"', source).group(1)


def upload(path: Path, url: str, token: str) -> None:
    import httpx
    with open(path, "rb") as f:
        r = httpx.put(f"{url.rstrip('/')}/api/v1/builds/{path.name}", content=f, timeout=600, params={"version": version()},
                      headers={"Authorization": f"Bearer {token}", "User-Agent": "AdventureSync/0.1"})
    if r.status_code != 200:
        sys.exit(f"upload failed: HTTP {r.status_code} {r.text[:200]}")
    print(f"uploaded: {url.rstrip('/')}/setup now offers {path.name} (version {version()})")


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
