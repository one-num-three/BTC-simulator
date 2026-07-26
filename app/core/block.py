from __future__ import annotations

import time
from typing import Any

from app.core.merkle import EMPTY_MERKLE_ROOT, merkle_root
from app.utils.crypto import hash_json

GENESIS_PREV_HASH = "0" * 64
MAX_TARGET_INT = (1 << 256) - 1
MAX_TARGET_HEX = "f" * 64


def compute_block_hash(block_or_header: dict[str, Any]) -> str:
    header = block_or_header.get("header", block_or_header)
    return hash_json(header)


def create_block(
    prev_hash: str,
    transactions: list[dict[str, Any]],
    difficulty: int,
    target: str | None = None,
    nonce: int = 0,
    timestamp: int | None = None,
    version: int = 1,
) -> dict[str, Any]:
    tx_ids = [tx["tx_id"] for tx in transactions]
    header = {
        "version": version,
        "prev_hash": prev_hash,
        "merkle_root": merkle_root(tx_ids),
        "timestamp": int(timestamp or time.time()),
        "difficulty": int(difficulty),
        "nonce": int(nonce),
    }
    if target is not None:
        header["target"] = normalize_target_hex(target)
    return {
        "header": header,
        "transactions": transactions,
    }


def genesis_block() -> dict[str, Any]:
    return {
        "header": {
            "version": 1,
            "prev_hash": GENESIS_PREV_HASH,
            "merkle_root": EMPTY_MERKLE_ROOT,
            "timestamp": 0,
            "difficulty": 0,
            "nonce": 0,
        },
        "transactions": [],
    }


def hash_meets_difficulty(block_hash: str, difficulty: int) -> bool:
    return hash_meets_target(block_hash, difficulty_to_target(difficulty))


def normalize_target_hex(target: str | int) -> str:
    if isinstance(target, int):
        value = target
    else:
        text = str(target).strip().lower()
        if text.startswith("0x"):
            text = text[2:]
        if not text or len(text) > 64:
            raise ValueError("target must contain 1 to 64 hexadecimal characters")
        try:
            value = int(text, 16)
        except ValueError as exc:
            raise ValueError("target must be hexadecimal") from exc
    if value < 0 or value > MAX_TARGET_INT:
        raise ValueError("target is outside the 256-bit range")
    return f"{value:064x}"


def difficulty_to_target(difficulty: int) -> str:
    bits = min(max(int(difficulty), 0), 256)
    if bits == 0:
        return MAX_TARGET_HEX
    if bits == 256:
        return "0" * 64
    return normalize_target_hex((1 << (256 - bits)) - 1)


def target_to_difficulty(target: str | int) -> int:
    value = int(normalize_target_hex(target), 16)
    if value == 0:
        return 256
    return max(256 - value.bit_length(), 0)


def target_preview(target: str | int, minimum_chars: int = 8) -> str:
    normalized = normalize_target_hex(target)
    leading_zeros = target_to_difficulty(normalized)
    visible = min(max(int(minimum_chars), leading_zeros + 2), 64)
    if visible == 64:
        return normalized
    return f"{normalized[:visible]}..."


def hash_meets_target(block_hash: str, target: str | int) -> bool:
    return int(block_hash, 16) <= int(normalize_target_hex(target), 16)


def block_work(target: str | int) -> int:
    """Expected number of hashes needed to find one block at this target.

    Bitcoin scores a chain by cumulative work, not by block count. The work of a
    single block is ``2**256 / (target + 1)``: the fraction of the 256-bit hash
    space that satisfies it. With the simulator's leading-zero-bit targets
    (``target = 2**(256-d) - 1``) this reduces to exactly ``2**d``, so a block at
    difficulty 13 is worth two blocks at difficulty 12.

    This is why the longest-chain rule must compare work and not height: with
    automatic difficulty a chain of 11 easy blocks is *shorter* in work than a
    chain of 10 hard ones, even though it has more blocks.
    """
    value = int(normalize_target_hex(target), 16)
    return (1 << 256) // (value + 1)


def header_work(header: dict[str, Any]) -> int:
    """Work of a block header, preferring its explicit target."""
    target = header.get("target")
    if target is None:
        return block_work(difficulty_to_target(int(header.get("difficulty", 0))))
    return block_work(target)


def chain_work_of(blocks: list[dict[str, Any]]) -> int:
    """Cumulative work of a list of blocks (genesis contributes nothing)."""
    return sum(header_work(block["header"]) for block in blocks[1:])


def format_work(work: int) -> str:
    """Store cumulative work as fixed-width hex so it sorts as text in SQLite."""
    return f"{int(work):064x}"


def parse_work(value: str | int | None) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return 0
    return int(text, 16)


MEDIAN_TIME_SPAN = 11


def median_time_past(timestamps: list[int]) -> int | None:
    """Median of the last ``MEDIAN_TIME_SPAN`` block timestamps.

    Bitcoin requires a new block's timestamp to be strictly greater than this
    value. Without it a miner can back-date blocks to shrink the measured span
    of a retarget window and drive difficulty up (or forward-date to drive it
    down), because the retarget only looks at the first and last timestamp.
    """
    recent = [int(value) for value in timestamps[-MEDIAN_TIME_SPAN:]]
    if not recent:
        return None
    recent.sort()
    return recent[len(recent) // 2]
