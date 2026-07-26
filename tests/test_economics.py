"""Issuance schedule, supply cap and coinbase maturity.

None of these rules existed before: the block reward was a constant read from
the config, so the chain issued coins forever and mining income was spendable
the instant it was mined.
"""

from __future__ import annotations

import copy
from pathlib import Path

from app.config import DEFAULT_CONFIG, chain_params_hash
from app.core.blockchain import Blockchain
from app.core.transaction import create_transfer
from app.core.wallet import generate_wallet
from app.storage.sqlite_store import SQLiteStore

from tests.test_core import mine_block


def make_chain(tmp_path: Path, name: str = "chain", **overrides):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["difficulty"] = 1
    config["auto_difficulty"] = False
    config.update(overrides)
    config["storage"]["path"] = str(tmp_path / f"{name}.db")
    store = SQLiteStore(config["storage"]["path"])
    return config, store, Blockchain(config, store)


# --------------------------------------------------------------------------
# halving
# --------------------------------------------------------------------------


def test_subsidy_halves_on_schedule(tmp_path: Path):
    _config, _store, chain = make_chain(tmp_path, halving_interval=10, mining_reward=50.0)
    assert chain.block_subsidy(1) == 50.0
    assert chain.block_subsidy(9) == 50.0
    assert chain.block_subsidy(10) == 25.0
    assert chain.block_subsidy(19) == 25.0
    assert chain.block_subsidy(20) == 12.5
    assert chain.block_subsidy(30) == 6.25


def test_subsidy_eventually_reaches_zero(tmp_path: Path):
    _config, _store, chain = make_chain(tmp_path, halving_interval=1, mining_reward=50.0)
    # 50 / 2**n drops below one satoshi after ~33 halvings
    assert chain.block_subsidy(100) == 0.0


def test_supply_is_capped(tmp_path: Path):
    _config, _store, chain = make_chain(tmp_path, halving_interval=10, mining_reward=50.0)
    cap = chain.max_supply()
    # Geometric series 10 * 50 * 2 = 1000, minus the 50 that genesis never
    # pays, minus satoshi-rounding dust in the deepest eras. Bitcoin's cap is
    # 20,999,999.9769 rather than 21,000,000 for the same kind of reason, so
    # the ragged number here is the honest one.
    assert 949.0 < cap < 950.0
    assert cap < 10 * 50 * 2

    # and the chain really cannot exceed it
    total = sum(
        chain.block_subsidy(height) for height in range(1, chain.halving_interval * 40)
    )
    assert round(total, 8) <= cap


def test_mined_coinbase_follows_the_schedule(tmp_path: Path):
    _config, store, chain = make_chain(
        tmp_path, halving_interval=3, mining_reward=8.0, coinbase_maturity=0
    )
    wallet = generate_wallet("miner")
    subsidies = []
    for _ in range(9):
        block, _ = mine_block(chain, wallet.address)
        assert chain.add_block(block)[0]
        subsidies.append(block["transactions"][0]["amount"])

    # Era boundaries fall on multiples of the interval, exactly as in Bitcoin
    # (`halvings = height / interval`). Height 0 is genesis and pays nothing,
    # so the first era only has `interval - 1` rewarded blocks.
    assert subsidies[:2] == [8.0, 8.0]  # heights 1, 2
    assert subsidies[2:5] == [4.0, 4.0, 4.0]  # heights 3, 4, 5
    assert subsidies[5:8] == [2.0, 2.0, 2.0]  # heights 6, 7, 8
    assert subsidies[8] == 1.0  # height 9
    assert chain.total_issued() == 35.0
    assert chain.get_balance(wallet.address) == 35.0


def test_block_with_wrong_subsidy_is_rejected(tmp_path: Path):
    _config, _store, chain = make_chain(
        tmp_path, halving_interval=2, mining_reward=10.0, coinbase_maturity=0
    )
    wallet = generate_wallet("miner")
    for _ in range(2):
        assert chain.add_block(mine_block(chain, wallet.address)[0])[0]

    # height 3 is in the second era, so the subsidy must be 5, not 10
    greedy = chain.create_candidate_block(wallet.address, [])
    assert greedy["transactions"][0]["amount"] == 5.0

    from app.core.block import compute_block_hash
    from app.core.merkle import merkle_root
    from app.core.transaction import create_coinbase

    coinbase = create_coinbase(wallet.address, 10.0, height=3)
    greedy["transactions"] = [coinbase]
    greedy["header"]["merkle_root"] = merkle_root([coinbase["tx_id"]])
    nonce = 0
    while True:
        greedy["header"]["nonce"] = nonce
        if compute_block_hash(greedy) <= greedy["header"]["target"]:
            break
        nonce += 1

    accepted, message = chain.add_block(greedy, source="greedy-miner")
    assert not accepted
    assert "subsidy" in message


