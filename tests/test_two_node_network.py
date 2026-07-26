"""Two real nodes over a real socket.

The P2P layer had no tests at all, which is how "a mining race splits the
network permanently" survived: every unit test drove a single chain object, and
the bug only exists in the interaction between two of them.

These start actual asyncio servers on loopback and let them talk.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

from app.config import DEFAULT_CONFIG, load_config
from app.core.block import compute_block_hash
from app.core.wallet import generate_wallet
from app.runtime import NodeService

pytestmark = pytest.mark.anyio if False else []

_PORT = [19100]


def make_service(tmp_path: Path, name: str, peers=(), **overrides) -> NodeService:
    _PORT[0] += 2
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "node_name": name,
            "difficulty": 2,
            "auto_difficulty": False,
            "listen_ip": "127.0.0.1",
            "enable_ipv6": False,
            "listen_port": _PORT[0],
            "web_port": _PORT[0] + 1,
            "servers": [list(peer) for peer in peers],
            "coinbase_maturity": 0,
            "halving_interval": 10**6,
            "sync_interval_seconds": 1,
            "storage": {"type": "sqlite", "path": f"./data/{name}.db"},
        }
    )
    config.update(overrides)
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return NodeService(load_config(path))


def mine_onto(service: NodeService, wallet, count: int = 1):
    """Mine synchronously into a node's own chain (no miner task involved)."""
    hashes = []
    for _ in range(count):
        block = service.blockchain.create_candidate_block(wallet.address, [])
        nonce = 0
        while True:
            block["header"]["nonce"] = nonce
            block_hash = compute_block_hash(block)
            if block_hash <= block["header"]["target"]:
                break
            nonce += 1
        accepted, message = service.blockchain.add_block(block, source="test")
        assert accepted, message
        hashes.append(block_hash)
    return hashes


async def wait_for(predicate, timeout: float = 12.0, interval: float = 0.05) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------


def test_nodes_connect_and_sync_the_chain(tmp_path: Path):
    async def scenario():
        alice = make_service(tmp_path, "alice")
        wallet = generate_wallet("alice-miner")
        mine_onto(alice, wallet, 3)

        bob = make_service(tmp_path, "bob", peers=[("127.0.0.1", alice.config["listen_port"])])
        try:
            await alice.start()
            await bob.start()
            connected = await wait_for(lambda: bob.p2p.connection_counts()["total"] > 0)
            assert connected, "bob never connected to alice"
            # bob starts empty and must pull alice's heavier chain
            synced = await wait_for(lambda: bob.blockchain.height() == 3)
            assert synced, f"bob stuck at height {bob.blockchain.height()}"
            assert bob.blockchain.tip_hash() == alice.blockchain.tip_hash()
            assert bob.blockchain.chain_work() == alice.blockchain.chain_work()
        finally:
            await alice.shutdown()
            await bob.shutdown()

    run(scenario())


def test_a_mining_race_heals_instead_of_splitting_the_network(tmp_path: Path):
    """The regression this whole change set exists for.

    Both nodes mine the same height at the same time, so each rejects the
    other's block as "does not connect to current tip". Before, that was the
    end of it: two chains, same height, no recovery path, and the only cure was
    a human clicking sync. Now the loser asks for the winner's chain and the
    heavier one wins.
    """

    async def scenario():
        alice = make_service(tmp_path, "race-a")
        bob = make_service(
            tmp_path, "race-b", peers=[("127.0.0.1", alice.config["listen_port"])]
        )
        alice_wallet = generate_wallet("race-a-miner")
        bob_wallet = generate_wallet("race-b-miner")
        try:
            await alice.start()
            await bob.start()
            assert await wait_for(lambda: bob.p2p.connection_counts()["total"] > 0)

            # simultaneous, competing blocks at height 1
            mine_onto(alice, alice_wallet, 1)
            mine_onto(bob, bob_wallet, 1)
            assert alice.blockchain.tip_hash() != bob.blockchain.tip_hash()
            assert alice.blockchain.height() == bob.blockchain.height() == 1

            # alice pulls ahead; her chain now carries strictly more work
            mine_onto(alice, alice_wallet, 2)
            await alice.p2p.broadcast_block(
                alice.store.get_blocks_from_height(alice.blockchain.height())[0]
            )

            healed = await wait_for(
                lambda: bob.blockchain.tip_hash() == alice.blockchain.tip_hash(), timeout=20
            )
            assert healed, (
                "network stayed split: "
                f"alice h={alice.blockchain.height()} w={alice.blockchain.chain_work()}, "
                f"bob h={bob.blockchain.height()} w={bob.blockchain.chain_work()}"
            )
            assert bob.blockchain.height() == 3
        finally:
            await alice.shutdown()
            await bob.shutdown()

    run(scenario())


