"""Real Bitcoin header serialization and nBits.

The strongest possible check is available here: Bitcoin's genesis block header
is a fixed 80 bytes whose hash everyone already knows. If the serializer is
even one byte or one endianness wrong, that hash does not come out.
"""

from __future__ import annotations

import pytest

from app.core.block import difficulty_to_target, normalize_target_hex
from app.core.btc_format import (
    BITCOIN_GENESIS_HASH,
    BITCOIN_GENESIS_HEADER_HEX,
    bitcoin_style_hash,
    describe_header,
    nbits_hex,
    nbits_to_target,
    serialize_header,
    target_to_nbits,
)

# --------------------------------------------------------------------------
# nBits compact encoding
# --------------------------------------------------------------------------


def test_bitcoin_minimum_difficulty_nbits():
    """0x1d00ffff is the value in Bitcoin's genesis block."""
    target = nbits_to_target(0x1D00FFFF)
    assert target == (
        "00000000ffff0000000000000000000000000000000000000000000000000000"
    )
    assert target_to_nbits(target) == 0x1D00FFFF


@pytest.mark.parametrize(
    "nbits",
    [0x1D00FFFF, 0x1B0404CB, 0x170ED0EB, 0x1A05DB8B, 0x03123456, 0x04923456 & ~0x00800000],
)
def test_nbits_round_trips(nbits: int):
    target = nbits_to_target(nbits)
    assert target_to_nbits(target) == nbits


def test_encoding_pads_when_the_mantissa_would_look_negative():
    """A leading byte >= 0x80 must be padded, or it reads as a negative value."""
    target = normalize_target_hex(0x80 << 248)
    nbits = target_to_nbits(target)
    assert not nbits & 0x00800000, "sign bit must never be set"
    assert nbits_to_target(nbits) == target


def test_negative_mantissa_is_rejected():
    with pytest.raises(ValueError):
        nbits_to_target(0x01800000)


def test_simulator_targets_survive_the_round_trip():
    """Every leading-zero-bit target the simulator can produce."""
    for bits in range(0, 240):
        target = difficulty_to_target(bits)
        nbits = target_to_nbits(target)
        # The compact form is lossy by design; it must never round *up*,
        # which would make a block easier than the rule says.
        assert int(nbits_to_target(nbits), 16) <= int(target, 16)


def test_zero_target_encodes_to_zero():
    assert target_to_nbits(0) == 0


def test_nbits_hex_is_eight_characters():
    assert nbits_hex(0x1D00FFFF) == "1d00ffff"
    assert len(nbits_hex(1)) == 8


# --------------------------------------------------------------------------
# 80-byte header
# --------------------------------------------------------------------------


def test_bitcoin_genesis_header_serializes_byte_for_byte():
    genesis = {
        "version": 1,
        "prev_hash": "0" * 64,
        "merkle_root": (
            "4a5e1e4baab89f3a32518a88c31bc87f618f76673e2cc77ab2127b7afdeda33b"
        ),
        "timestamp": 1231006505,
        "nonce": 2083236893,
    }
    raw = serialize_header(genesis, nbits=0x1D00FFFF)
    assert len(raw) == 80
    assert raw.hex() == BITCOIN_GENESIS_HEADER_HEX


def test_bitcoin_genesis_header_hashes_to_the_known_block_hash():
    """The end-to-end proof that the byte layout is right."""
    raw = bytes.fromhex(BITCOIN_GENESIS_HEADER_HEX)
    assert bitcoin_style_hash(raw) == BITCOIN_GENESIS_HASH


def test_header_is_always_eighty_bytes():
    header = {
        "version": 1,
        "prev_hash": "ab" * 32,
        "merkle_root": "cd" * 32,
        "timestamp": 1700000000,
        "nonce": 42,
        "target": difficulty_to_target(20),
    }
    assert len(serialize_header(header)) == 80


def test_hashes_are_written_in_internal_byte_order():
    header = {
        "version": 1,
        "prev_hash": "00" * 31 + "ff",
        "merkle_root": "0" * 64,
        "timestamp": 0,
        "nonce": 0,
        "target": difficulty_to_target(1),
    }
    raw = serialize_header(header)
    # display order ends with ff, so the wire bytes must *start* with it
    assert raw[4] == 0xFF
    assert raw[5:36] == b"\x00" * 31


def test_describe_header_breaks_the_bytes_down(monkeypatch=None):
    header = {
        "version": 1,
        "prev_hash": "11" * 32,
        "merkle_root": "22" * 32,
        "timestamp": 1700000000,
        "nonce": 99,
        "target": difficulty_to_target(16),
    }
    described = describe_header(header)

    assert described["size"] == 80
    assert len(described["hex"]) == 160
    assert [field["name"] for field in described["fields"]] == [
        "version",
        "prev_hash",
        "merkle_root",
        "time",
        "bits",
        "nonce",
    ]
    assert sum(field["bytes"] for field in described["fields"]) == 80
    # the pieces must concatenate back into the whole header
    assert "".join(field["hex"] for field in described["fields"]) == described["hex"]
    assert described["nbits_hex"] == nbits_hex(described["nbits"])


def test_describe_header_does_not_claim_to_be_the_consensus_hash():
    header = {
        "version": 1,
        "prev_hash": "0" * 64,
        "merkle_root": "0" * 64,
        "timestamp": 1,
        "nonce": 1,
        "target": difficulty_to_target(4),
    }
    described = describe_header(header)
    from app.core.block import compute_block_hash

    # The panel is explicitly a teaching view; the simulator's own hash of the
    # same header is a different number and the note has to say so.
    assert described["bitcoin_style_hash"] != compute_block_hash({"header": header})
    assert "只用于展示" in described["note"]
