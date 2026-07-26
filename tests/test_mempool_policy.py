"""Mempool as a fee market.

Previously a full mempool rejected every new transaction regardless of what it
paid, nothing ever expired, and there was no way to bump a stuck transaction.
That made "why do fees go up when the network is busy" impossible to show.
"""

from __future__ import annotations

import copy
import time
from pathlib import Path

from app.config import DEFAULT_CONFIG
from app.core.blockchain import Blockchain
from app.core.mempool import Mempool
from app.core.transaction import create_transfer, estimate_transaction_bytes
from app.core.wallet import generate_wallet
from app.storage.sqlite_store import SQLiteStore
from tests.test_core import mine_block


def make_stack(tmp_path: Path, **overrides):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "difficulty": 1,
            "auto_difficulty": False,
            "coinbase_maturity": 0,
            "halving_interval": 10**6,
        }
    )
    config.update(overrides)
    config["storage"]["path"] = str(tmp_path / "mempool.db")
    store = SQLiteStore(config["storage"]["path"])
    chain = Blockchain(config, store)
    return config, store, chain, Mempool(store, chain)


def funded_wallet(chain: Blockchain, blocks: int = 3):
    wallet = generate_wallet("payer")
    for _ in range(blocks):
        block, _ = mine_block(chain, wallet.address)
        assert chain.add_block(block)[0]
    return wallet


def transfer(wallet, receiver, amount, fee, note=None, timestamp=None):
    return create_transfer(
        wallet.address,
        receiver,
        amount=amount,
        fee=fee,
        public_key=wallet.public_key,
        private_key=wallet.private_key,
        note=note,
        timestamp=timestamp,
    )


def test_ordered_puts_the_best_fee_rate_first(tmp_path: Path):
    _config, _store, chain, mempool = make_stack(tmp_path)
    wallet = funded_wallet(chain)
    targets = [generate_wallet(f"r{i}").address for i in range(3)]

    assert mempool.add_transaction(transfer(wallet, targets[0], 1, 0.001))[0]
    assert mempool.add_transaction(transfer(wallet, targets[1], 1, 0.5))[0]
    assert mempool.add_transaction(transfer(wallet, targets[2], 1, 0.05))[0]

    fees = [float(tx["fee"]) for tx in mempool.ordered()]
    assert fees == sorted(fees, reverse=True)


def test_full_mempool_evicts_the_cheapest_instead_of_refusing_everything(tmp_path: Path):
    wallet_probe = generate_wallet("probe")
    sample = create_transfer(
        wallet_probe.address,
        generate_wallet("x").address,
        amount=1,
        fee=0.1,
        public_key=wallet_probe.public_key,
        private_key=wallet_probe.private_key,
    )
    size = estimate_transaction_bytes(sample)

    # room for about two transactions
    _config, _store, chain, mempool = make_stack(tmp_path, mempool_max_bytes=size * 2 + 20)
    wallet = funded_wallet(chain)

    cheap = transfer(wallet, generate_wallet("a").address, 1, 0.0001)
    middle = transfer(wallet, generate_wallet("b").address, 1, 0.01)
    rich = transfer(wallet, generate_wallet("c").address, 1, 5.0)

    assert mempool.add_transaction(cheap)[0]
    assert mempool.add_transaction(middle)[0]

    accepted, message = mempool.add_transaction(rich)
    assert accepted, message

    remaining = {tx["tx_id"] for tx in mempool.ordered()}
    assert rich["tx_id"] in remaining
    assert cheap["tx_id"] not in remaining, "the cheapest should have been evicted"


def test_a_transaction_cheaper_than_everything_queued_cannot_push_in(tmp_path: Path):
    probe = generate_wallet("probe")
    sample = create_transfer(
        probe.address,
        generate_wallet("x").address,
        amount=1,
        fee=0.1,
        public_key=probe.public_key,
        private_key=probe.private_key,
    )
    size = estimate_transaction_bytes(sample)
    _config, _store, chain, mempool = make_stack(tmp_path, mempool_max_bytes=size * 2 + 20)
    wallet = funded_wallet(chain)

    assert mempool.add_transaction(transfer(wallet, generate_wallet("a").address, 1, 1.0))[0]
    assert mempool.add_transaction(transfer(wallet, generate_wallet("b").address, 1, 1.0))[0]

    accepted, message = mempool.add_transaction(
        transfer(wallet, generate_wallet("c").address, 1, 0.000001)
    )
    assert not accepted
    assert "fee rate" in message
    assert len(mempool.ordered()) == 2


