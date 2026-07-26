from __future__ import annotations

import copy
import json
import re
import secrets
import string
from pathlib import Path
from typing import Any

from app import __version__
from app.utils.crypto import hash_json


DEFAULT_CONFIG: dict[str, Any] = {
    "version": __version__,
    "network_id": "btc-sim-classroom",
    "node_name": "server1",
    "listen_ip": "0.0.0.0",
    "enable_ipv6": True,
    "advertise_ip": None,
    "advertise_ipv6": None,
    "listen_port": 7464,
    "web_host": "127.0.0.1",
    "web_port": 8000,
    "difficulty_mode": "binary_leading_zero",
    "difficulty": 12,
    "auto_difficulty": True,
    "target_block_seconds": 60,
    "difficulty_adjustment_interval": 10,
    "difficulty_adjustment_tolerance": 0.25,
    "min_difficulty": 0,
    "max_difficulty": 255,
    "mining_reward": 50.0,
    # Bitcoin halves every 210,000 blocks (~4 years). A classroom needs to see
    # several halvings inside one lesson, so the default window is tiny. The
    # supply cap follows from it: sum(interval * reward / 2**era) until the
    # subsidy rounds to zero.
    "halving_interval": 20,
    # Blocks a coinbase output must be buried under before it can be spent.
    # Real Bitcoin uses 100; the point is that mining income is not instantly
    # spendable, because a reorg can undo it.
    "coinbase_maturity": 5,
    "max_block_transactions": 100,
    "mempool_max_bytes": 314572800,
    "mempool_expiry_seconds": 3600,
    "min_relay_fee": 0.0,
    "sync_interval_seconds": 10,
    "peer_connect_timeout_seconds": 5,
    "trust_loopback_admin": True,
    "require_admin_for_writes": True,
    "servers": [
        ["127.0.0.1", 7464],
        ["127.0.0.1", 7465],
        ["127.0.0.1", 7466],
    ],
    "storage": {
        "type": "sqlite",
        "path": "./data/blockchain.db",
    },
}

CONSENSUS_PARAM_KEYS = (
    "difficulty_mode",
    "difficulty",
    "auto_difficulty",
    "target_block_seconds",
    "difficulty_adjustment_interval",
    "difficulty_adjustment_tolerance",
    "min_difficulty",
    "max_difficulty",
    "mining_reward",
    "halving_interval",
    "coinbase_maturity",
    "max_block_transactions",
)


def consensus_params(config: dict[str, Any]) -> dict[str, Any]:
    return {key: config.get(key, DEFAULT_CONFIG[key]) for key in CONSENSUS_PARAM_KEYS}


def chain_params_hash(config: dict[str, Any]) -> str:
    return hash_json(consensus_params(config))


def sanitize_node_name(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "").strip())
    cleaned = cleaned.strip("-")
    return cleaned[:32] or "node"


def random_node_name() -> str:
    alphabet = string.ascii_lowercase + string.digits
    suffix = "".join(secrets.choice(alphabet) for _ in range(12))
    return f"node-{suffix}"


def is_default_node_name(value: str) -> bool:
    name = str(value or "").strip().lower()
    return name == "default"


def network_identity(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "network_id": str(config.get("network_id", DEFAULT_CONFIG["network_id"])),
        "chain_params_hash": chain_params_hash(config),
        "consensus_params": consensus_params(config),
    }


def _merge_defaults(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_defaults(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(config_path: str | Path = "config.json") -> dict[str, Any]:
    path = Path(config_path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    raw = json.loads(path.read_text(encoding="utf-8"))
    config = _merge_defaults(DEFAULT_CONFIG, raw)
    config["_config_path"] = str(path.resolve())
    config["_config_dir"] = str(path.resolve().parent)
    return config


def save_config(config: dict[str, Any]) -> None:
    """Persist public configuration values back to the active config file."""
    path = Path(config.get("_config_path", "config.json"))
    public_config = {
        key: value
        for key, value in config.items()
        if not key.startswith("_")
    }
    path.write_text(
        json.dumps(public_config, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def resolve_project_path(config: dict[str, Any], value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (Path(config.get("_config_dir", ".")).resolve() / path).resolve()
