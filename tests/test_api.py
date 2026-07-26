"""HTTP surface: authorisation, lookup endpoints and the live socket.

The API layer had no tests at all, which is how an unauthenticated
``POST /api/transactions`` -- an endpoint that signs with the node's private
key -- survived in a tool meant to be run on a shared classroom network.
"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import DEFAULT_CONFIG, load_config
from app.runtime import NodeService
from app.web.api import _status_patch, create_web_app
from app.web.security import AdminGuard

_PORT = [17600]


@pytest.fixture()
def node(tmp_path: Path):
    _PORT[0] += 3
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "difficulty": 1,
            "auto_difficulty": False,
            "listen_port": _PORT[0],
            "web_port": _PORT[0] + 1,
            "servers": [],
            "coinbase_maturity": 0,
            "halving_interval": 10**6,
            "storage": {"type": "sqlite", "path": "./data/api.db"},
        }
    )
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    service = NodeService(load_config(path))
    app = create_web_app(service)
    with TestClient(app) as client:
        yield service, client, {"X-Admin-Token": service.admin_token}


def mine_one(service: NodeService, client: TestClient, headers: dict[str, str]) -> int:
    client.post("/api/mining/start", headers=headers)
    deadline = time.time() + 30
    while service.blockchain.height() < 1 and time.time() < deadline:
        time.sleep(0.05)
    client.post("/api/mining/stop", headers=headers)
    return service.blockchain.height()


# --------------------------------------------------------------------------
# authorisation
# --------------------------------------------------------------------------

WRITE_ENDPOINTS = [
    ("post", "/api/transactions", {"receiver": "ab" * 32, "amount": 1, "fee": 0}),
    ("post", "/api/mining/start", None),
    ("post", "/api/mining/stop", None),
    ("post", "/api/chain/reset", None),
    ("post", "/api/settings/difficulty", {"difficulty": 3}),
    ("post", "/api/wallet/generate", {"name": "x"}),
    ("post", "/api/peers", {"ip": "127.0.0.1", "port": 59999}),
    ("post", "/api/peers/ban", {"ip": "10.0.0.1", "port": 7464}),
    ("post", "/api/sync", None),
]


@pytest.mark.parametrize("method,path,payload", WRITE_ENDPOINTS)
def test_write_endpoints_reject_untrusted_callers(node, method, path, payload):
    _service, client, _headers = node
    response = getattr(client, method)(path, json=payload)
    assert response.status_code == 403, path


def test_wallet_export_requires_authorisation(node):
    """The private key must never be readable by an anonymous LAN caller."""
    _service, client, headers = node
    assert client.get("/api/wallet/export").status_code == 403
    allowed = client.get("/api/wallet/export", headers=headers)
    assert allowed.status_code == 200
    assert len(allowed.json()["private_key"]) == 64


@pytest.mark.parametrize("method,path,payload", WRITE_ENDPOINTS)
def test_write_endpoints_accept_the_admin_token(node, method, path, payload):
    _service, client, headers = node
    response = getattr(client, method)(path, json=payload, headers=headers)
    assert response.status_code != 403, path


def test_token_can_also_be_supplied_as_a_query_parameter(node):
    service, client, _headers = node
    response = client.post(f"/api/sync?token={service.admin_token}")
    assert response.status_code == 200


def test_wrong_token_is_rejected(node):
    _service, client, _headers = node
    response = client.post("/api/sync", headers={"X-Admin-Token": "not-the-token"})
    assert response.status_code == 403


def test_reads_stay_open_to_everyone(node):
    _service, client, _headers = node
    for path in (
        "/api/status",
        "/api/blocks",
        "/api/peers",
        "/api/classroom",
        "/api/security-events",
        "/api/mempool",
        "/api/stats",
        "/api/lab",
    ):
        assert client.get(path).status_code == 200, path


def test_session_endpoint_reports_authorisation(node):
    _service, client, headers = node
    assert client.get("/api/session").json()["is_admin"] is False
    assert client.get("/api/session", headers=headers).json()["is_admin"] is True


def test_guard_can_be_disabled_for_a_trusted_room():
    guard = AdminGuard({"require_admin_for_writes": False}, "token")

    class FakeRequest:
        client = None
        headers: dict[str, str] = {}
        query_params: dict[str, str] = {}

    assert guard.is_trusted(FakeRequest()) is True


# --------------------------------------------------------------------------
# lookup endpoints
# --------------------------------------------------------------------------


def test_transaction_and_proof_lookup(node):
    service, client, headers = node
    assert mine_one(service, client, headers) >= 1

    height = service.blockchain.height()
    block = client.get(f"/api/blocks/{height}").json()
    tx_id = block["transactions"][0]["tx_id"]

    detail = client.get(f"/api/transactions/{tx_id}")
    assert detail.status_code == 200
    assert detail.json()["state"] == "confirmed"
    assert detail.json()["confirmations"] >= 1

    proof = client.get(f"/api/proof/{tx_id}")
    assert proof.status_code == 200
    assert proof.json()["verified"] is True

    assert client.get("/api/transactions/deadbeef").status_code == 404
    assert client.get("/api/proof/deadbeef").status_code == 404


def test_search_finds_blocks_transactions_and_addresses(node):
    service, client, headers = node
    assert mine_one(service, client, headers) >= 1
    address = client.get("/api/status").json()["wallet"]["address"]
    height = service.blockchain.height()
    tx_id = client.get(f"/api/blocks/{height}").json()["transactions"][0]["tx_id"]

    assert client.get(f"/api/search?q={height}").json()["kind"] == "block"
    assert client.get(f"/api/search?q={tx_id}").json()["kind"] == "transaction"
    assert client.get(f"/api/search?q={address}").json()["kind"] == "address"
    assert client.get("/api/search?q=nothing-here").json()["kind"] == "not_found"


def test_history_reports_direction_and_confirmations(node):
    service, client, headers = node
    assert mine_one(service, client, headers) >= 1
    body = client.get("/api/transactions").json()
    assert body["transactions"]
    mined = body["transactions"][0]
    assert mined["direction"] == "mined"
    assert mined["confirmations"] >= 1
    assert body["summary"]["received"] > 0


def test_wallet_round_trip_through_export_and_import(node):
    _service, client, headers = node
    original = client.get("/api/wallet/export", headers=headers).json()

    client.post("/api/wallet/generate", json={"name": "second"}, headers=headers)
    assert len(client.get("/api/wallets").json()["wallets"]) == 2

    restored = client.post(
        "/api/wallet/import",
        json={"private_key": original["private_key"], "name": "restored"},
        headers=headers,
    )
    assert restored.status_code == 200
    assert restored.json()["address"] == original["address"]
    assert client.get("/api/wallet").json()["address"] == original["address"]


def test_import_rejects_a_malformed_key(node):
    _service, client, headers = node
    response = client.post(
        "/api/wallet/import", json={"private_key": "not-a-key"}, headers=headers
    )
    assert response.status_code == 400


def test_selecting_an_unknown_wallet_is_a_404(node):
    _service, client, headers = node
    response = client.post(
        "/api/wallet/select", json={"address": "ff" * 32}, headers=headers
    )
    assert response.status_code == 404


def test_ban_blocks_future_connections(node):
    """The security page used to only report attackers; now it can stop them."""
    service, client, headers = node
    client.post("/api/peers/ban", json={"ip": "10.11.12.13", "port": 7464}, headers=headers)
    assert service.store.is_peer_banned("10.11.12.13", 7464)

    accepted, message = asyncio.run(service.p2p.connect_peer("10.11.12.13", 7464))
    assert not accepted
    assert "banned" in message

    client.post("/api/peers/unban", json={"ip": "10.11.12.13", "port": 7464}, headers=headers)
    assert not service.store.is_peer_banned("10.11.12.13", 7464)


def test_lab_tasks_can_be_overridden_by_a_file(node, tmp_path: Path):
    service, client, _headers = node
    custom = [{"id": "only", "title": "自定义任务", "detail": "老师写的", "check": "has_block"}]
    (tmp_path / "lab_tasks.json").write_text(
        json.dumps(custom, ensure_ascii=False), encoding="utf-8"
    )
    body = client.get("/api/lab").json()
    assert body["total"] == 1
    assert body["tasks"][0]["title"] == "自定义任务"


# --------------------------------------------------------------------------
# live socket
# --------------------------------------------------------------------------


def test_status_patch_sends_only_what_changed():
    first = {"height": 1, "logs": ["a"], "mempool": {"count": 0}}
    assert _status_patch({}, first) == {"full": True, **first}

    assert _status_patch(first, first) == {}

    second = {"height": 2, "logs": ["a"], "mempool": {"count": 0}}
    patch = _status_patch(first, second)
    assert patch == {"full": False, "height": 2}
    assert "logs" not in patch


def test_websocket_streams_status(node):
    _service, client, _headers = node
    with client.websocket_connect("/ws/events") as socket:
        frame = socket.receive_json()
        assert frame["full"] is True
        assert "height" in frame
        assert "wallet" in frame
