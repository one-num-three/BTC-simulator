"""Consensus rules that only show up once two nodes disagree.

Everything here covers behaviour the simulator did not have before: cumulative
work as the chain-selection rule, orphan retention so a mining race can heal,
and a median-time-past floor so timestamps cannot be used to steer difficulty.
"""

from __future__ import annotations

import copy
import time
from pathlib import Path

from app.config import DEFAULT_CONFIG
from app.core.block import (
    MEDIAN_TIME_SPAN,
    block_work,
    compute_block_hash,
    difficulty_to_target,
    format_work,
    header_work,
    median_time_past,
    parse_work,
)
from app.core.blockchain import DISCONNECTED_BLOCK_REASON, MAX_ORPHAN_BLOCKS, Blockchain
from app.core.wallet import generate_wallet
from app.storage.sqlite_store import SQLiteStore
from app.utils.collections import BoundedSet
from tests.test_core import mine_block


def make_chain(tmp_path: Path, name: str = "chain", **overrides):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["difficulty"] = 1
    config["auto_difficulty"] = False
    config.update(overrides)
    config["storage"]["path"] = str(tmp_path / f"{name}.db")
    store = SQLiteStore(config["storage"]["path"])
    return config, store, Blockchain(config, store)


def chain_blocks(store: SQLiteStore) -> list[dict]:
    return store.get_blocks_from_height(0)


# --------------------------------------------------------------------------
# work accounting
# --------------------------------------------------------------------------


def test_block_work_doubles_per_difficulty_bit():
    assert block_work(difficulty_to_target(0)) == 1
    assert block_work(difficulty_to_target(1)) == 2
    assert block_work(difficulty_to_target(8)) == 256
    # each extra leading zero bit halves the target, so it doubles the work
    for bits in range(0, 20):
        assert block_work(difficulty_to_target(bits + 1)) == 2 * block_work(
            difficulty_to_target(bits)
        )


def test_header_work_falls_back_to_difficulty_when_target_is_absent():
    assert header_work({"difficulty": 5}) == block_work(difficulty_to_target(5))
    assert header_work({"difficulty": 0, "target": difficulty_to_target(7)}) == 128


def test_work_round_trips_through_hex_storage():
    for value in (0, 1, 2**40, 2**200 + 12345):
        assert parse_work(format_work(value)) == value
    assert parse_work(None) == 0
    assert parse_work("") == 0


def test_store_tracks_cumulative_chain_work(tmp_path: Path):
    _config, store, chain = make_chain(tmp_path)
    wallet = generate_wallet("miner")
    assert chain.chain_work() == 0

    expected = 0
    for _ in range(3):
        block, _block_hash = mine_block(chain, wallet.address)
        accepted, _ = chain.add_block(block)
        assert accepted
        expected += header_work(block["header"])
        assert chain.chain_work() == expected


def test_recompute_chain_work_backfills_legacy_databases(tmp_path: Path):
    _config, store, chain = make_chain(tmp_path)
    wallet = generate_wallet("miner")
    for _ in range(3):
        block, _ = mine_block(chain, wallet.address)
        assert chain.add_block(block)[0]

    expected = chain.chain_work()
    # simulate a database written before the chain_work column existed
    with store.lock, store.conn:
        store.conn.execute("UPDATE blocks SET chain_work = NULL")
    assert store.tip_chain_work() == expected


# --------------------------------------------------------------------------
# chain selection by work, not height
# --------------------------------------------------------------------------


def test_heavier_chain_wins_even_when_it_has_fewer_blocks(tmp_path: Path):
    """The rule that was wrong before: length is not the tie-break, work is."""
    _c1, store_a, chain_a = make_chain(tmp_path, "a", difficulty=1)
    _c2, store_b, chain_b = make_chain(tmp_path, "b", difficulty=1)

    wallet = generate_wallet("miner")
    # A: four cheap blocks
    for _ in range(4):
        assert chain_a.add_block(mine_block(chain_a, wallet.address)[0])[0]
    # B: two expensive blocks, mined under a harder rule
    chain_b.config["difficulty"] = 6
    for _ in range(2):
        assert chain_b.add_block(mine_block(chain_b, wallet.address)[0])[0]

    assert chain_a.height() > chain_b.height()
    assert chain_b.chain_work() > chain_a.chain_work()

    # A must adopt B's chain despite B being shorter in blocks.
    chain_a.config["difficulty"] = 6
    accepted, message = chain_a.replace_with_chain(chain_blocks(store_b), source="test")
    assert accepted, message
    assert chain_a.height() == chain_b.height()
    assert chain_a.chain_work() == chain_b.chain_work()


