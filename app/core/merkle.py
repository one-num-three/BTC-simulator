from __future__ import annotations

from typing import Any

from app.utils.crypto import sha256_hex

EMPTY_MERKLE_ROOT = "0" * 64


def _combine(left: str, right: str) -> str:
    try:
        payload = bytes.fromhex(left) + bytes.fromhex(right)
    except ValueError:
        payload = f"{left}{right}".encode("utf-8")
    return sha256_hex(payload)


def _build_layers(tx_ids: list[str]) -> list[list[str]]:
    """Every level of the tree, leaves first, root last.

    The odd-node case duplicates the last hash, exactly as Bitcoin does. That
    duplication is the root of CVE-2012-2459 (two different transaction lists
    producing the same root); the simulator is safe from it only because
    ``Blockchain.validate_block`` rejects a block containing duplicate tx_ids.
    """
    if not tx_ids:
        return [[EMPTY_MERKLE_ROOT]]
    layers = [list(tx_ids)]
    layer = list(tx_ids)
    while len(layer) > 1:
        if len(layer) % 2 == 1:
            layer.append(layer[-1])
            layers[-1] = list(layer)
        layer = [_combine(layer[i], layer[i + 1]) for i in range(0, len(layer), 2)]
        layers.append(list(layer))
    return layers


def merkle_root(tx_ids: list[str]) -> str:
    if not tx_ids:
        return EMPTY_MERKLE_ROOT
    return _build_layers(tx_ids)[-1][0]


def merkle_layers(tx_ids: list[str]) -> list[list[str]]:
    """Public view of the tree, used to draw it in the console."""
    return _build_layers(tx_ids)


def merkle_proof(tx_ids: list[str], index: int) -> list[dict[str, str]]:
    """The sibling hashes needed to walk one leaf up to the root.

    This is the whole of SPV in one function: a light client holding only the
    block headers can be convinced a transaction is in a block by replaying
    ``len(proof)`` hashes, without downloading the block. For a 1000-transaction
    block that is 10 hashes instead of the entire body.
    """
    if not tx_ids:
        raise ValueError("cannot prove membership in an empty tree")
    if not 0 <= index < len(tx_ids):
        raise IndexError("transaction index is outside the block")

    layers = _build_layers(tx_ids)
    proof: list[dict[str, str]] = []
    position = index
    for layer in layers[:-1]:
        sibling = position ^ 1
        if sibling >= len(layer):
            sibling = position
        proof.append(
            {
                "hash": layer[sibling],
                "position": "left" if sibling < position else "right",
            }
        )
        position //= 2
    return proof


def verify_merkle_proof(leaf: str, proof: list[dict[str, Any]], root: str) -> bool:
    """Replay a proof. Returns True when it lands on the expected root."""
    current = leaf
    for step in proof:
        sibling = str(step["hash"])
        if str(step.get("position")) == "left":
            current = _combine(sibling, current)
        else:
            current = _combine(current, sibling)
    return current == root


def proof_steps(leaf: str, proof: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Intermediate hashes of a proof replay, for the classroom walkthrough."""
    steps: list[dict[str, str]] = []
    current = leaf
    for step in proof:
        sibling = str(step["hash"])
        left, right = (
            (sibling, current) if str(step.get("position")) == "left" else (current, sibling)
        )
        current = _combine(left, right)
        steps.append({"left": left, "right": right, "parent": current})
    return steps
