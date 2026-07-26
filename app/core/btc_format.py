"""Bitcoin's real wire formats, for teaching -- not for consensus.

The simulator hashes a block header as the double-SHA256 of its canonical JSON.
That is deliberate: it keeps the header readable, and a student can see exactly
what goes into the hash. But it means the number students see here has nothing
to do with the 80 bytes real Bitcoin hashes, and two of the most-asked
questions -- "what is actually in a block header?" and "what is that `bits`
field?" -- had no answer in this project.

This module implements both properly:

* ``serialize_header`` produces the genuine 80-byte header, little-endian
  fields and all, with hashes in internal byte order.
* ``target_to_nbits`` / ``nbits_to_target`` implement the compact
  floating-point encoding Bitcoin packs the target into.

Nothing here feeds ``compute_block_hash``. Changing the consensus hash would
invalidate every existing classroom database for no teaching gain, and the
JSON form is easier to read at the whiteboard. These functions back the
"real Bitcoin header" panel in the block explorer, where the two
representations are shown side by side.

The serializer is verified against the real Bitcoin genesis block, whose
80-byte header and hash are fixed constants of the network, so this cannot
silently drift into being subtly wrong.
"""

from __future__ import annotations

import hashlib
import struct
from typing import Any

from app.core.block import normalize_target_hex

#: Bitcoin's genesis block, used as a fixed reference point in the tests.
BITCOIN_GENESIS_HEADER_HEX = (
    "01000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "3ba3edfd7a7b12b27ac72c3e67768f617fc81bc3888a51323a9fb8aa4b1e5e4a"
    "29ab5f49"
    "ffff001d"
    "1dac2b7c"
)
BITCOIN_GENESIS_HASH = "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f"


def target_to_nbits(target: str | int) -> int:
    """Pack a 256-bit target into Bitcoin's 32-bit compact form.

    The layout is one byte of exponent followed by three bytes of mantissa:
    ``target = mantissa * 256**(exponent - 3)``. It is a tiny floating-point
    format, which is why the target in a real block header is only ever an
    approximation of a round number.
    """
    value = int(normalize_target_hex(target), 16) if not isinstance(target, int) else target
    if value <= 0:
        return 0

    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    # The mantissa is signed in Bitcoin's encoding, so a leading byte >= 0x80
    # would read as negative. Pad to keep it positive, exactly as Bitcoin does.
    if raw[0] & 0x80:
        raw = b"\x00" + raw

    exponent = len(raw)
    mantissa_bytes = raw[:3]
    if len(mantissa_bytes) < 3:
        mantissa_bytes = mantissa_bytes + b"\x00" * (3 - len(mantissa_bytes))
    mantissa = int.from_bytes(mantissa_bytes, "big")
    return (exponent << 24) | mantissa


def nbits_to_target(nbits: int) -> str:
    """Unpack compact form back to a 64-character target."""
    nbits = int(nbits)
    exponent = nbits >> 24
    mantissa = nbits & 0x007FFFFF
    if nbits & 0x00800000:
        raise ValueError("negative nBits mantissa is not a valid target")
    if exponent <= 3:
        value = mantissa >> (8 * (3 - exponent))
    else:
        value = mantissa << (8 * (exponent - 3))
    if value > (1 << 256) - 1:
        raise ValueError("nBits decodes to a target larger than 256 bits")
    return normalize_target_hex(value)


def nbits_hex(nbits: int) -> str:
    return f"{int(nbits):08x}"


def _reversed_hash_bytes(value: str) -> bytes:
    """Hashes go on the wire in 'internal byte order': reversed from display.

    This is the single most confusing detail of Bitcoin's encoding, and the
    reason a block hash printed by a block explorer starts with zeros while the
    bytes in the header end with them.
    """
    text = str(value or "").strip().lower().removeprefix("0x")
    if len(text) != 64:
        text = text.rjust(64, "0")[:64]
    return bytes.fromhex(text)[::-1]


def serialize_header(header: dict[str, Any], nbits: int | None = None) -> bytes:
    """The genuine 80-byte Bitcoin block header.

    version (4, LE) | prev_hash (32, internal) | merkle_root (32, internal) |
    time (4, LE) | bits (4, LE) | nonce (4, LE)
    """
    if nbits is None:
        target = header.get("target")
        nbits = (
            target_to_nbits(target)
            if target is not None
            else target_to_nbits((1 << (256 - int(header.get("difficulty", 0)))) - 1)
        )
    return b"".join(
        [
            struct.pack("<I", int(header.get("version", 1)) & 0xFFFFFFFF),
            _reversed_hash_bytes(header.get("prev_hash", "0" * 64)),
            _reversed_hash_bytes(header.get("merkle_root", "0" * 64)),
            struct.pack("<I", int(header.get("timestamp", 0)) & 0xFFFFFFFF),
            struct.pack("<I", int(nbits) & 0xFFFFFFFF),
            struct.pack("<I", int(header.get("nonce", 0)) & 0xFFFFFFFF),
        ]
    )


def bitcoin_style_hash(header_bytes: bytes) -> str:
    """Double SHA256, reversed for display -- how Bitcoin names a block."""
    digest = hashlib.sha256(hashlib.sha256(header_bytes).digest()).digest()
    return digest[::-1].hex()


def describe_header(header: dict[str, Any]) -> dict[str, Any]:
    """Field-by-field breakdown for the explorer panel."""
    target = header.get("target")
    nbits = (
        target_to_nbits(target)
        if target is not None
        else target_to_nbits((1 << (256 - int(header.get("difficulty", 0)))) - 1)
    )
    raw = serialize_header(header, nbits)

    fields = [
        {
            "name": "version",
            "bytes": 4,
            "hex": raw[0:4].hex(),
            "value": str(header.get("version", 1)),
            "note": "小端序 32 位整数",
        },
        {
            "name": "prev_hash",
            "bytes": 32,
            "hex": raw[4:36].hex(),
            "value": str(header.get("prev_hash", "")),
            "note": "字节序与显示相反（internal byte order）",
        },
        {
            "name": "merkle_root",
            "bytes": 32,
            "hex": raw[36:68].hex(),
            "value": str(header.get("merkle_root", "")),
            "note": "同样是反转字节序",
        },
        {
            "name": "time",
            "bytes": 4,
            "hex": raw[68:72].hex(),
            "value": str(header.get("timestamp", 0)),
            "note": "Unix 秒，小端序",
        },
        {
            "name": "bits",
            "bytes": 4,
            "hex": raw[72:76].hex(),
            "value": nbits_hex(nbits),
            "note": "目标值的压缩表示：1 字节指数 + 3 字节尾数",
        },
        {
            "name": "nonce",
            "bytes": 4,
            "hex": raw[76:80].hex(),
            "value": str(header.get("nonce", 0)),
            "note": "矿工唯一能自由改动的字段，只有 32 位",
        },
    ]

    return {
        "size": len(raw),
        "hex": raw.hex(),
        "fields": fields,
        "nbits": nbits,
        "nbits_hex": nbits_hex(nbits),
        "nbits_target": nbits_to_target(nbits),
        "simulator_target": normalize_target_hex(target) if target is not None else None,
        "bitcoin_style_hash": bitcoin_style_hash(raw),
        "note": (
            "这是按真实比特币规则序列化出来的 80 字节区块头。本模拟器实际使用的区块 hash "
            "是 canonical JSON 的双 SHA256，与这里的数值不同——这样区块头可读，"
            "学生能直接看到哈希的输入是什么。这一面板只用于展示真实格式。"
        ),
    }
