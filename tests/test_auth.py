from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from nff_diagnosis.auth.store import AgentStore
from nff_diagnosis.server.app import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def agent_store(tmp_path: Path) -> AgentStore:
    return AgentStore(tmp_path / "agents.db")


@pytest.fixture()
def app_client(tmp_path: Path):
    with patch("nff_diagnosis.engine.diagnosis.anthropic.Anthropic", MagicMock(return_value=MagicMock())):
        app = create_app(
            store_root=tmp_path / "store",
            db_path=tmp_path / "test.db",
            api_key="fake",
        )
        with TestClient(app) as c:
            yield app, c


def _make_human_identity(owner_id: str = "user-123"):
    """Patch _resolve so the Bearer token is treated as a human identity."""
    from nff_diagnosis.auth.models import Identity

    async def fake_resolve(auth, store):
        if auth and auth.startswith("Bearer human-"):
            uid = auth[len("Bearer human-"):]
            return Identity(type="human", owner_id=uid)
        return None

    return patch("nff_diagnosis.auth.dependencies._resolve", side_effect=fake_resolve)


# ---------------------------------------------------------------------------
# AgentStore unit tests
# ---------------------------------------------------------------------------


def test_store_create_and_get(agent_store: AgentStore):
    a = agent_store.create(
        owner_id="u1", name="Test", slug="test-agent",
        description=None, avatar_url=None, parent_agent_id=None,
    )
    assert a.slug == "test-agent"
    assert a.is_active is True

    fetched = agent_store.get_by_id(a.id)
    assert fetched is not None
    assert fetched.name == "Test"


def test_store_get_by_slug(agent_store: AgentStore):
    agent_store.create(
        owner_id="u1", name="Slug Test", slug="slug-test",
        description=None, avatar_url=None, parent_agent_id=None,
    )
    assert agent_store.get_by_slug("slug-test") is not None
    assert agent_store.get_by_slug("does-not-exist") is None


def test_store_list_by_owner(agent_store: AgentStore):
    agent_store.create(owner_id="u1", name="A", slug="a", description=None, avatar_url=None, parent_agent_id=None)
    agent_store.create(owner_id="u1", name="B", slug="b", description=None, avatar_url=None, parent_agent_id=None)
    agent_store.create(owner_id="u2", name="C", slug="c", description=None, avatar_url=None, parent_agent_id=None)

    u1_agents = agent_store.list_by_owner("u1")
    assert len(u1_agents) == 2
    assert all(a.owner_id == "u1" for a in u1_agents)


def test_store_deactivate(agent_store: AgentStore):
    a = agent_store.create(owner_id="u1", name="D", slug="d", description=None, avatar_url=None, parent_agent_id=None)
    assert agent_store.deactivate(a.id, "u1") is True
    assert agent_store.get_by_slug("d") is None  # is_active filter applied
    assert agent_store.deactivate(a.id, "u1") is False  # already inactive → 0 rows


def test_store_deactivate_wrong_owner(agent_store: AgentStore):
    a = agent_store.create(owner_id="u1", name="E", slug="e", description=None, avatar_url=None, parent_agent_id=None)
    assert agent_store.deactivate(a.id, "wrong") is False


def test_store_upsert_key(agent_store: AgentStore):
    a = agent_store.create(owner_id="u1", name="F", slug="f", description=None, avatar_url=None, parent_agent_id=None)
    assert a.key_hash is None

    agent_store.upsert_key(a.id, "abc123", "nff_abc1")
    updated = agent_store.get_by_id(a.id)
    assert updated is not None
    assert updated.key_hash == "abc123"
    assert updated.key_prefix == "nff_abc1"


def test_store_get_by_key_hash(agent_store: AgentStore):
    a = agent_store.create(owner_id="u1", name="G", slug="g", description=None, avatar_url=None, parent_agent_id=None)
    agent_store.upsert_key(a.id, "deadbeef", "nff_dead")

    found = agent_store.get_by_key_hash("deadbeef")
    assert found is not None
    assert found.id == a.id


def test_store_count_sub_agents(agent_store: AgentStore):
    parent = agent_store.create(owner_id="u1", name="Parent", slug="parent", description=None, avatar_url=None, parent_agent_id=None)
    for i in range(3):
        agent_store.create(owner_id="u1", name=f"Child {i}", slug=f"child-{i}", description=None, avatar_url=None, parent_agent_id=parent.id)

    assert agent_store.count_active_sub_agents(parent.id) == 3


