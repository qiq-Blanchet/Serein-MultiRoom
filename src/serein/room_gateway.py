"""M2: fail-closed, statically authenticated multi-room Chat / MCP gateway.

The public gateway reads ONLY a periodically replaced credential-routing manifest,
not memory databases or room secret directories. OAuth and admin endpoints are
intentionally out of scope until independently audience-bound (M5 / M3).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

# The M2 public interface is deliberately narrow. Never proxy the Serein
# management API until M3 administrator authentication is implemented.
CHAT_PATHS = frozenset({
    "/v1/models", "/v1/chat/completions",
    "/api/hook/recall", "/v1/host/deliveries",
})
MCP_PATHS = frozenset({"/mcp", "/mcp/", "/serein/mcp", "/serein/mcp/"})
ALLOWED_METHODS = frozenset({"GET", "POST", "DELETE"})
HOP_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "proxy-connection",
})
# Never let the caller override the gateway's internal identity or audience.
UNTRUSTED_HEADERS = frozenset({
    "authorization", "host", "x-forwarded-for", "x-forwarded-host",
    "x-forwarded-proto", "x-forwarded-port", "x-real-ip",
    "x-serein-room-id", "x-serein-internal-token", "x-serein-gateway-room",
})


def _json_error(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status,
                        headers={"Cache-Control": "no-store"})


def _verified_route(path: Path, key: str):
    """Identify a ready room solely by server-provisioned key digest.

    The atomic routes file contains ONLY internal tokens (not DB contents),
    and is replaced on every transition into/out of ready state.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != 1 or not isinstance(data.get("rooms"), list):
            return None
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        for row in data["rooms"]:
            if not isinstance(row, dict):
                continue
            if not secrets.compare_digest(str(row.get("gateway_key_sha256", "")), digest):
                continue
            room_id = row.get("room_id")
            token = row.get("internal_token")
            if (not isinstance(room_id, str) or len(room_id) != 32
                    or any(char not in "0123456789abcdef" for char in room_id)
                    or not isinstance(token, str) or len(token) < 32):
                return None
            return {"room_id": room_id, "internal_token": token}
    except (OSError, ValueError, TypeError):
        return None
    return None


def create_app(routes_file: Path, *, transport=None) -> FastAPI:
    """Create a gateway; injected HTTP transport is for offline tests only."""
    routes_file = Path(routes_file)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    def health():
        # No room names, IDs or registry metadata exposed.
        return {"status": "ok" if routes_file.is_file() else "unavailable"}

    @app.api_route("/{route_path:path}", methods=list(ALLOWED_METHODS))
    async def proxy(route_path: str, request: Request):
        path = request.scope.get("path", "")
        if path not in CHAT_PATHS and path not in MCP_PATHS:
            return _json_error(404, "Unknown public route")
        if request.method not in ALLOWED_METHODS:
            return _json_error(405, "Method not allowed")
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer ") or not authorization[7:]:
            return _json_error(401, "Bearer room key required")
        key = authorization[7:]
        # Ignore all caller-supplied room routing hints for routing itself.
        route = _verified_route(routes_file, key)
        if not route:
            return _json_error(401, "Invalid room key or inactive room")
        requested_room = request.headers.get("x-serein-room-id")
        if requested_room and requested_room != route["room_id"]:
            return _json_error(403, "Room identity mismatch")

        # MCP browser callers require same-origin. OAuth is NOT transparently
        # proxied, because room-specific resource/audience is not yet in place.
        if path in MCP_PATHS and request.headers.get("origin"):
            origin = request.headers["origin"]
            try:
                parsed = urlsplit(origin)
                origin_ok = parsed.scheme in ("http", "https") and (
                    parsed.netloc.lower() == request.headers.get("host", "").lower()
                )
            except ValueError:
                origin_ok = False
            if not origin_ok:
                return _json_error(403, "Invalid MCP origin")

        target = f"http://room-{route['room_id']}:8011"
        upstream_url = target + path
        if request.scope.get("query_string"):
            upstream_url += "?" + request.scope["query_string"].decode("latin-1")
        headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in HOP_HEADERS | UNTRUSTED_HEADERS
        }
        headers["Authorization"] = "Bearer " + route["internal_token"]
        headers["Host"] = f"room-{route['room_id']}:8011"
        if path in MCP_PATHS:
            # Trusted gateway owns these values; user cannot spoof them.
            headers["X-Forwarded-Host"] = request.headers.get("host", "")
            headers["X-Forwarded-Proto"] = request.url.scheme

        client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=300, write=120, pool=10),
            follow_redirects=False, transport=transport,
        )
        try:
            content = request.stream()
            if path == "/v1/chat/completions":
                # Explicit room hints are checked, never used as authentication.
                # This endpoint is JSON and the underlying Serein API also
                # materializes its full request body.
                original_body = await request.body()
                if len(original_body) > 64 * 1024 * 1024:
                    await client.aclose()
                    return _json_error(413, "Chat request exceeds gateway limit")
                try:
                    body = json.loads(original_body)
                except ValueError:
                    await client.aclose()
                    return _json_error(400, "Invalid chat JSON")
                if isinstance(body, dict):
                    hints = [body.get("room_id")]
                    opts = body.get("serein")
                    if isinstance(opts, dict):
                        hints.append(opts.get("room_id"))
                    if any(hint is not None and hint != route["room_id"]
                           for hint in hints):
                        await client.aclose()
                        return _json_error(403, "Room identity mismatch")
                content = original_body
            upstream_request = client.build_request(
                request.method, upstream_url, headers=headers, content=content)
            upstream = await client.send(upstream_request, stream=True)
        except httpx.HTTPError:
            await client.aclose()
            return _json_error(502, "Room service unavailable")

        response_headers = {
            key: value for key, value in upstream.headers.items()
            if key.lower() not in HOP_HEADERS
        }
        response_headers["Cache-Control"] = "no-store"

        async def body():
            try:
                async for part in upstream.aiter_raw():
                    yield part
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            body(), status_code=upstream.status_code, headers=response_headers,
        )

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the authenticated M2 room gateway")
    parser.add_argument("--routes-file", type=Path, default=Path(os.environ.get(
        "SEREIN_MULTIROOM_ROUTES_FILE", "/control/routes.json")))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    arguments = parser.parse_args(argv)
    if not arguments.routes_file.is_file():
        parser.error("Route manifest does not exist; create a room first")
    import uvicorn
    uvicorn.run(create_app(arguments.routes_file), host=arguments.host, port=arguments.port)


if __name__ == "__main__":
    main()
