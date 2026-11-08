import json
import pathlib
import sys

from fastapi.testclient import TestClient

from paper.app import create_app
from paper.limits import KILL_PHRASE


ROOT = pathlib.Path(__file__).resolve().parents[1]
PAPER = ROOT / "paper"


def test_paper_sources_do_not_reference_live_execution():
    forbidden = (
        "subprocess",
        "gmgn-cli",
        "GMGN_API_KEY",
        "GMGN_PRIVATE_KEY",
        "load_env",
        "do_buy",
        "do_sell",
        "LiveGMGN",
        "os.environ",
        "1.18",
        "import app",
        "from app import",
        "urllib",
        "requests",
    )
    for path in PAPER.rglob("*.py"):
        text = path.read_text()
        for token in forbidden:
            assert token not in text, f"{path.name} contains {token}"


def test_importing_paper_does_not_load_live_app():
    import paper.app  # noqa: F401
    assert "app" not in sys.modules
    assert "def do_buy" in (ROOT / "app.py").read_text()


def test_engine_does_not_read_home_or_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    cfg = home / ".config" / "gmgn"
    cfg.mkdir(parents=True)
    (cfg / ".env").write_text("GMGN_API_KEY=sentinel-secret-value\nGMGN_PRIVATE_KEY=sentinel-private\n")
    monkeypatch.setenv("GMGN_API_KEY", "sentinel-secret-value")
    monkeypatch.setenv("GMGN_PRIVATE_KEY", "sentinel-private")
    monkeypatch.setattr(pathlib.Path, "home", lambda: home)
    from paper.engine import PaperEngine
    eng = PaperEngine(tmp_path / "state.json")
    eng.apply_example_preset()
    blob = json.dumps(eng.status()) + (tmp_path / "state.json").read_text()
    assert "sentinel-secret-value" not in blob
    assert "sentinel-private" not in blob
    assert "1.18" not in blob


def test_legacy_routes_and_secrets_and_loopback(tmp_path):
    app = create_app(tmp_path / "state.json")
    client = TestClient(app, base_url="http://127.0.0.1")
    for path in ("/api/buy", "/api/sell", "/api/mode", "/api/config", "/api/run", "/api/wallet"):
        response = client.post(path, json={"address": "x", "size_sol": 1})
        assert response.status_code == 410
        assert response.json()["live_orders"] is False
        assert response.headers["X-Paper-Only"] == "1"
    refused = client.post("/api/paper/policies", json={"api_key": "abc", "signing_key": "-----BEGIN PRIVATE KEY-----\nabc"})
    assert refused.status_code == 400
    assert refused.json()["error"] == "credentials_refused"
    assert "BEGIN PRIVATE KEY" not in (tmp_path / "state.json").read_text() if (tmp_path / "state.json").exists() else True
    evil = TestClient(app, base_url="http://evil.example")
    assert evil.get("/api/paper/status").status_code == 403
    page = client.get("/")
    assert page.status_code == 200
    assert "PAPER ONLY" in page.text
    assert "not an executable GMGN quote" in page.text
    assert "20% gross" in page.text
    assert "api_key" not in page.text.lower()
    status = client.get("/api/paper/status").json()
    assert status["live_trading_possible"] is False
    assert status["wallet_balance_read"] is False
    assert status["run"] == "stopped"
    assert "policies_not_configured" in status["blockers"]
    killed = client.post("/api/paper/kill", json={"confirm": KILL_PHRASE})
    assert killed.status_code == 200
    assert client.post("/api/paper/start", json={}).status_code == 409
