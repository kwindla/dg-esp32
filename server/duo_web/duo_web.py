"""Browser stand-in for the EchoEar duo device, for testing the demo bot.

Serves a page that speaks the device's wire behaviour (duo hello/toggle,
liveness pong) and proxies Pipecat signalling to the bot. Everything lives
under a secret path prefix so a public tunnel does not expose the bot.

    DUO_WEB_SECRET=... uv run python duo_web/duo_web.py [--port 7881] [--bot http://127.0.0.1:7861]
"""

import argparse
import os
from pathlib import Path

import aiohttp
from aiohttp import web

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
CHARACTERS = HERE.parent.parent / "assets" / "characters"
HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade", "host",
       "content-length", "content-encoding", "proxy-authorization", "te", "trailer"}


def make_app(secret, bot):
    prefix = f"/{secret}"
    app = web.Application(client_max_size=4 * 1024 * 1024)

    async def session_ctx(app):
        app["http"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        yield
        await app["http"].close()

    app.cleanup_ctx.append(session_ctx)

    async def index(request):
        return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    async def slash(request):
        raise web.HTTPFound(prefix + "/")

    async def character(request):
        name, rest = request.match_info["name"], request.match_info["path"]
        if name not in ("angel", "devil"):
            raise web.HTTPNotFound()
        path = (CHARACTERS / name / rest).resolve()
        if not path.is_relative_to(CHARACTERS / name) or path.suffix not in (".png", ".json") or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def proxy(request):
        tail = request.match_info["tail"]
        if not (tail == "start" or tail.startswith("sessions/") or tail.startswith("api/offer")):
            raise web.HTTPNotFound()
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
        async with request.app["http"].request(request.method, f"{bot}/{tail}", headers=headers,
                                               params=request.query, data=await request.read()) as r:
            body = await r.read()
            out = {k: v for k, v in r.headers.items() if k.lower() not in HOP}
            return web.Response(status=r.status, body=body, headers=out)

    app.router.add_get(prefix, slash)
    app.router.add_get(prefix + "/", index)
    app.router.add_static(prefix + "/static", STATIC)
    app.router.add_get(prefix + "/characters/{name}/{path:.+}", character)
    app.router.add_route("*", prefix + "/bot/{tail:.+}", proxy)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7881)
    parser.add_argument("--bot", default="http://127.0.0.1:7861")
    args = parser.parse_args()
    secret = os.environ.get("DUO_WEB_SECRET", "")
    if len(secret) < 16 or not secret.isalnum():
        raise SystemExit("DUO_WEB_SECRET must be at least 16 alphanumeric characters")
    web.run_app(make_app(secret, args.bot.rstrip("/")), host=args.host, port=args.port,
                access_log=None)


if __name__ == "__main__":
    main()
