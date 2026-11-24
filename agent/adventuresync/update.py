"""Is there a newer version of this program on the server, and how to get it.

The server's setup page lists what it can hand out (GET /api/v1/setup, no token needed): the
ready-made programs its owner uploaded, each with a version, and the version of the source.
Nothing is installed: a ready-made program is one file, so updating is downloading the new
file next to the old one and opening it.
"""
import io
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from . import __version__

USER_AGENT = "AdventureSync/0.1"                    # the same name the uploads use (see core.USER_AGENT)
# What a ready-made program may be called. The name comes from the server and ends up in a
# file path and an address here, so anything else (a folder in it, say) is not taken.
BUILD_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.(exe|zip|dmg|pkg|gz)")


def as_numbers(version: Any) -> Tuple[int, ...]:
    """'0.10.2' -> (0, 10, 2), so that 0.10 counts as newer than 0.9. () when it is not a version."""
    text = str(version or "").strip().lstrip("v")
    return tuple(int(n) for n in text.split(".")) if re.fullmatch(r"\d+(\.\d+){0,3}", text) else ()


def packed() -> bool:
    """Running as a ready-made one-file program, not from source with Python."""
    return bool(getattr(sys, "frozen", False))


def build_prefix() -> Optional[str]:
    """How the ready-made program for this computer is named, as scripts/build-client.py names it."""
    system = {"Windows": "windows", "Darwin": "macos"}.get(platform.system())
    arch = {"AMD64": "x64", "x86_64": "x64", "arm64": "arm64", "ARM64": "arm64"}.get(platform.machine(), platform.machine().lower())
    return f"AdventureSync-{system}-{arch}" if system else None


def check(url: str, client: Optional[Any] = None, current: str = __version__) -> Optional[Dict[str, Any]]:
    """What the server offers that is newer than this program, or None.

    -> {"version", "name", "url", "size", "kind"}; kind is "build" for a ready-made program for
    this computer and "source" for the zip of source files. Raises when the server cannot be asked."""
    base = url.rstrip("/")
    if client is None:
        r = httpx.get(f"{base}/api/v1/setup", timeout=10, follow_redirects=True, headers={"User-Agent": USER_AGENT})
    else:
        r = client.get("/api/v1/setup")
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    try:
        offered = r.json()
        builds = offered.get("builds") or []
    except (ValueError, AttributeError):
        raise RuntimeError("the answer did not come from the Adventure Hub server")
    mine = as_numbers(current)
    if packed():
        prefix = build_prefix()
        newer = [b for b in builds if isinstance(b, dict) and prefix and str(b.get("name", "")).startswith(prefix)
                 and BUILD_NAME.fullmatch(str(b["name"])) and as_numbers(b.get("version")) > mine]
        if not newer:
            return None
        best = max(newer, key=lambda b: as_numbers(b["version"]))
        return {"version": best["version"], "name": best["name"], "size": best.get("size"), "kind": "build",
                "url": f"{base}/download/builds/{best['name']}"}
    if as_numbers(offered.get("source_version")) > mine:
        return {"version": offered["source_version"], "name": "AdventureSync-source.zip", "size": None, "kind": "source",
                "url": f"{base}/download/client-source.zip"}
    return None


def can_fetch(found: Dict[str, Any]) -> bool:
    """Whether this program can download and open the new version by itself. That is so for the
    one-file Windows program; anything else is handed to the browser to download."""
    return packed() and sys.platform.startswith("win") and found.get("kind") == "build"


def target(found: Dict[str, Any], folder: Optional[Path] = None) -> Path:
    """Where the new program goes: beside the running one, with its version in the name."""
    folder = folder or Path(sys.executable).resolve().parent
    if not BUILD_NAME.fullmatch(str(found["name"])) or not as_numbers(found.get("version")):
        raise RuntimeError("the server named the new version in a way this program does not accept")
    stem, dot, ext = str(found["name"]).rpartition(".")
    return folder / f"{stem}-{found['version']}{dot}{ext}"


def fetch(found: Dict[str, Any], folder: Optional[Path] = None,
          progress: Optional[Callable[[int, Optional[int]], None]] = None, client: Optional[Any] = None) -> Path:
    """Download the new program. It is written under a temporary name and given its real one
    only when all of it arrived, so a broken download is never mistaken for the program."""
    dest = target(found, folder)
    size = found.get("size")
    if dest.is_file() and size and dest.stat().st_size == size:
        return dest                                   # already fetched earlier
    part = dest.with_name(dest.name + ".part")
    got = 0
    try:
        with (client or httpx).stream("GET", found["url"], **({} if client else {
                "timeout": 60, "follow_redirects": True, "headers": {"User-Agent": USER_AGENT}})) as r:
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            with open(part, "wb") as f:
                for chunk in r.iter_bytes(1 << 16):
                    f.write(chunk)
                    got += len(chunk)
                    if progress:
                        progress(got, size)
        if not got or (size and got != size):
            raise RuntimeError(f"the download is incomplete ({got} of {size} bytes)")
        os.replace(part, dest)
    finally:
        if part.exists():
            part.unlink()
    return dest


