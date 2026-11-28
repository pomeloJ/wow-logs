"""Step-by-step guides with pictures: how to hook ChatGPT up to the voice API.

The pictures are screenshots with everything personal taken out. The ChatGPT guide needs the
address of the MCP server that sits between ChatGPT and this one (a Cloudflare worker): it is
a setting of the server (AH_MCP_URL), never written into the code, so the public repository
holds no address of anybody's. The page asks for it at /api/v1/voice/mcp.
"""
import os
import re

from fastapi import HTTPException
from fastapi.responses import FileResponse

HERE = os.path.dirname(os.path.abspath(__file__))
PICTURE = re.compile(r"^[a-z0-9-]{1,40}\.webp$")


def register(app, mcp_url=None) -> None:
    url = (mcp_url if mcp_url is not None else os.environ.get("AH_MCP_URL", "")).strip() or None

    @app.get("/guide/chatgpt", include_in_schema=False)
    def chatgpt_guide():
        return FileResponse(os.path.join(HERE, "guide.html"), media_type="text/html", headers={"Cache-Control": "no-cache"})

    @app.get("/guide/img/{name}", include_in_schema=False)
    def guide_picture(name: str):
        path = os.path.join(HERE, "guide", name)
        if not PICTURE.match(name) or not os.path.isfile(path):
            raise HTTPException(404, "no such picture")
        return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/api/v1/voice/mcp")
    def voice_mcp():
        """Where ChatGPT (or another MCP client) connects to read a character with a voice token:
        `url` plus `?token=<voice token>`. `url` is null when this server has not been told the
        address of its MCP server. See /guide/chatgpt for the steps."""
        return {"url": url, "guide": "/guide/chatgpt"}