def test_halving_parameters_are_part_of_the_consensus_fingerprint():
    base = copy.deepcopy(DEFAULT_CONFIG)
    changed = copy.deepcopy(DEFAULT_CONFIG)
    changed["halving_interval"] = base["halving_interval"] + 1
    assert chain_params_hash(base) != chain_params_hash(changed)

    changed = copy.deepcopy(DEFAULT_CONFIG)
    changed["coinbase_maturity"] = base["coinbase_maturity"] + 1
    assert chain_params_hash(base) != chain_params_hash(changed)


# --------------------------------------------------------------------------
# coinbase maturity
# --------------------------------------------------------------------------


def test_fresh_mining_reward_is_not_spendable(tmp_path: Path):
    _config, store, chain = make_chain(tmp_path, coinbase_maturity=3, halving_interval=10**6)
    miner = generate_wallet("miner")
    receiver = generate_wallet("receiver")

    block, _ = mine_block(chain, miner.address)
    assert chain.add_block(block)[0]

    assert chain.get_balance(miner.address) == 50.0
    assert chain.spendable_balance(miner.address) == 0.0
    assert chain.immature_balance(miner.address) == 50.0

    tx = create_transfer(
        miner.address,
        receiver.address,
        amount=1.0,
        fee=0.01,
        public_key=miner.public_key,
        private_key=miner.private_key,
    )
    try:
        chain.validate_transfer_transaction(tx)
    except ValueError as exc:
        assert "insufficient" in str(exc)
    else:
        raise AssertionError("immature coinbase should not be spendable")


def test_reward_becomes_spendable_after_the_maturity_depth(tmp_path: Path):
    _config, _store, chain = make_chain(tmp_path, coinbase_maturity=3, halving_interval=10**6)
    miner = generate_wallet("miner")

    for _ in range(4):
        block, _ = mine_block(chain, miner.address)
        assert chain.add_block(block)[0]

    # heights 1..4, spending happens at height 5.
    # height 1 has depth 4 >= 3, height 2 has depth 3 >= 3 -> both mature.
    assert chain.height() == 4
    assert chain.get_balance(miner.address) == 200.0
    assert chain.immature_balance(miner.address) == 100.0
    assert chain.spendable_balance(miner.address) == 100.0


def test_maturity_zero_keeps_the_old_instant_spend_behaviour(tmp_path: Path):
    _config, _store, chain = make_chain(tmp_path, coinbase_maturity=0, halving_interval=10**6)
    miner = generate_wallet("miner")
    assert chain.add_block(mine_block(chain, miner.address)[0])[0]
    assert chain.spendable_balance(miner.address) == 50.0
    assert chain.immature_balance(miner.address) == 0.0


def test_replacement_chain_enforces_maturity(tmp_path: Path):
    """A chain that spends an unripe reward must not be adopted."""
    _c1, store_src, chain_src = make_chain(
        tmp_path, "src", coinbase_maturity=0, halving_interval=10**6
    )
    miner = generate_wallet("miner")
    receiver = generate_wallet("receiver")

    assert chain_src.add_block(mine_block(chain_src, miner.address)[0])[0]
    tx = create_transfer(
        miner.address,
        receiver.address,
        amount=1.0,
        fee=0.0,
        public_key=miner.public_key,
        private_key=miner.private_key,
    )
    assert chain_src.mempool_ready(tx) if hasattr(chain_src, "mempool_ready") else True
    block = chain_src.create_candidate_block(miner.address, [tx])
    from app.core.block import compute_block_hash

    nonce = 0
    while True:
        block["header"]["nonce"] = nonce
        if compute_block_hash(block) <= block["header"]["target"]:
            break
        nonce += 1
    assert chain_src.add_block(block)[0]

    # Same blocks, but validated by a node that requires maturity 5.
    _c2, _store_dst, strict = make_chain(
        tmp_path, "dst", coinbase_maturity=5, halving_interval=10**6
    )
    accepted, message = strict.replace_with_chain(
        store_src.get_blocks_from_height(0), source="test"
    )
    assert not accepted
    assert "spends more than available balance" in message
