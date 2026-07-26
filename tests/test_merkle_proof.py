"""Merkle inclusion proofs -- the whole of SPV in one function.

``merkle.py`` used to compute a root and nothing else, so the console could
show students a merkle_root field with no way to demonstrate what it is for.
"""

from __future__ import annotations

import pytest

from app.core.merkle import (
    EMPTY_MERKLE_ROOT,
    merkle_layers,
    merkle_proof,
    merkle_root,
    proof_steps,
    verify_merkle_proof,
)


def leaves(count: int) -> list[str]:
    return [f"{index:064x}" for index in range(1, count + 1)]


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5, 7, 8, 9, 16, 17, 33])
def test_every_leaf_can_prove_itself(count: int):
    tx_ids = leaves(count)
    root = merkle_root(tx_ids)
    for index, leaf in enumerate(tx_ids):
        proof = merkle_proof(tx_ids, index)
        assert verify_merkle_proof(leaf, proof, root), f"{count} leaves, index {index}"


def test_proof_is_logarithmic_not_linear():
    """The point of the structure: 1024 transactions, 10 hashes."""
    tx_ids = leaves(1024)
    assert len(merkle_proof(tx_ids, 500)) == 10
    assert len(merkle_proof(leaves(8), 0)) == 3
    assert len(merkle_proof(leaves(1), 0)) == 0


def test_wrong_leaf_does_not_verify():
    tx_ids = leaves(8)
    root = merkle_root(tx_ids)
    proof = merkle_proof(tx_ids, 3)
    assert verify_merkle_proof(tx_ids[3], proof, root)
    assert not verify_merkle_proof(tx_ids[4], proof, root)
    assert not verify_merkle_proof("ff" * 32, proof, root)


def test_tampered_proof_does_not_verify():
    tx_ids = leaves(8)
    root = merkle_root(tx_ids)
    proof = merkle_proof(tx_ids, 2)

    flipped = [dict(step) for step in proof]
    flipped[0]["hash"] = "00" * 32
    assert not verify_merkle_proof(tx_ids[2], flipped, root)

    swapped = [dict(step) for step in proof]
    swapped[0]["position"] = "left" if swapped[0]["position"] == "right" else "right"
    assert not verify_merkle_proof(tx_ids[2], swapped, root)


def test_proof_steps_land_on_the_root():
    tx_ids = leaves(5)
    root = merkle_root(tx_ids)
    proof = merkle_proof(tx_ids, 4)
    steps = proof_steps(tx_ids[4], proof)
    assert len(steps) == len(proof)
    assert steps[-1]["parent"] == root


def test_odd_layers_duplicate_the_last_hash():
    tx_ids = leaves(3)
    layers = merkle_layers(tx_ids)
    # bottom layer is padded to an even width by repeating the last leaf
    assert layers[0] == [tx_ids[0], tx_ids[1], tx_ids[2], tx_ids[2]]
    assert layers[-1] == [merkle_root(tx_ids)]


def test_empty_tree_is_handled():
    assert merkle_root([]) == EMPTY_MERKLE_ROOT
    with pytest.raises(ValueError):
        merkle_proof([], 0)


def test_index_outside_the_block_is_rejected():
    with pytest.raises(IndexError):
        merkle_proof(leaves(4), 4)
    with pytest.raises(IndexError):
        merkle_proof(leaves(4), -1)


def test_root_is_unchanged_by_the_refactor():
    """The proof rewrite must not alter any historical block's merkle_root."""

    def legacy_root(tx_ids: list[str]) -> str:
        from app.utils.crypto import sha256_hex

        if not tx_ids:
            return EMPTY_MERKLE_ROOT
        layer = list(tx_ids)
        while len(layer) > 1:
            if len(layer) % 2 == 1:
                layer.append(layer[-1])
            nxt = []
            for i in range(0, len(layer), 2):
                left, right = layer[i], layer[i + 1]
                try:
                    payload = bytes.fromhex(left) + bytes.fromhex(right)
                except ValueError:
                    payload = f"{left}{right}".encode("utf-8")
                nxt.append(sha256_hex(payload))
            layer = nxt
        return layer[0]

    for count in (0, 1, 2, 3, 5, 8, 13, 21):
        tx_ids = leaves(count)
        assert merkle_root(tx_ids) == legacy_root(tx_ids)


def test_non_hex_leaves_still_work():
    tx_ids = ["not-hex-a", "not-hex-b", "not-hex-c"]
    root = merkle_root(tx_ids)
    for index, leaf in enumerate(tx_ids):
        assert verify_merkle_proof(leaf, merkle_proof(tx_ids, index), root)
