"""Loopback paper HTTP API. It does not import the live trader."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from paper.engine import PaperEngine

STATIC = Path(__file__).resolve().parent / "static"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}
LEGACY_ROUTES = (
    "/api/buy",
    "/api/sell",
    "/api/mode",
    "/api/config",
    "/api/run",
    "/api/unmonitor",
    "/api/chain",
    "/api/settings",
    "/api/settings/reset",
    "/api/wallet",
    "/api/positions",
)
SECRET_KEYS = {
    "api_key",
    "private_key",
    "signing_key",
    "secret",
    "password",
    "gmgn_api_key",
    "gmgn_private_key",
    "wallet_balance",
    "wallet_private_key",
}


def create_app(state_path: str | Path) -> FastAPI:
    app = FastAPI(title="Paper-only simulator", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = PaperEngine(state_path)

    @app.middleware("http")
    async def paper_guard(request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0].lower()
        if host not in LOOPBACK_HOSTS:
            return _paper(JSONResponse({"ok": False, "error": "loopback_only",
                                        "message": "paper runtime accepts 127.0.0.1 and localhost only"}, status_code=403))
        response = await call_next(request)
        response.headers["X-Paper-Only"] = "1"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/paper/status")
    def status():
        return _paper(app.state.engine.status())

    @app.get("/api/paper/candidates")
    def candidates():
        return _paper(app.state.engine.candidates())

    @app.get("/api/paper/ledger")
    def ledger():
        return _paper(app.state.engine.ledger())

    @app.post("/api/paper/preset/example")
    async def preset(request: Request):
        refused = await _refuse_secrets(request)
        if refused is not None:
            return refused
        return _paper(app.state.engine.apply_example_preset())

    @app.post("/api/paper/policies")
    async def policies(request: Request):
        body, refused = await _json_or_refuse(request)
        if refused is not None:
            return refused
        return _paper(app.state.engine.configure(body))

    @app.post("/api/paper/start")
    async def start(request: Request):
        refused = await _refuse_secrets(request)
        if refused is not None:
            return refused
        return _paper(app.state.engine.start())

    @app.post("/api/paper/pause")
    async def pause(request: Request):
        refused = await _refuse_secrets(request)
        if refused is not None:
            return refused
        return _paper(app.state.engine.pause())

    @app.post("/api/paper/kill")
    async def kill(request: Request):
        body, refused = await _json_or_refuse(request)
        if refused is not None:
            return refused
        return _paper(app.state.engine.kill(str(body.get("confirm") or "")))

    @app.post("/api/paper/reset")
    async def reset(request: Request):
        body, refused = await _json_or_refuse(request)
        if refused is not None:
            return refused
        return _paper(app.state.engine.reset(str(body.get("confirm") or "")))

    @app.post("/api/paper/entries")
    async def entries(request: Request):
        body, refused = await _json_or_refuse(request)
        if refused is not None:
            return refused
        return _paper(app.state.engine.open_position(str(body.get("token_id") or ""), str(body.get("idempotency_key") or "")))

    @app.post("/api/paper/step")
    async def step(request: Request):
        body, refused = await _json_or_refuse(request)
        if refused is not None:
            return refused
        return _paper(app.state.engine.step(body.get("seconds"), str(body.get("idempotency_key") or "")))

    @app.post("/api/paper/exit")
    async def exit_pos(request: Request):
        body, refused = await _json_or_refuse(request)
        if refused is not None:
            return refused
        return _paper(app.state.engine.request_exit(str(body.get("position_id") or ""), str(body.get("idempotency_key") or "")))

    @app.post("/api/paper/resolve")
    async def resolve(request: Request):
        body, refused = await _json_or_refuse(request)
        if refused is not None:
            return refused
        return _paper(app.state.engine.resolve(
            str(body.get("position_id") or ""),
            str(body.get("outcome") or ""),
            str(body.get("idempotency_key") or ""),
        ))

    def legacy():
        return _paper(JSONResponse({
            "ok": False,
            "error": "live_route_removed",
            "message": "This paper runtime has no live buy, sell, mode, or key route.",
            "live_orders": False,
        }, status_code=410))

    for path in LEGACY_ROUTES:
        app.add_api_route(path, legacy, methods=["GET", "POST", "PUT", "DELETE"])

    return app


def _paper(payload):
    if isinstance(payload, JSONResponse):
        payload.headers["X-Paper-Only"] = "1"
        payload.headers["Cache-Control"] = "no-store"
        return payload
    http = 200
    body = payload
    if isinstance(payload, dict) and "http" in payload:
        http = int(payload["http"])
        body = {key: value for key, value in payload.items() if key not in {"http", "remember", "mutated"}}
    response = JSONResponse(body, status_code=http)
    response.headers["X-Paper-Only"] = "1"
    response.headers["Cache-Control"] = "no-store"
    return response


def _secret_key(value) -> bool:
    return isinstance(value, str) and value.lower() in SECRET_KEYS


def _contains_secret(value) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if _secret_key(key) or _contains_secret(item):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_secret(item) for item in value)
    if isinstance(value, str) and "BEGIN PRIVATE KEY" in value:
        return True
    return False


async def _json_or_refuse(request: Request):
    refused = await _refuse_secrets(request)
    if refused is not None:
        return None, refused
    raw = await request.body()
    if not raw:
        return {}, None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, _paper(JSONResponse({"ok": False, "error": "bad_json", "message": "body must be JSON"}, status_code=400))
    if not isinstance(data, dict):
        return None, _paper(JSONResponse({"ok": False, "error": "bad_json", "message": "body must be an object"}, status_code=400))
    return data, None


async def _refuse_secrets(request: Request):
    raw = await request.body()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if _contains_secret(data):
        return _paper(JSONResponse({
            "ok": False,
            "error": "credentials_refused",
            "message": "paper runtime does not accept API keys, signing keys, or wallet secrets",
            "live_orders": False,
        }, status_code=400))
    return None