def test_store_deactivate_all_for_owner(agent_store: AgentStore):
    agent_store.create(owner_id="u1", name="H", slug="h", description=None, avatar_url=None, parent_agent_id=None)
    agent_store.create(owner_id="u1", name="I", slug="i", description=None, avatar_url=None, parent_agent_id=None)
    agent_store.deactivate_all_for_owner("u1")
    assert agent_store.list_by_owner("u1") == []


# ---------------------------------------------------------------------------
# GET /api/agents
# ---------------------------------------------------------------------------


def test_get_agents_by_slug_public(app_client):
    app, client = app_client
    store: AgentStore = app.state.agent_store
    store.create(owner_id="u1", name="Public", slug="public-agent", description="desc", avatar_url=None, parent_agent_id=None)

    resp = client.get("/api/agents?slug=public-agent")
    assert resp.status_code == 200
    data = resp.json()
    assert data["slug"] == "public-agent"
    assert "key_hash" not in data


def test_get_agents_by_slug_not_found(app_client):
    _, client = app_client
    assert client.get("/api/agents?slug=ghost").status_code == 404


def test_get_agents_list_unauthorized(app_client):
    _, client = app_client
    assert client.get("/api/agents").status_code == 401


def test_get_agents_list_authenticated(app_client):
    app, client = app_client
    store: AgentStore = app.state.agent_store
    store.create(owner_id="user-abc", name="My Agent", slug="my-agent", description=None, avatar_url=None, parent_agent_id=None)

    with _make_human_identity():
        resp = client.get("/api/agents", headers={"Authorization": "Bearer human-user-abc"})
    assert resp.status_code == 200
    agents = resp.json()
    assert len(agents) == 1
    assert agents[0]["slug"] == "my-agent"


# ---------------------------------------------------------------------------
# POST /api/agents
# ---------------------------------------------------------------------------


def test_create_agent_human(app_client):
    _, client = app_client
    with _make_human_identity():
        resp = client.post(
            "/api/agents",
            json={"name": "New Agent"},
            headers={"Authorization": "Bearer human-user-xyz"},
        )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "New Agent"
    assert data["slug"] == "new-agent"


def test_create_agent_explicit_slug(app_client):
    _, client = app_client
    with _make_human_identity():
        resp = client.post(
            "/api/agents",
            json={"name": "Explicit", "slug": "my-slug"},
            headers={"Authorization": "Bearer human-u1"},
        )
    assert resp.status_code == 201
    assert resp.json()["slug"] == "my-slug"


def test_create_agent_invalid_slug(app_client):
    _, client = app_client
    with _make_human_identity():
        resp = client.post(
            "/api/agents",
            json={"name": "Bad", "slug": "BAD SLUG!!"},
            headers={"Authorization": "Bearer human-u1"},
        )
    assert resp.status_code == 400


def test_create_agent_unauthenticated(app_client):
    _, client = app_client
    assert client.post("/api/agents", json={"name": "X"}).status_code == 401


def test_create_agent_slug_collision_auto_increment(app_client):
    _, client = app_client
    with _make_human_identity():
        h = {"Authorization": "Bearer human-u1"}
        client.post("/api/agents", json={"name": "Dupe"}, headers=h)
        resp2 = client.post("/api/agents", json={"name": "Dupe"}, headers=h)
    assert resp2.status_code == 201
    assert resp2.json()["slug"] == "dupe-1"


# ---------------------------------------------------------------------------
# DELETE /api/agents
# ---------------------------------------------------------------------------


def test_delete_agent(app_client):
    app, client = app_client
    store: AgentStore = app.state.agent_store
    a = store.create(owner_id="user-del", name="Del", slug="del", description=None, avatar_url=None, parent_agent_id=None)

    with _make_human_identity():
        resp = client.request("DELETE", f"/api/agents?id={a.id}", headers={"Authorization": "Bearer human-user-del"})
    assert resp.status_code == 204
    assert store.get_by_slug("del") is None


def test_delete_agent_not_found(app_client):
    _, client = app_client
    with _make_human_identity():
        resp = client.request("DELETE", "/api/agents?id=ghost-id", headers={"Authorization": "Bearer human-u1"})
    assert resp.status_code == 404