def test_lighter_chain_is_rejected_even_when_it_is_longer(tmp_path: Path):
    _c1, store_a, chain_a = make_chain(tmp_path, "a", difficulty=6)
    _c2, store_b, chain_b = make_chain(tmp_path, "b", difficulty=1)

    wallet = generate_wallet("miner")
    for _ in range(2):
        assert chain_a.add_block(mine_block(chain_a, wallet.address)[0])[0]
    chain_b.config["difficulty"] = 1
    for _ in range(4):
        assert chain_b.add_block(mine_block(chain_b, wallet.address)[0])[0]

    heavy_work = chain_a.chain_work()
    chain_a.config["difficulty"] = 1
    accepted, message = chain_a.replace_with_chain(chain_blocks(store_b), source="test")
    assert not accepted
    assert "less work" in message
    assert chain_a.chain_work() == heavy_work


def test_equal_work_keeps_the_chain_we_already_had(tmp_path: Path):
    _c1, store_a, chain_a = make_chain(tmp_path, "a")
    _c2, store_b, chain_b = make_chain(tmp_path, "b")
    wallet = generate_wallet("miner")

    for _ in range(3):
        assert chain_a.add_block(mine_block(chain_a, wallet.address)[0])[0]
    for _ in range(3):
        assert chain_b.add_block(mine_block(chain_b, wallet.address)[0])[0]

    original_tip = chain_a.tip_hash()
    accepted, message = chain_a.replace_with_chain(chain_blocks(store_b), source="test")
    assert not accepted
    assert "same work" in message
    assert chain_a.tip_hash() == original_tip


# --------------------------------------------------------------------------
# orphan retention: a mining race must heal
# --------------------------------------------------------------------------


def test_competing_block_is_kept_as_an_orphan_not_discarded(tmp_path: Path):
    _c1, _store_a, chain_a = make_chain(tmp_path, "a")
    _c2, _store_b, chain_b = make_chain(tmp_path, "b")
    # Distinct miners, otherwise both nodes build a byte-identical candidate
    # (same height, same coinbase receiver, same second) and the "race" is a
    # duplicate rather than a fork.
    miner_a = generate_wallet("miner-a")
    miner_b = generate_wallet("miner-b")

    own, _ = mine_block(chain_a, miner_a.address)
    assert chain_a.add_block(own)[0]

    rival, rival_hash = mine_block(chain_b, miner_b.address)
    accepted, message = chain_a.add_block(rival, source="peer")

    assert not accepted
    assert message == DISCONNECTED_BLOCK_REASON
    # Previously this block was dropped and the split became permanent.
    assert rival_hash in chain_a.orphans


def test_orphan_is_connected_once_its_parent_arrives(tmp_path: Path):
    _c1, store_src, chain_src = make_chain(tmp_path, "src")
    _c2, _store_dst, chain_dst = make_chain(tmp_path, "dst")
    wallet = generate_wallet("miner")

    first, _ = mine_block(chain_src, wallet.address)
    assert chain_src.add_block(first)[0]
    second, second_hash = mine_block(chain_src, wallet.address)
    assert chain_src.add_block(second)[0]

    # deliver out of order
    accepted, _ = chain_dst.add_block(second, source="peer")
    assert not accepted
    assert chain_dst.height() == 0
    assert len(chain_dst.orphans) == 1

    accepted, _ = chain_dst.add_block(first, source="peer")
    assert accepted
    # connect_orphans should have stitched the cached child on automatically
    assert chain_dst.height() == 2
    assert chain_dst.tip_hash() == second_hash
    assert not chain_dst.orphans


def test_orphan_pool_is_bounded(tmp_path: Path):
    _c1, _store, chain = make_chain(tmp_path)
    for index in range(MAX_ORPHAN_BLOCKS + 25):
        chain.remember_orphan(
            {
                "header": {
                    "version": 1,
                    "prev_hash": f"{index:064x}",
                    "merkle_root": "0" * 64,
                    "timestamp": index,
                    "difficulty": 1,
                    "nonce": index,
                    "target": difficulty_to_target(1),
                },
                "transactions": [],
            }
        )
    assert len(chain.orphans) == MAX_ORPHAN_BLOCKS