def test_periodic_sync_catches_a_node_up_with_no_broadcast(tmp_path: Path):
    """`sync_interval_seconds` was configured everywhere and read nowhere."""

    async def scenario():
        alice = make_service(tmp_path, "tick-a")
        bob = make_service(tmp_path, "tick-b", peers=[("127.0.0.1", alice.config["listen_port"])])
        wallet = generate_wallet("tick-miner")
        try:
            await alice.start()
            await bob.start()
            assert await wait_for(lambda: bob.p2p.connection_counts()["total"] > 0)
            assert bob.blockchain.height() == 0

            # Mine after the handshake and never broadcast. Only the periodic
            # sync tick can get bob there.
            mine_onto(alice, wallet, 4)
            caught_up = await wait_for(lambda: bob.blockchain.height() == 4, timeout=20)
            assert caught_up, f"bob stayed at height {bob.blockchain.height()}"
        finally:
            await alice.shutdown()
            await bob.shutdown()

    run(scenario())


def test_a_transaction_reaches_the_other_node(tmp_path: Path):
    async def scenario():
        alice = make_service(tmp_path, "tx-a")
        bob = make_service(tmp_path, "tx-b", peers=[("127.0.0.1", alice.config["listen_port"])])
        try:
            await alice.start()
            await bob.start()
            assert await wait_for(lambda: bob.p2p.connection_counts()["total"] > 0)

            wallet = alice.default_wallet()
            miner = generate_wallet("tx-miner")
            # fund alice's own wallet
            block = alice.blockchain.create_candidate_block(wallet["address"], [])
            nonce = 0
            while True:
                block["header"]["nonce"] = nonce
                if compute_block_hash(block) <= block["header"]["target"]:
                    break
                nonce += 1
            assert alice.blockchain.add_block(block)[0]
            await wait_for(lambda: bob.blockchain.height() == 1, timeout=20)

            accepted, _message, _tx = await alice.create_transaction(
                miner.address, amount=1.0, fee=0.05
            )
            assert accepted
            relayed = await wait_for(lambda: bob.mempool.stats()["count"] == 1)
            assert relayed, "the transaction never reached bob's mempool"
        finally:
            await alice.shutdown()
            await bob.shutdown()

    run(scenario())


def test_a_node_with_different_consensus_params_is_refused(tmp_path: Path):
    """chain_params_hash keeps two classrooms from contaminating each other."""

    async def scenario():
        alice = make_service(tmp_path, "params-a")
        bob = make_service(
            tmp_path,
            "params-b",
            peers=[("127.0.0.1", alice.config["listen_port"])],
            halving_interval=3,  # different consensus rule
        )
        try:
            await alice.start()
            await bob.start()
            await asyncio.sleep(2.0)
            peers = bob.store.list_peers()
            assert peers, "bob should have recorded the attempt"
            assert any(
                peer.get("status") == "param_mismatch"
                or "mismatch" in str(peer.get("mismatch_reason") or "")
                for peer in peers
            ), peers
            assert bob.p2p.connection_counts()["total"] == 0
        finally:
            await alice.shutdown()
            await bob.shutdown()

    run(scenario())


def test_a_banned_peer_cannot_reconnect(tmp_path: Path):
    async def scenario():
        alice = make_service(tmp_path, "ban-a")
        bob = make_service(tmp_path, "ban-b")
        try:
            await alice.start()
            await bob.start()
            await bob.ban_peer("127.0.0.1", alice.config["listen_port"])
            accepted, message = await bob.p2p.connect_peer(
                "127.0.0.1", alice.config["listen_port"]
            )
            assert not accepted
            assert "banned" in message
        finally:
            await alice.shutdown()
            await bob.shutdown()

    run(scenario())
