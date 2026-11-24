"""The setup page: get the sync program onto another computer.

Two kinds of download, neither holding any secret:
* the program as source (the addon, the Python package and one start file, the same for
  Windows and macOS), zipped on request from the files that came with the server;
* ready-made builds (an .exe, a zipped .app) that the owner made on a computer of that kind
  and uploaded here with the write token.

Each has a version, which the sync program asks for (GET /api/v1/setup) to tell its user when
a newer one is here. A build's version is given when it is uploaded and kept in a small file
beside it; the source's is read from the source itself.
"""
import io
import json
import os
import re
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response

HERE = os.path.dirname(os.path.abspath(__file__))
# Only these kinds of file ever go into the source zip: never a settings file with a token in it.
ALLOWED = (".py", ".pyw", ".lua", ".toc", ".txt", ".xml")
BUILD_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.(exe|zip|dmg|pkg|gz)$")
MAX_BUILD = 150 * 1024 * 1024
VERSION = re.compile(r"^\d+(\.\d+){0,3}$")


def sources(root: Optional[str] = None) -> Optional[Dict[str, str]]:
    """Where the program's files are: next to a deployed server, or in a checkout of the repository."""
    deployed = root or os.environ.get("AH_CLIENT_DIR") or os.path.join(HERE, "..", "client")
    repo = os.path.join(HERE, "..", "..")
    for found in ({"package": os.path.join(deployed, "adventuresync"), "addon": os.path.join(deployed, "addon"), "extras": deployed},
                  {"package": os.path.join(repo, "agent", "adventuresync"), "addon": os.path.join(repo, "addon"),
                   "extras": os.path.join(repo, "agent"), "readme": os.path.join(repo, "client", "README.txt")}):
        if os.path.isdir(found["package"]) and os.path.isdir(os.path.join(found["addon"], "AdventureCollector")):
            found.setdefault("readme", os.path.join(found["extras"], "README.txt"))
            return found
    return None


def source_version(src: Optional[Dict[str, str]]) -> Optional[str]:
    """The version written in the sync program's own source."""
    try:
        with open(os.path.join(src["package"], "__init__.py"), encoding="utf-8") as f:
            found = re.search(r'^__version__\s*=\s*"([^"]+)"', f.read(), re.M)
    except (OSError, TypeError):
        return None
    return found.group(1) if found and VERSION.match(found.group(1)) else None


def _add_tree(z: zipfile.ZipFile, folder: str, inside: str) -> None:
    for base, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith((".", "__")))
        for name in sorted(files):
            if name.endswith(ALLOWED) and not name.startswith("."):
                rel = os.path.relpath(os.path.join(base, name), folder).replace(os.sep, "/")
                _add(z, os.path.join(base, name), f"{inside}/{rel}")


def _add(z: zipfile.ZipFile, path: str, name: str) -> None:
    with open(path, "rb") as f:
        data = f.read()
    info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    z.writestr(info, data)


def bundle(src: Dict[str, str], whole: bool) -> bytes:
    """A zip of the addon alone, or of the whole program as source."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        if not whole:
            _add_tree(z, os.path.join(src["addon"], "AdventureCollector"), "AdventureCollector")
        else:
            top = "AdventureSync"
            _add_tree(z, src["package"], f"{top}/adventuresync")
            _add_tree(z, os.path.join(src["addon"], "AdventureCollector"), f"{top}/addon/AdventureCollector")
            for name, path in (("AdventureSync.pyw", os.path.join(src["extras"], "AdventureSync.pyw")),
                               ("requirements.txt", os.path.join(src["extras"], "requirements.txt")),
                               ("README.txt", src["readme"])):
                if os.path.isfile(path):
                    _add(z, path, f"{top}/{name}")
    return out.getvalue()


def register(app, can_write, builds_dir: str, client_dir: Optional[str] = None) -> None:
    os.makedirs(builds_dir, exist_ok=True)

    def about(name: str) -> str:
        """The small file beside a build that says which version it is."""
        return os.path.join(builds_dir, name + ".json")

    def version_of(name: str) -> Optional[str]:
        try:
            with open(about(name), encoding="utf-8") as f:
                version = json.load(f).get("version")
        except (OSError, ValueError, AttributeError):
            return None
        return version if isinstance(version, str) and VERSION.match(version) else None

    def builds() -> List[Dict[str, Any]]:
        out = []
        for name in sorted(os.listdir(builds_dir)):
            path = os.path.join(builds_dir, name)
            if BUILD_NAME.match(name) and os.path.isfile(path):
                st = os.stat(path)
                out.append({"name": name, "size": st.st_size, "version": version_of(name),
                            "updated": datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
        return out

    def zipped(whole: bool, filename: str) -> Response:
        src = sources(client_dir)
        if not src:
            raise HTTPException(404, "the program's files are not on this server")
        return Response(bundle(src, whole), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get("/setup", include_in_schema=False)
    def setup_page():
        return FileResponse(os.path.join(HERE, "setup.html"), media_type="text/html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/v1/setup")
    def setup_info():
        """What this server can hand out. No secrets."""
        src = sources(client_dir)
        return {"source": src is not None, "source_version": source_version(src), "builds": builds()}

    @app.get("/download/addon.zip", include_in_schema=False)
    def addon_zip():
        return zipped(False, "AdventureCollector.zip")

    @app.get("/download/client-source.zip", include_in_schema=False)
    def source_zip():
        return zipped(True, "AdventureSync-source.zip")

    @app.get("/download/builds/{name}", include_in_schema=False)
    def get_build(name: str):
        path = os.path.join(builds_dir, name)
        if not BUILD_NAME.match(name) or not os.path.isfile(path):
            raise HTTPException(404, "no such file")
        return FileResponse(path, media_type="application/octet-stream", filename=name)

    @app.put("/api/v1/builds/{name}", dependencies=[Depends(can_write)])
    async def put_build(name: str, request: Request, version: Optional[str] = None):
        """Upload a ready-made program for the setup page to offer. Written to disk as it arrives.
        ?version=1.2.3 says which version it is, so that older copies of it can offer the update."""
        if not BUILD_NAME.match(name):
            raise HTTPException(400, "name must be letters, digits, dots and dashes, ending in .exe, .zip, .dmg, .pkg or .gz")
        if version is not None and not VERSION.match(version):
            raise HTTPException(400, "version must be numbers separated by dots, like 1.2.3")
        tmp, size = os.path.join(builds_dir, f".{name}.part"), 0
        try:
            with open(tmp, "wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_BUILD:
                        raise HTTPException(413, "file too large")
                    f.write(chunk)
            if not size:
                raise HTTPException(400, "empty file")
            # The old version file goes first: a build is never listed with the version of the one before it.
            if os.path.exists(about(name)):
                os.remove(about(name))
            os.replace(tmp, os.path.join(builds_dir, name))
            if version:
                with open(about(name), "w", encoding="utf-8") as f:
                    json.dump({"version": version}, f)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        return {"success": True, "name": name, "size": size, "version": version}

    @app.delete("/api/v1/builds/{name}", dependencies=[Depends(can_write)])
    def delete_build(name: str):
        path = os.path.join(builds_dir, name)
        if not BUILD_NAME.match(name) or not os.path.isfile(path):
            raise HTTPException(404, "no such file")
        os.remove(path)
        if os.path.exists(about(name)):
            os.remove(about(name))
        return {"success": True}