# --------------------------------------------------------------------------
# median time past
# --------------------------------------------------------------------------


def test_median_time_past_uses_the_middle_of_the_window():
    assert median_time_past([]) is None
    assert median_time_past([10]) == 10
    assert median_time_past([5, 1, 3]) == 3
    # only the last MEDIAN_TIME_SPAN entries count
    values = list(range(100))
    assert median_time_past(values) == median_time_past(values[-MEDIAN_TIME_SPAN:])


def test_block_with_backdated_timestamp_is_rejected(tmp_path: Path):
    _config, _store, chain = make_chain(tmp_path)
    wallet = generate_wallet("miner")
    now = int(time.time())

    for offset in range(MEDIAN_TIME_SPAN + 2):
        block, _ = mine_block(chain, wallet.address, timestamp=now + offset)
        assert chain.add_block(block)[0]

    floor = chain.median_time_past()
    assert floor is not None

    stale = chain.create_candidate_block(wallet.address, [])
    stale["header"]["timestamp"] = floor - 1  # one second below the floor
    nonce = 0
    while True:
        stale["header"]["nonce"] = nonce
        if compute_block_hash(stale) <= stale["header"]["target"]:
            break
        nonce += 1

    accepted, message = chain.add_block(stale, source="attacker")
    assert not accepted
    assert "median" in message


def test_candidate_timestamp_is_never_older_than_the_median(tmp_path: Path):
    """Fast classroom blocks must not fail their own timestamp rule."""
    _config, _store, chain = make_chain(tmp_path)
    wallet = generate_wallet("miner")
    future = int(time.time()) + 3600

    for _ in range(MEDIAN_TIME_SPAN + 2):
        block, _ = mine_block(chain, wallet.address, timestamp=future)
        chain.add_block(block)

    floor = chain.median_time_past()
    assert floor is not None
    candidate = chain.create_candidate_block(wallet.address, [])
    assert int(candidate["header"]["timestamp"]) >= floor


def test_fast_mining_does_not_ratchet_timestamps_into_the_future(tmp_path: Path):
    """Regression: a strictly-greater MTP rule drifts a fast chain forward.

    With `timestamp > median` the miner has to add a second whenever the median
    catches up, whether or not real time moved. At the hundreds-of-blocks-per-
    second a difficulty-1 classroom chain reaches, that walked the chain hours
    into the future within minutes -- and then the two-hour future-block limit
    started rejecting the node's own blocks.
    """
    _config, _store, chain = make_chain(tmp_path, difficulty=0)
    wallet = generate_wallet("miner")
    start = int(time.time())

    for _ in range(400):
        block, _ = mine_block(chain, wallet.address)
        assert chain.add_block(block)[0]

    drift = int(chain.tip()["timestamp"]) - int(time.time())
    assert drift <= 2, f"timestamps drifted {drift}s ahead of the wall clock"
    assert int(chain.tip()["timestamp"]) >= start


# --------------------------------------------------------------------------
# gossip cache
# --------------------------------------------------------------------------


def test_bounded_set_evicts_oldest_entries():
    seen = BoundedSet(maxlen=3)
    for item in ("a", "b", "c"):
        seen.add(item)
    assert len(seen) == 3
    seen.add("d")
    assert len(seen) == 3
    assert "a" not in seen
    assert "d" in seen


def test_bounded_set_refreshes_on_repeat():
    seen = BoundedSet(maxlen=3)
    for item in ("a", "b", "c"):
        seen.add(item)
    seen.add("a")  # touch -> now the newest
    seen.add("d")
    assert "a" in seen
    assert "b" not in seen


def test_mempool_survives_a_reorg_by_being_cleared(tmp_path: Path):
    """replace_chain wipes the mempool; make sure orphans go with it."""
    _c1, store_a, chain_a = make_chain(tmp_path, "a")
    _c2, store_b, chain_b = make_chain(tmp_path, "b")
    wallet = generate_wallet("miner")

    assert chain_a.add_block(mine_block(chain_a, wallet.address)[0])[0]
    for _ in range(3):
        assert chain_b.add_block(mine_block(chain_b, wallet.address)[0])[0]

    chain_a.remember_orphan(mine_block(chain_b, wallet.address)[0])
    assert chain_a.orphans

    accepted, _ = chain_a.replace_with_chain(chain_blocks(store_b), source="test")
    assert accepted
    assert not chain_a.orphans
