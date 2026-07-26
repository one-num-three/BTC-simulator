#!/usr/bin/env python3
"""Boot a node the way a user does, mine, transact, shut down.

This deliberately starts ``main.py`` as a real subprocess and talks to it over
HTTP with nothing but the standard library. It therefore covers the actual
entrypoint -- argument parsing, config loading, SQLite schema and migrations,
wallet creation, the uvicorn server, the P2P listener, the lifespan handler and
the startup banner -- none of which an in-process test client exercises.

It also means the smoke test has no test-only dependencies. An earlier version
used ``fastapi.testclient``, which needs ``httpx``; that made CI fail on a
machine that had installed only ``requirements.txt``, and it was testing the
test harness rather than the thing users run.

Unit tests cover the pieces. This catches the wiring.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import DEFAULT_CONFIG  # noqa: E402

WEB_PORT = 18999
P2P_PORT = 17999
BOOT_TIMEOUT_SECONDS = 60
MINING_TIMEOUT_SECONDS = 120

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    if condition:
        print(f"ok    {label}")
    else:
        print(f"FAIL  {label} {detail}".rstrip())
        failures.append(label)
    return bool(condition)


def request(
    path: str,
    method: str = "GET",
    payload: dict | None = None,
    token: str | None = None,
) -> tuple[int, dict]:
    """Minimal HTTP client so the smoke test needs no third-party packages."""
    url = f"http://127.0.0.1:{WEB_PORT}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Admin-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            body = response.read().decode()
            return response.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        try:
            return exc.code, json.loads(body) if body else {}
        except json.JSONDecodeError:
            return exc.code, {"detail": body[:200]}


def wait_for_boot() -> bool:
    deadline = time.time() + BOOT_TIMEOUT_SECONDS
    while time.time() < deadline:
        try:
            status, _ = request("/api/status")
            if status == 200:
                return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.3)
    return False


def main() -> None:
    with tempfile.TemporaryDirectory() as workdir:
        work = Path(workdir)
        config = copy.deepcopy(DEFAULT_CONFIG)
        config.update(
            {
                "node_name": "smoke",
                "difficulty": 4,
                "auto_difficulty": False,
                "listen_ip": "127.0.0.1",
                "enable_ipv6": False,
                "web_host": "127.0.0.1",
                "listen_port": P2P_PORT,
                "web_port": WEB_PORT,
                "servers": [],
                "coinbase_maturity": 0,
                "halving_interval": 4,
                # Loopback is trusted by default, which is what lets a student
                # drive their own console without a token. That also means a
                # same-machine smoke test would sail through every guarded
                # endpoint and prove nothing, so turn it off here to exercise
                # the token path over a real socket.
                "trust_loopback_admin": False,
                "storage": {"type": "sqlite", "path": str(work / "data" / "smoke.db")},
            }
        )
        config_path = work / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")

        process = subprocess.Popen(
            [sys.executable, "main.py", "--config", str(config_path)],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            if not check("node boots", wait_for_boot()):
                process.terminate()
                print(process.communicate(timeout=10)[0][-2000:])
                sys.exit(1)

            token_path = work / "data" / "admin_token"
            check("admin token file was created", token_path.exists())
            token = token_path.read_text(encoding="utf-8").strip() if token_path.exists() else ""
            check("admin token is non-empty", bool(token))

            _status, body = request("/api/status")
            check("wallet was created", bool(body.get("wallet", {}).get("address")))
            check("genesis is present", body.get("height") == 0)

            denied, _ = request("/api/chain/reset", method="POST", payload={})
            check("unauthenticated writes are refused", denied == 403, f"(got {denied})")

            wrong, _ = request(
                "/api/chain/reset", method="POST", payload={}, token="not-the-token"
            )
            check("a wrong token is refused", wrong == 403, f"(got {wrong})")

            code, _ = request("/api/status")
            check("reads stay open without a token", code == 200, f"(got {code})")

            started, _ = request("/api/mining/start", method="POST", payload={}, token=token)
            check("mining starts", started == 200, f"(got {started})")

            deadline = time.time() + MINING_TIMEOUT_SECONDS
            target_height = int(config["halving_interval"])
            height = 0
            while time.time() < deadline:
                _status, body = request("/api/status")
                height = int(body.get("height", 0))
                if height >= target_height:
                    break
                time.sleep(0.5)
            request("/api/mining/stop", method="POST", payload={}, token=token)
            check("blocks were mined", height >= 2, f"(height {height})")

            _status, block = request(f"/api/blocks/{height}")
            tx_id = block["transactions"][0]["tx_id"]

            code, proof = request(f"/api/proof/{tx_id}")
            check("merkle proof verifies", code == 200 and proof.get("verified") is True)

            code, header = request(f"/api/blocks/{height}/header")
            check(
                "real 80-byte header renders",
                code == 200 and header.get("size") == 80 and len(header.get("hex", "")) == 160,
            )

            _status, body = request("/api/status")
            supply = body.get("supply", {})
            check(
                "halving schedule is applied",
                0 < supply.get("next_block_subsidy", 0) < DEFAULT_CONFIG["mining_reward"],
                f"(subsidy {supply.get('next_block_subsidy')} at height {height})",
            )

            code, sent = request(
                "/api/transactions",
                method="POST",
                payload={"receiver": "ab" * 32, "amount": 1.0, "fee": 0.01},
                token=token,
            )
            check("transaction is accepted", code == 200, str(sent)[:200])

            _status, body = request("/api/status")
            check("mempool holds it", body.get("mempool", {}).get("count") == 1)

            _status, history = request("/api/transactions")
            check("history lists the mined coinbase", history.get("total", 0) >= 2)

            code, _ = request("/api/stats")
            check("stats endpoint responds", code == 200)

            _status, found = request(f"/api/search?q={height}")
            check("search finds the block", found.get("kind") == "block")

            _status, tips = request("/api/tips")
            check("tips endpoint reports a clean chain", tips.get("forked") is False)
        finally:
            process.terminate()
            try:
                output = process.communicate(timeout=15)[0]
            except subprocess.TimeoutExpired:
                process.kill()
                output = process.communicate()[0]
            if failures:
                print("\n--- node output ---")
                print((output or "")[-3000:])

    if failures:
        print(f"\nsmoke test FAILED: {', '.join(failures)}")
        sys.exit(1)
    print("\nsmoke test passed")


if __name__ == "__main__":
    main()
