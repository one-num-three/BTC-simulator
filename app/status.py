"""Machine-readable state values shared by storage, P2P and the Web API.

These used to be Chinese display strings that were written into SQLite, sent
over the wire in HELLO, and then compared with ``==`` in Python. That made the
node's behaviour depend on its UI language: renaming a label silently broke
peer filtering, and translating the console was impossible.

The values here are the contract. Anything the user reads is looked up from
``app/web/static/app.js`` (see ``STATUS_LABELS``), so the two can change
independently.
"""

from __future__ import annotations


class PeerStatus:
    CONNECTED = "connected"
    OFFLINE = "offline"
    KNOWN = "known"
    PARAM_MISMATCH = "param_mismatch"
    SELF_CONNECTION = "self_connection"
    DUPLICATE_ADDRESS = "duplicate_address"
    BANNED = "banned"
    SELF = "self"


class MinerStatus:
    IDLE = "idle"
    MINING = "mining"
    PAUSED = "paused"


#: Values written by builds that stored Chinese labels, mapped to the new ones
#: so an existing classroom database keeps working after an upgrade.
LEGACY_PEER_STATUS = {
    "参数不匹配": PeerStatus.PARAM_MISMATCH,
    "self connection": PeerStatus.SELF_CONNECTION,
    "duplicate peer address": PeerStatus.DUPLICATE_ADDRESS,
    "本机": PeerStatus.SELF,
}

LEGACY_MINER_STATUS = {
    "未挖矿": MinerStatus.IDLE,
    "正在挖矿": MinerStatus.MINING,
    "暂停": MinerStatus.PAUSED,
}


def normalize_peer_status(value: str | None) -> str | None:
    if value is None:
        return None
    return LEGACY_PEER_STATUS.get(str(value), str(value))


def normalize_miner_status(value: str | None) -> str | None:
    if value is None:
        return None
    return LEGACY_MINER_STATUS.get(str(value), str(value))