def test_delete_agent_agent_caller_rejected(app_client):
    from nff_diagnosis.auth.models import Identity

    app, client = app_client
    store: AgentStore = app.state.agent_store
    a = store.create(owner_id="u1", name="X", slug="x", description=None, avatar_url=None, parent_agent_id=None)

    async def agent_identity(auth, store_):
        return Identity(type="agent", owner_id="u1", agent_id=a.id)

    with patch("nff_diagnosis.auth.dependencies._resolve", side_effect=agent_identity):
        resp = client.request("DELETE", f"/api/agents?id={a.id}", headers={"Authorization": "Bearer nff_fake"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/agent-key
# ---------------------------------------------------------------------------


def test_rotate_agent_key(app_client):
    app, client = app_client
    store: AgentStore = app.state.agent_store
    a = store.create(owner_id="user-key", name="KeyAgent", slug="key-agent", description=None, avatar_url=None, parent_agent_id=None)

    with _make_human_identity():
        resp = client.post(
            "/api/agent-key",
            json={"agent_id": a.id},
            headers={"Authorization": "Bearer human-user-key"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["key"].startswith("nff_")
    assert len(data["key"]) == 52  # "nff_" + 48 hex chars
    assert data["key_prefix"] == data["key"][:12]

    updated = store.get_by_id(a.id)
    assert updated is not None
    expected_hash = hashlib.sha256(data["key"].encode()).hexdigest()
    assert updated.key_hash == expected_hash


def test_rotate_key_rotates(app_client):
    app, client = app_client
    store: AgentStore = app.state.agent_store
    a = store.create(owner_id="user-rot", name="Rot", slug="rot", description=None, avatar_url=None, parent_agent_id=None)

    with _make_human_identity():
        h = {"Authorization": "Bearer human-user-rot"}
        key1 = client.post("/api/agent-key", json={"agent_id": a.id}, headers=h).json()["key"]
        key2 = client.post("/api/agent-key", json={"agent_id": a.id}, headers=h).json()["key"]
    assert key1 != key2

    # Old key no longer resolves
    old_hash = hashlib.sha256(key1.encode()).hexdigest()
    assert store.get_by_key_hash(old_hash) is None


def test_rotate_key_wrong_owner(app_client):
    app, client = app_client
    store: AgentStore = app.state.agent_store
    a = store.create(owner_id="u-owner", name="Own", slug="own", description=None, avatar_url=None, parent_agent_id=None)

    with _make_human_identity():
        resp = client.post(
            "/api/agent-key",
            json={"agent_id": a.id},
            headers={"Authorization": "Bearer human-other-user"},
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Agent key auth path (nff_ token -> identity)
# ---------------------------------------------------------------------------


def test_agent_key_resolves_identity(app_client):
    import secrets as _secrets

    app, client = app_client
    store: AgentStore = app.state.agent_store

    a = store.create(owner_id="u1", name="Listed", slug="listed", description=None, avatar_url=None, parent_agent_id=None)
    raw_key = "nff_" + _secrets.token_hex(24)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    store.upsert_key(a.id, key_hash, raw_key[:12])

    # The agent should be able to list agents owned by u1
    resp = client.get("/api/agents", headers={"Authorization": f"Bearer {raw_key}"})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /api/delete-account
# ---------------------------------------------------------------------------


def test_delete_account_deactivates_agents(app_client):
    app, client = app_client
    store: AgentStore = app.state.agent_store
    store.create(owner_id="user-gone", name="Gone", slug="gone", description=None, avatar_url=None, parent_agent_id=None)

    with _make_human_identity():
        resp = client.post("/api/delete-account", headers={"Authorization": "Bearer human-user-gone"})
    assert resp.status_code == 204
    assert store.list_by_owner("user-gone") == []


def test_delete_account_calls_supabase_when_configured(app_client):
    _, client = app_client

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with _make_human_identity(), \
         patch.dict("os.environ", {"SUPABASE_URL": "https://fake.supabase.co", "SUPABASE_SERVICE_ROLE_KEY": "svc-key"}), \
         patch("nff_diagnosis.auth.router.httpx.AsyncClient") as mock_client_cls:
        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_http.delete = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_http

        resp = client.post("/api/delete-account", headers={"Authorization": "Bearer human-owner-1"})
    assert resp.status_code == 204
    mock_http.delete.assert_called_once()


def test_delete_account_agent_rejected(app_client):
    from nff_diagnosis.auth.models import Identity

    _, client = app_client

    async def agent_identity(auth, store_):
        return Identity(type="agent", owner_id="u1", agent_id="some-id")

    with patch("nff_diagnosis.auth.dependencies._resolve", side_effect=agent_identity):
        resp = client.post("/api/delete-account", headers={"Authorization": "Bearer nff_fake"})
    assert resp.status_code == 401