def launch(path: Path) -> None:
    """Open the downloaded program as a process of its own, which goes on when this one closes."""
    env = dict(os.environ)
    # A one-file program unpacks itself and tells its children where. The new program is not a
    # child in that sense: it has to unpack its own files, not use this (older) program's.
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    for name in ("_MEIPASS2", "_PYI_ARCHIVE_FILE", "_PYI_APPLICATION_HOME_DIR", "_PYI_PARENT_PROCESS_LEVEL"):
        env.pop(name, None)
    flags = 0
    if sys.platform.startswith("win"):
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(path)], cwd=os.getcwd(), env=env, close_fds=True, creationflags=flags)


# ------------------------------------------------------------ the addon in the game
# The addon changes more often than this program and is a handful of text files, so it can be
# brought up to date by itself: fetched from the server and put into the game's AddOns folder.
ADDON = "AdventureCollector"
ADDON_FILES = (".lua", ".toc", ".xml", ".txt")
ADDON_MAX = 5 * 1024 * 1024


def toc_version(folder: Path) -> Optional[str]:
    """The version an installed copy of the addon says it is, or None."""
    try:
        text = (folder / f"{ADDON}.toc").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    found = re.search(r"^##\s*Version:\s*(\S+)", text, re.M)
    return found.group(1) if found and as_numbers(found.group(1)) else None


def installed_addons(addon_dirs: List[Path]) -> List[Dict[str, Any]]:
    """Where the addon is installed (one entry per game version) and which version each is."""
    return [{"folder": str(d), "version": toc_version(d / ADDON)} for d in addon_dirs if (d / ADDON).is_dir()]


def check_addon(url: str, addon_dirs: List[Path], client: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    """The server's addon, when it is newer than a copy installed here, or None. Nothing is
    offered where the addon is not installed at all: that is what "install" is for."""
    base = url.rstrip("/")
    if client is None:
        r = httpx.get(f"{base}/api/v1/setup", timeout=10, follow_redirects=True, headers={"User-Agent": USER_AGENT})
    else:
        r = client.get("/api/v1/setup")
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    try:
        offered = r.json().get("addon_version")
    except (ValueError, AttributeError):
        raise RuntimeError("the answer did not come from the Adventure Hub server")
    if not as_numbers(offered):
        return None
    old = [a for a in installed_addons(addon_dirs) if as_numbers(a["version"]) < as_numbers(offered)]
    if not old:
        return None
    return {"version": offered, "installed": min((a["version"] or "?" for a in old), key=as_numbers),
            "folders": [a["folder"] for a in old], "url": f"{base}/download/addon.zip"}


def addon_files(data: bytes) -> Dict[str, bytes]:
    """The files of a downloaded addon zip, by their path inside the addon's folder. Only what
    an addon is made of is taken, and nothing that would land outside its folder."""
    files: Dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        total = 0
        for info in z.infolist():
            name = info.filename
            if name.endswith("/"):
                continue
            parts = name.split("/")
            if parts[0] != ADDON or len(parts) < 2 or "\\" in name or any(p in ("", ".", "..") or ":" in p for p in parts) \
                    or not name.lower().endswith(ADDON_FILES):
                raise RuntimeError(f"the addon download holds something unexpected: {name}")
            total += info.file_size
            if total > ADDON_MAX:
                raise RuntimeError("the addon download is too large to be the addon")
            files["/".join(parts[1:])] = z.read(info)
    if f"{ADDON}.toc" not in files or "Core.lua" not in files:
        raise RuntimeError("the addon download is not the addon")
    return files


def fetch_addon(found: Dict[str, Any], client: Optional[Any] = None) -> List[Path]:
    """Download the addon and replace each out-of-date copy. The new files are written beside
    the old folder first and swapped in whole, so a failure leaves the old addon as it was."""
    if client is None:
        r = httpx.get(found["url"], timeout=60, follow_redirects=True, headers={"User-Agent": USER_AGENT})
    else:
        r = client.get("/download/addon.zip")
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    if len(r.content) > ADDON_MAX:
        raise RuntimeError("the addon download is too large to be the addon")
    try:
        files = addon_files(r.content)
    except zipfile.BadZipFile:
        raise RuntimeError("the addon download is not a zip file")
    done = []
    for folder in found["folders"]:
        addons = Path(folder)
        fresh, dest = addons / f".{ADDON}.new", addons / ADDON
        if fresh.exists():
            shutil.rmtree(fresh)
        for rel, data in files.items():
            path = fresh.joinpath(*rel.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        if dest.exists():
            shutil.rmtree(dest)
        fresh.rename(dest)
        done.append(dest)
    return done
