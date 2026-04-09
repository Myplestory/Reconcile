"""Tests for reconcile.web — Quart app, REST API, SSE."""

import pytest
from reconcile.orchestrator import Orchestrator, TeamConfig
from reconcile.web.app import create_app


@pytest.fixture
def app():
    orch = Orchestrator(db_path="/tmp/test_web.db")
    orch.add_team(TeamConfig(team_id="t1", team_name="Alpha"))
    return create_app(orch)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.mark.asyncio
async def test_health(client):
    res = await client.get("/api/health")
    assert res.status_code == 200
    data = await res.get_json()
    assert data["status"] == "ok"
    assert data["teams"] == 1


@pytest.mark.asyncio
async def test_list_teams(client):
    res = await client.get("/api/teams")
    assert res.status_code == 200
    data = await res.get_json()
    assert len(data) == 1
    assert data[0]["team_id"] == "t1"
    assert data[0]["name"] == "Alpha"


@pytest.mark.asyncio
async def test_team_detail(client):
    res = await client.get("/api/teams/t1")
    assert res.status_code == 200
    data = await res.get_json()
    assert data["team_id"] == "t1"


@pytest.mark.asyncio
async def test_team_not_found(client):
    res = await client.get("/api/teams/nonexistent")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_add_team(client):
    res = await client.post("/api/teams", json={"team_id": "t2", "team_name": "Beta"})
    assert res.status_code == 201
    data = await res.get_json()
    assert data["team_id"] == "t2"

    # Verify it shows up in list
    res = await client.get("/api/teams")
    data = await res.get_json()
    assert any(t["team_id"] == "t2" for t in data)


@pytest.mark.asyncio
async def test_add_team_missing_id(client):
    res = await client.post("/api/teams", json={"team_name": "NoID"})
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_remove_team(client):
    res = await client.delete("/api/teams/t1")
    assert res.status_code == 200

    res = await client.get("/api/teams/t1")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_remove_nonexistent(client):
    res = await client.delete("/api/teams/ghost")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_sweep(client):
    res = await client.post("/api/teams/t1/sweep")
    assert res.status_code == 200
    data = await res.get_json()
    assert data["status"] in ("complete", "started")


@pytest.mark.asyncio
async def test_github_webhook_no_secret(client):
    res = await client.post("/hooks/github", json={
        "repository": {"name": "test-repo"},
        "sender": {"login": "alice"},
        "ref": "refs/heads/main",
    }, headers={"X-GitHub-Event": "push"})
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_github_webhook_bad_signature():
    orch = Orchestrator(db_path="/tmp/test_web2.db")
    app = create_app(orch, github_webhook_secret="mysecret")
    client = app.test_client()
    res = await client.post("/hooks/github", json={"test": True},
                            headers={"X-Hub-Signature-256": "sha256=bad"})
    assert res.status_code == 403


def test_sse_module_imports():
    """SSE helpers should import cleanly with no hardcoded refs."""
    import inspect
    from reconcile.web.sse import alert_stream, metrics_stream
    source = inspect.getsource(alert_stream) + inspect.getsource(metrics_stream)
    for pattern in ["buffalo", "pmtool", "webdev", "shallow", "1470"]:
        assert pattern not in source.lower()


@pytest.mark.asyncio
async def test_get_config(client):
    res = await client.get("/api/teams/t1/config")
    assert res.status_code == 200
    data = await res.get_json()
    assert "sweep_on_alert" in data
    assert "detectors" in data
    assert isinstance(data["detectors"], dict)


@pytest.mark.asyncio
async def test_get_config_not_found(client):
    res = await client.get("/api/teams/ghost/config")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_patch_config_sweep(client):
    res = await client.patch("/api/teams/t1/config", json={"sweep_debounce": 60.0})
    assert res.status_code == 200
    # Verify change applied
    res = await client.get("/api/teams/t1/config")
    data = await res.get_json()
    assert data["sweep_debounce"] == 60.0


@pytest.mark.asyncio
async def test_patch_config_detector_threshold(client):
    # First check what detectors exist
    res = await client.get("/api/teams/t1/config")
    data = await res.get_json()
    if "batch-completion" in data["detectors"]:
        res = await client.patch("/api/teams/t1/config", json={
            "detectors": {"batch-completion": {"min_cards": 10}}
        })
        assert res.status_code == 200
        res = await client.get("/api/teams/t1/config")
        data = await res.get_json()
        assert data["detectors"]["batch-completion"]["min_cards"] == 10


@pytest.mark.asyncio
async def test_teams_list_includes_alert_count(client):
    res = await client.get("/api/teams")
    data = await res.get_json()
    assert "alert_count" in data[0]


@pytest.mark.asyncio
async def test_no_hardcoded_urls():
    """Verify app.py source contains no hardcoded instance URLs."""
    import inspect
    source = inspect.getsource(create_app)
    for pattern in ["buffalo", "pmtool", "webdev", "shallow", "s26", "1470"]:
        assert pattern not in source.lower(), f"Found hardcoded reference: {pattern}"
