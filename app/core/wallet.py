from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from ecdsa import BadSignatureError, SECP256k1, SigningKey, VerifyingKey


@dataclass(frozen=True)
class WalletRecord:
    name: str
    address: str
    public_key: str
    private_key: str
    created_at: int
    is_default: bool = True


def address_from_public_key(public_key_hex: str) -> str:
    """In this simulator the wallet address is the uncompressed public key hex.

    This mirrors the hand-written design note "address equals public key" and
    keeps signature validation transparent for teaching. It is not a real BTC
    address format.
    """
    return public_key_hex


def generate_wallet(name: str = "default") -> WalletRecord:
    signing_key = SigningKey.generate(curve=SECP256k1)
    verifying_key = signing_key.verifying_key
    public_key = verifying_key.to_string().hex()
    private_key = signing_key.to_string().hex()
    return WalletRecord(
        name=name,
        address=address_from_public_key(public_key),
        public_key=public_key,
        private_key=private_key,
        created_at=int(time.time()),
        is_default=True,
    )


def wallet_from_private_key(private_key_hex: str, name: str = "imported") -> WalletRecord:
    """Rebuild a wallet from its private key.

    This is what makes "your keys are your coins" demonstrable: a student can
    export the key, wipe the node, import the key on another machine and find
    the same balance waiting on chain.
    """
    cleaned = str(private_key_hex or "").strip().lower().removeprefix("0x")
    try:
        raw = bytes.fromhex(cleaned)
    except ValueError as exc:
        raise ValueError("private key must be hexadecimal") from exc
    if len(raw) != 32:
        raise ValueError("private key must be 32 bytes (64 hex characters)")
    try:
        signing_key = SigningKey.from_string(raw, curve=SECP256k1)
    except Exception as exc:  # ecdsa raises several types for bad scalars
        raise ValueError("private key is not a valid secp256k1 scalar") from exc
    public_key = signing_key.verifying_key.to_string().hex()
    return WalletRecord(
        name=name,
        address=address_from_public_key(public_key),
        public_key=public_key,
        private_key=cleaned,
        created_at=int(time.time()),
        is_default=True,
    )


def sign_payload(private_key_hex: str, payload: bytes) -> str:
    signing_key = SigningKey.from_string(bytes.fromhex(private_key_hex), curve=SECP256k1)
    return signing_key.sign_deterministic(payload, hashfunc=hashlib.sha256).hex()


def verify_signature(public_key_hex: str, signature_hex: str, payload: bytes) -> bool:
    try:
        verifying_key = VerifyingKey.from_string(bytes.fromhex(public_key_hex), curve=SECP256k1)
        return verifying_key.verify(
            bytes.fromhex(signature_hex),
            payload,
            hashfunc=hashlib.sha256,
        )
    except (BadSignatureError, ValueError):
        return False
