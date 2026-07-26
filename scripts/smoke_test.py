#!/usr/bin/env python3
"""Boot a node, mine one block, send one transaction, shut down.

Runs in CI as an end-to-end check that the pieces fit together: config
loading, SQLite schema and migrations, wallet creation, the P2P listener, the
HTTP surface, the admin guard, mining, and the Merkle proof endpoint.

Unit tests exercise these separately; this catches the wiring.
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import DEFAULT_CONFIG, load_config  # noqa: E402
from app.runtime import NodeService  # noqa: E402
from app.web.api import create_web_app  # noqa: E402

MINING_TIMEOUT_SECONDS = 90


def fail(message: str) -> None:
    print(f"FAIL  {message}")
    sys.exit(1)


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"ok    {label}")
    else:
        fail(f"{label} {detail}".strip())


def main() -> None:
    with tempfile.TemporaryDirectory() as workdir:
        config = copy.deepcopy(DEFAULT_CONFIG)
        config.update(
            {
                "node_name": "smoke",
                "difficulty": 4,
                "auto_difficulty": False,
                "listen_ip": "127.0.0.1",
                "enable_ipv6": False,
                "listen_port": 17999,
                "web_port": 18999,
                "servers": [],
                "coinbase_maturity": 0,
                "halving_interval": 4,
                "storage": {"type": "sqlite", "path": "./data/smoke.db"},
            }
        )
        config_path = Path(workdir) / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")

        service = NodeService(load_config(config_path))
        app = create_web_app(service)
        headers = {"X-Admin-Token": service.admin_token}

        with TestClient(app) as client:
            status = client.get("/api/status")
            check("status endpoint responds", status.status_code == 200)
            body = status.json()
            check("wallet was created", bool(body["wallet"]["address"]))
            check("genesis is present", body["height"] == 0)

            check(
                "unauthenticated writes are refused",
                client.post("/api/chain/reset").status_code == 403,
            )

            started = client.post("/api/mining/start", headers=headers)
            check("mining starts", started.status_code == 200)

            deadline = time.time() + MINING_TIMEOUT_SECONDS
            while (
                service.blockchain.height() < int(config["halving_interval"])
                and time.time() < deadline
            ):
                time.sleep(0.2)
            client.post("/api/mining/stop", headers=headers)
            height = service.blockchain.height()
            check("blocks were mined", height >= 2, f"(height {height})")

            block = client.get(f"/api/blocks/{height}").json()
            tx_id = block["transactions"][0]["tx_id"]
            proof = client.get(f"/api/proof/{tx_id}")
            check("merkle proof verifies", proof.status_code == 200 and proof.json()["verified"])

            supply = client.get("/api/status").json()["supply"]
            check(
                "halving schedule is applied",
                supply["next_block_subsidy"] < DEFAULT_CONFIG["mining_reward"],
                f"(subsidy {supply['next_block_subsidy']} at height {height})",
            )

            sent = client.post(
                "/api/transactions",
                json={"receiver": "ab" * 32, "amount": 1.0, "fee": 0.01},
                headers=headers,
            )
            check("transaction is accepted", sent.status_code == 200, sent.text[:200])
            check("mempool holds it", service.mempool.stats()["count"] == 1)

            history = client.get("/api/transactions").json()
            check("history lists the mined coinbase", history["total"] >= 2)

            check("stats endpoint responds", client.get("/api/stats").status_code == 200)
            check("search finds the block", client.get(f"/api/search?q={height}").json()["kind"] == "block")

    print("\nsmoke test passed")


if __name__ == "__main__":
    main()