def test_replace_by_fee_bumps_a_stuck_transaction(tmp_path: Path):
    _config, _store, chain, mempool = make_stack(tmp_path)
    wallet = funded_wallet(chain)
    receiver = generate_wallet("receiver").address

    original = transfer(wallet, receiver, 2, 0.001, timestamp=int(time.time()))
    assert mempool.add_transaction(original)[0]

    bumped = transfer(wallet, receiver, 2, 0.75, timestamp=int(time.time()) + 1)
    accepted, message = mempool.add_transaction(bumped)
    assert accepted, message

    ids = {tx["tx_id"] for tx in mempool.ordered()}
    assert bumped["tx_id"] in ids
    assert original["tx_id"] not in ids, "the low-fee original should be gone"


def test_a_lower_fee_replacement_is_not_a_replacement(tmp_path: Path):
    _config, _store, chain, mempool = make_stack(tmp_path)
    wallet = funded_wallet(chain)
    receiver = generate_wallet("receiver").address

    original = transfer(wallet, receiver, 2, 0.5, timestamp=int(time.time()))
    assert mempool.add_transaction(original)[0]

    cheaper = transfer(wallet, receiver, 2, 0.01, timestamp=int(time.time()) + 1)
    accepted, _message = mempool.add_transaction(cheaper)
    ids = {tx["tx_id"] for tx in mempool.ordered()}
    assert original["tx_id"] in ids, "the original must survive a cheaper bid"
    if accepted:
        assert cheaper["tx_id"] in ids


def test_expired_transactions_are_dropped(tmp_path: Path):
    _config, store, chain, mempool = make_stack(tmp_path, mempool_expiry_seconds=60)
    wallet = funded_wallet(chain)
    tx = transfer(wallet, generate_wallet("r").address, 1, 0.02)
    assert mempool.add_transaction(tx)[0]

    # backdate its arrival past the expiry window
    with store.lock, store.conn:
        store.conn.execute(
            "UPDATE mempool SET received_at = ? WHERE tx_id = ?",
            (int(time.time()) - 600, tx["tx_id"]),
        )

    assert mempool.expire_old() == 1
    assert mempool.stats()["count"] == 0


def test_min_relay_fee_is_enforced(tmp_path: Path):
    _config, _store, chain, mempool = make_stack(tmp_path, min_relay_fee=0.01)
    wallet = funded_wallet(chain)

    accepted, message = mempool.add_transaction(
        transfer(wallet, generate_wallet("r").address, 1, 0.0)
    )
    assert not accepted
    assert "relay minimum" in message

    assert mempool.add_transaction(transfer(wallet, generate_wallet("r2").address, 1, 0.02))[0]


def test_stats_expose_the_fee_market(tmp_path: Path):
    _config, _store, chain, mempool = make_stack(tmp_path)
    wallet = funded_wallet(chain)
    assert mempool.add_transaction(transfer(wallet, generate_wallet("a").address, 1, 0.01))[0]
    assert mempool.add_transaction(transfer(wallet, generate_wallet("b").address, 1, 0.9))[0]

    stats = mempool.stats()
    assert stats["count"] == 2
    assert stats["max_fee_rate"] > stats["min_fee_rate"] > 0
    assert stats["total_fees"] == 0.91
    assert 0 <= stats["usage"] <= 1


def test_miner_packs_the_highest_fee_rate_first(tmp_path: Path):
    _config, _store, chain, mempool = make_stack(tmp_path)
    wallet = funded_wallet(chain)
    assert mempool.add_transaction(transfer(wallet, generate_wallet("low").address, 1, 0.0001))[0]
    assert mempool.add_transaction(transfer(wallet, generate_wallet("high").address, 1, 2.0))[0]

    picked = chain.select_transactions_for_block(1)
    assert len(picked) == 1
    assert float(picked[0]["fee"]) == 2.0
