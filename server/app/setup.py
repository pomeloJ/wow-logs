"""The setup page: get the sync program onto another computer.

Two kinds of download, neither holding any secret:
* the program as source (the addon, the Python package and one start file, the same for
  Windows and macOS), zipped on request from the files that came with the server;
* ready-made builds (an .exe, a zipped .app) that the owner made on a computer of that kind
  and uploaded here with the write token.
"""
import io
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

    def builds() -> List[Dict[str, Any]]:
        out = []
        for name in sorted(os.listdir(builds_dir)):
            path = os.path.join(builds_dir, name)
            if BUILD_NAME.match(name) and os.path.isfile(path):
                st = os.stat(path)
                out.append({"name": name, "size": st.st_size,
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
        return {"source": sources(client_dir) is not None, "builds": builds()}

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
    async def put_build(name: str, request: Request):
        """Upload a ready-made program for the setup page to offer. Written to disk as it arrives."""
        if not BUILD_NAME.match(name):
            raise HTTPException(400, "name must be letters, digits, dots and dashes, ending in .exe, .zip, .dmg, .pkg or .gz")
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
            os.replace(tmp, os.path.join(builds_dir, name))
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        return {"success": True, "name": name, "size": size}

    @app.delete("/api/v1/builds/{name}", dependencies=[Depends(can_write)])
    def delete_build(name: str):
        path = os.path.join(builds_dir, name)
        if not BUILD_NAME.match(name) or not os.path.isfile(path):
            raise HTTPException(404, "no such file")
        os.remove(path)
        return {"success": True}
