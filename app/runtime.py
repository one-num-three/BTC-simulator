from __future__ import annotations

import asyncio
import json
import socket
import time
from collections import deque
from pathlib import Path
from typing import Any

from app.config import (
    DEFAULT_CONFIG,
    network_identity,
    resolve_project_path,
    save_config,
)
from app.core.block import target_preview
from app.core.blockchain import Blockchain
from app.core.mempool import Mempool
from app.core.merkle import merkle_proof, proof_steps, verify_merkle_proof
from app.core.miner import Miner
from app.core.transaction import create_transfer, estimate_transaction_bytes
from app.core.wallet import generate_wallet, wallet_from_private_key
from app.network.address import (
    configured_listen_hosts,
    format_host_port,
    format_url_host,
    is_ipv6_host,
    normalize_host,
    parse_host_port,
)
from app.network.node import P2PNode
from app.status import PeerStatus
from app.storage.sqlite_store import SQLiteStore


DEFAULT_LAB_TASKS: list[dict[str, Any]] = [
    {
        "id": "wallet",
        "title": "拿到自己的钱包地址",
        "detail": "在“接收”页复制地址，这串公钥就是你在这条链上的身份。",
        "check": "has_wallet",
    },
    {
        "id": "peers",
        "title": "连上至少一个同学的节点",
        "detail": "在“节点”页填对方的 IP 和 P2P 端口，或用“入网”页的地址。",
        "check": "has_peer",
    },
    {
        "id": "mining",
        "title": "开始挖矿并挖到第一个区块",
        "detail": "观察 nonce 怎么一个个试，以及 hash 什么时候小于目标值。",
        "check": "has_block",
    },
    {
        "id": "balance",
        "title": "拿到第一笔挖矿奖励",
        "detail": "注意奖励要等成熟期过去才能花，这就是重组保护。",
        "check": "has_balance",
    },
    {
        "id": "mempool",
        "title": "发一笔交易并在内存池里看到它",
        "detail": "交易先进内存池，被打包进区块才算确认。",
        "check": "has_tx",
    },
    {
        "id": "sync",
        "title": "和同学的链保持同一个高度",
        "detail": "如果分叉了，观察累计工作量更大的那条链怎么赢。",
        "check": "in_sync",
    },
]

_LAB_CHECKS = {
    "has_wallet": lambda s: True,
    "has_peer": lambda s: s["peers"] > 0,
    "has_block": lambda s: s["height"] > 0,
    "has_balance": lambda s: s["balance"] > 0,
    "has_tx": lambda s: s["mempool"] > 0 or s["tx_count"] > 0,
    "in_sync": lambda s: s["height"] > 0 and s["peers"] > 0,
}


def _load_lab_tasks(config: dict[str, Any]) -> list[dict[str, Any]]:
    path = Path(config.get("_config_dir", ".")) / "lab_tasks.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                return [dict(item) for item in data]
        except (json.JSONDecodeError, OSError, TypeError):
            pass
    return [dict(task) for task in DEFAULT_LAB_TASKS]


def _direction(record: dict[str, Any], address: str) -> str:
    if record.get("type") == "coinbase":
        return "mined"
    if record.get("sender") == address:
        return "out"
    if record.get("receiver") == address:
        return "in"
    return "other"


def _fee_buckets(transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group pending transactions by fee rate so congestion is visible."""
    buckets = [
        {"label": "0", "min": 0.0, "count": 0, "bytes": 0},
        {"label": "0-0.01", "min": 1e-12, "count": 0, "bytes": 0},
        {"label": "0.01-0.1", "min": 0.01, "count": 0, "bytes": 0},
        {"label": "0.1-1", "min": 0.1, "count": 0, "bytes": 0},
        {"label": "1+", "min": 1.0, "count": 0, "bytes": 0},
    ]
    for tx in transactions:
        fee = float(tx.get("fee", 0.0))
        size = estimate_transaction_bytes(tx)
        chosen = buckets[0]
        for bucket in buckets:
            if fee >= bucket["min"]:
                chosen = bucket
        chosen["count"] += 1
        chosen["bytes"] += size
    return buckets


class EventLog:
    def __init__(self, maxlen: int = 300):
        self.items: deque[dict[str, Any]] = deque(maxlen=maxlen)

    def add(self, message: str, **fields: Any) -> None:
        item = {"time": int(time.time()), "message": message}
        item.update(fields)
        self.items.appendleft(item)

    def recent(self, limit: int = 80) -> list[dict[str, Any]]:
        return list(self.items)[:limit]


class NodeService:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        storage_path = resolve_project_path(config, config["storage"]["path"])
        self.events = EventLog()
        self.security_events = EventLog(maxlen=500)
        self.store = SQLiteStore(storage_path)
        self.blockchain = Blockchain(config, self.store, self.log)
        self._ensure_default_wallet()
        self.mempool = Mempool(self.store, self.blockchain, self.log)
        self.p2p = P2PNode(config, self, self.log)
        self.miner = Miner(self.blockchain, self.mempool, self.log, self._on_mined_block)
        self._sync_task: asyncio.Task[None] | None = None

    @property
    def sync_interval_seconds(self) -> int:
        return max(int(self.config.get("sync_interval_seconds", 10)), 1)

    def log(self, message: str) -> None:
        self.events.add(message)

    def record_security_event(
        self,
        event_type: str,
        source: str,
        reason: str,
        *,
        severity: str = "warning",
        peer_name: str | None = None,
        peer_ip: str | None = None,
        peer_port: int | None = None,
        wallet_address: str | None = None,
        tx_id: str | None = None,
        block_hash: str | None = None,
    ) -> None:
        label = peer_name or source or "unknown"
        message = f"{event_type} from {label}: {reason}"
        self.security_events.add(
            message,
            type=event_type,
            severity=severity,
            source=source,
            reason=reason,
            peer_name=peer_name,
            peer_ip=peer_ip,
            peer_port=peer_port,
            wallet_address=wallet_address,
            tx_id=tx_id,
            block_hash=block_hash,
        )
        self.log(f"Security alert: {message}")

    def _route_ip(self, family: socket.AddressFamily, target: str) -> str | None:
        """Best-effort "which local address would I use to reach X".

        The socket is created *inside* the try on purpose: on a host with no
        IPv6 stack (CI containers, some school networks, IPv6 disabled in the
        kernel) ``socket.socket(AF_INET6, ...)`` itself raises ``OSError:
        Address family not supported by protocol``. That used to escape and
        take down every caller of ``network_info()`` -- which is the join page,
        the status endpoint and the teacher page.
        """
        sock: socket.socket | None = None
        try:
            sock = socket.socket(family, socket.SOCK_DGRAM)
            if family == socket.AF_INET6:
                sock.connect((target, 80, 0, 0))
            else:
                sock.connect((target, 80))
            return normalize_host(sock.getsockname()[0])
        except OSError:
            return None
        finally:
            if sock is not None:
                sock.close()

    def _lan_ip(self) -> str:
        return self._route_ip(socket.AF_INET, "8.8.8.8") or "127.0.0.1"

    def _lan_ipv6(self) -> str | None:
        if not socket.has_ipv6:
            return None
        return self._route_ip(socket.AF_INET6, "2001:4860:4860::8888")

    def _ensure_default_wallet(self) -> None:
        if self.store.get_default_wallet():
            return
        wallet = generate_wallet("default")
        self.store.save_wallet(wallet, make_default=True)
        self.log(f"Wallet generated: {wallet.address[:16]}")

    async def start(self) -> None:
        await self.p2p.start()
        self._sync_task = asyncio.create_task(self._sync_loop(), name="btc-sim-sync")

    async def shutdown(self) -> None:
        if self._sync_task is not None:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except BaseException:
                pass
            self._sync_task = None
        await self.miner.stop()
        await self.p2p.stop()
        self.store.close()

    async def _sync_loop(self) -> None:
        """Keep the node converging without anyone pressing a button.

        ``sync_interval_seconds`` has been in every config file since the first
        commit but nothing ever read it, so a node that lost a mining race or
        missed a broadcast stayed behind until a human clicked "sync". Each tick
        does three cheap things: ping live peers so ``last_seen`` stays honest,
        retry peers we know but are not connected to, and pull the chain from
        any peer reporting more cumulative work than us.
        """
        interval = self.sync_interval_seconds
        while True:
            try:
                await asyncio.sleep(interval)
                await self.p2p.ping_peers()
                await self.p2p.reconnect_known_peers()
                pulled = await self.p2p.request_blocks_from_better_peers()
                if pulled:
                    self.log(f"Auto-sync requested chain from {pulled} peer(s)")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never let one bad tick kill the loop
                self.log(f"Auto-sync tick failed: {exc}")

    async def _on_mined_block(self, block: dict[str, Any], block_hash: str) -> None:
        await self.p2p.broadcast_block(block)

    def default_wallet(self) -> dict[str, Any]:
        wallet = self.store.get_default_wallet()
        if not wallet:
            self._ensure_default_wallet()
            wallet = self.store.get_default_wallet()
        if not wallet:
            raise RuntimeError("wallet unavailable")
        return wallet

    def generate_new_wallet(self, name: str = "default") -> dict[str, Any]:
        wallet = generate_wallet(name)
        self.store.save_wallet(wallet, make_default=True)
        self.log(f"Wallet generated: {wallet.address[:16]}")
        return self.default_wallet()

    # ------------------------------------------------------------------
    # wallets
    #
    # There used to be exactly one usable wallet: generating a new one made the
    # old key unreachable from the console even though its coins were still on
    # chain, and there was no way to back a key up or move it to another node.
    # ------------------------------------------------------------------

    def _wallet_view(self, wallet: dict[str, Any]) -> dict[str, Any]:
        address = wallet["address"]
        return {
            "name": wallet["name"],
            "address": address,
            "public_key": wallet["public_key"],
            "created_at": wallet["created_at"],
            "is_default": bool(wallet["is_default"]),
            "balance": self.blockchain.get_balance(address),
            "spendable": self.blockchain.spendable_balance(address),
            "immature": self.blockchain.immature_balance(address),
            "available": self.blockchain.get_available_balance(address),
        }

    def list_wallets(self) -> list[dict[str, Any]]:
        return [self._wallet_view(wallet) for wallet in self.store.list_wallets()]

    def select_wallet(self, address: str) -> dict[str, Any]:
        if not self.store.set_default_wallet(address):
            raise ValueError("wallet not found on this node")
        self.log(f"Active wallet switched to {address[:16]}")
        return self._wallet_view(self.default_wallet())

    def import_wallet(
        self, private_key: str, name: str = "imported", make_default: bool = True
    ) -> dict[str, Any]:
        wallet = wallet_from_private_key(private_key, name)
        if self.store.get_wallet(wallet.address):
            if make_default:
                self.store.set_default_wallet(wallet.address)
            self.log(f"Wallet already present: {wallet.address[:16]}")
            return self._wallet_view(self.store.get_wallet(wallet.address) or {})
        self.store.save_wallet(wallet, make_default=make_default)
        self.log(f"Wallet imported: {wallet.address[:16]}")
        return self._wallet_view(self.store.get_wallet(wallet.address) or {})

    def export_wallet(self, address: str | None = None) -> dict[str, Any]:
        wallet = self.store.get_wallet(address) if address else self.default_wallet()
        if not wallet:
            raise ValueError("wallet not found on this node")
        return {
            "name": wallet["name"],
            "address": wallet["address"],
            "public_key": wallet["public_key"],
            "private_key": wallet["private_key"],
            "created_at": wallet["created_at"],
            "warning": (
                "This key is stored and exported in plain text. It exists to "
                "teach how key ownership works and must never hold real value."
            ),
        }

    # ------------------------------------------------------------------
    # transaction lookup and search
    # ------------------------------------------------------------------

    def transaction_history(
        self,
        address: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit = min(max(int(limit), 1), 200)
        offset = max(int(offset), 0)
        wallet_address = self.default_wallet()["address"]
        target = address or wallet_address
        confirmed = self.store.list_transactions(target, limit=limit, offset=offset)
        pending = self.store.list_pending_transactions(target) if offset == 0 else []
        height = self.blockchain.height()
        for record in confirmed:
            block_height = record.get("block_height")
            record["confirmations"] = (
                height - int(block_height) + 1 if block_height is not None else 0
            )
            record["direction"] = _direction(record, target)
        for record in pending:
            record["confirmations"] = 0
            record["direction"] = _direction(record, target)
        return {
            "address": target,
            "is_own_wallet": target == wallet_address,
            "pending": pending,
            "transactions": confirmed,
            "total": self.store.count_transactions(target),
            "limit": limit,
            "offset": offset,
            "summary": self.store.address_summary(target),
        }

    def transaction_detail(self, tx_id: str) -> dict[str, Any] | None:
        record = self.store.get_transaction(tx_id) or self.store.get_mempool_transaction(tx_id)
        if record is None:
            return None
        block_height = record.get("block_height")
        record["confirmations"] = (
            self.blockchain.height() - int(block_height) + 1 if block_height is not None else 0
        )
        record["proof"] = self.merkle_proof(tx_id)
        return record

    def merkle_proof(self, tx_id: str) -> dict[str, Any] | None:
        """Everything a light client needs to be convinced this tx is in a block."""
        record = self.store.get_transaction(tx_id)
        if record is None or not record.get("block_hash"):
            return None
        block = self.store.get_block_json_by_hash(str(record["block_hash"]))
        if not block:
            return None
        tx_ids = [tx["tx_id"] for tx in block.get("transactions", [])]
        if tx_id not in tx_ids:
            return None
        index = tx_ids.index(tx_id)
        proof = merkle_proof(tx_ids, index)
        root = block["header"]["merkle_root"]
        return {
            "tx_id": tx_id,
            "block_hash": record["block_hash"],
            "block_height": record.get("block_height"),
            "index": index,
            "tx_count": len(tx_ids),
            "merkle_root": root,
            "proof": proof,
            "steps": proof_steps(tx_id, proof),
            "verified": verify_merkle_proof(tx_id, proof, root),
            "hashes_needed": len(proof),
            "hashes_avoided": max(len(tx_ids) - len(proof), 0),
        }

    def search(self, query: str) -> dict[str, Any]:
        """One box that finds a block, a transaction or an address.

        The explorer previously accepted only a block height or a block hash,
        so there was no way to look up a transaction you had just sent.
        """
        term = str(query or "").strip()
        if not term:
            return {"query": term, "kind": "empty", "results": []}

        if term.isdigit():
            block = self.store.get_block_detail(int(term))
            if block:
                return {"query": term, "kind": "block", "block": block}

        tx = self.transaction_detail(term)
        if tx:
            return {"query": term, "kind": "transaction", "transaction": tx}

        block = self.store.get_block_detail(term)
        if block:
            return {"query": term, "kind": "block", "block": block}

        summary = self.store.address_summary(term)
        if summary["received_count"] or summary["sent_count"]:
            return {
                "query": term,
                "kind": "address",
                "address": summary,
                "transactions": self.store.list_transactions(term, limit=25),
            }

        matches = self.store.find_addresses(term, limit=10)
        if matches:
            return {"query": term, "kind": "suggestions", "addresses": matches}
        return {"query": term, "kind": "not_found", "results": []}

    # ------------------------------------------------------------------
    # chart data
    # ------------------------------------------------------------------

    def chart_stats(self, window: int = 100) -> dict[str, Any]:
        window = min(max(int(window), 2), 500)
        blocks = self.store.block_series(limit=window)
        points: list[dict[str, Any]] = []
        intervals: list[int] = []
        cumulative = 0.0
        for index, row in enumerate(blocks):
            interval = None
            # Genesis carries timestamp 0, so the gap between it and block 1 is
            # "seconds since 1970" and would swamp every average it touches.
            if index > 0 and int(blocks[index - 1]["height"]) > 0:
                interval = max(int(row["timestamp"]) - int(blocks[index - 1]["timestamp"]), 0)
                intervals.append(interval)
            difficulty = int(row["difficulty"])
            cumulative = round(cumulative + float(row["coinbase_amount"]), 8)
            points.append(
                {
                    "height": int(row["height"]),
                    "timestamp": int(row["timestamp"]),
                    "difficulty": difficulty,
                    "interval": interval,
                    "tx_count": int(row["tx_count"]),
                    "fees": round(float(row["fees"]), 8),
                    "subsidy": round(float(row["coinbase_amount"]) - float(row["fees"]), 8),
                    "issued": cumulative,
                    # Estimated hashes spent on this block: 2^difficulty attempts
                    # on average, spread over the observed interval.
                    "hashrate": (
                        round((2**difficulty) / interval, 2)
                        if interval and interval > 0
                        else None
                    ),
                }
            )
        average_interval = round(sum(intervals) / len(intervals), 2) if intervals else None
        mempool = self.mempool.ordered()
        return {
            "window": window,
            "blocks": points,
            "average_interval": average_interval,
            "target_block_seconds": self.blockchain.target_block_seconds,
            "estimated_hashrate": (
                round((2 ** self.blockchain.expected_difficulty()) / average_interval, 2)
                if average_interval and average_interval > 0
                else None
            ),
            "supply": self.blockchain.supply_policy(),
            "mempool_fee_buckets": _fee_buckets(mempool),
        }

    # ------------------------------------------------------------------
    # peer management
    # ------------------------------------------------------------------

    async def forget_peer(self, ip: str, port: int) -> dict[str, Any]:
        host = normalize_host(ip)
        await self.p2p.disconnect_peer(host, int(port))
        self.store.delete_peer(host, int(port))
        self.log(f"Peer removed: {format_host_port(host, int(port))}")
        return {"removed": True, "ip": host, "port": int(port)}

    async def ban_peer(self, ip: str, port: int) -> dict[str, Any]:
        host = normalize_host(ip)
        self.store.ban_peer(host, int(port), "banned from the console", int(time.time()))
        await self.p2p.disconnect_peer(host, int(port))
        self.store.upsert_peer(
            host,
            int(port),
            None,
            None,
            "outbound",
            PeerStatus.BANNED,
            int(time.time()),
        )
        self.log(f"Peer banned: {format_host_port(host, int(port))}")
        return {"banned": True, "ip": host, "port": int(port)}

    def unban_peer(self, ip: str, port: int) -> dict[str, Any]:
        host = normalize_host(ip)
        self.store.unban_peer(host, int(port))
        self.log(f"Peer unbanned: {format_host_port(host, int(port))}")
        return {"banned": False, "ip": host, "port": int(port)}

    # ------------------------------------------------------------------
    # classroom lab sheet
    # ------------------------------------------------------------------

    def lab_tasks(self) -> dict[str, Any]:
        """Progress on the lab checklist, evaluated on the server.

        The task list used to be hardcoded in the browser, so a teacher could
        not change it without editing JavaScript. It now comes from
        ``lab_tasks.json`` next to the config file when that file exists.
        """
        status = self.status_summary()
        tasks = _load_lab_tasks(self.config)
        for task in tasks:
            key = str(task.get("check") or "")
            task["done"] = bool(_LAB_CHECKS.get(key, lambda _s: False)(status))
        return {
            "tasks": tasks,
            "completed": sum(1 for task in tasks if task["done"]),
            "total": len(tasks),
        }

    def status_summary(self) -> dict[str, Any]:
        wallet = self.default_wallet()
        return {
            "height": self.blockchain.height(),
            "balance": self.blockchain.get_balance(wallet["address"]),
            "peers": self.p2p.connection_counts()["total"],
            "mempool": self.mempool.stats()["count"],
            "is_mining": self.miner.is_mining,
            "tx_count": self.store.count_transactions(wallet["address"]),
        }

    async def create_transaction(
        self,
        receiver: str,
        amount: float,
        fee: float,
        note: str | None = None,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        wallet = self.default_wallet()
        tx = create_transfer(
            sender=wallet["address"],
            receiver=receiver,
            amount=amount,
            fee=fee,
            public_key=wallet["public_key"],
            private_key=wallet["private_key"],
            note=note,
        )
        accepted, result = self.mempool.add_transaction(tx, source="local")
        if not accepted:
            return False, result, None
        await self.p2p.broadcast_tx(tx)
        return True, result, tx

    async def receive_transaction(self, tx: dict[str, Any], source: str = "peer") -> tuple[bool, str]:
        return self.mempool.add_transaction(tx, source=source)

    async def receive_block(self, block: dict[str, Any], source: str = "peer") -> tuple[bool, str]:
        accepted, message = self.blockchain.add_block(block, source=source)
        if not accepted:
            self.log(f"Block rejected from {source}: {message}")
        return accepted, message

    async def receive_blocks(
        self,
        blocks: list[dict[str, Any]],
        from_height: int = 0,
        source: str = "sync",
    ) -> tuple[bool, str]:
        if from_height == 0:
            accepted, message = self.blockchain.replace_with_chain(blocks, source=source)
            if not accepted:
                self.log(f"Chain replacement skipped from {source}: {message}")
            return accepted, message

        accepted_any = False
        last_message = "no blocks"
        for block in blocks:
            accepted, last_message = await self.receive_block(block, source=source)
            accepted_any = accepted_any or accepted
        return accepted_any, last_message

    async def connect_peer(self, ip: str, port: int) -> tuple[bool, str]:
        return await self.p2p.connect_peer(ip, int(port))

    async def sync_blocks(self) -> dict[str, Any]:
        requested = await self.p2p.request_blocks()
        self.log(f"Block sync requested from {requested} peer(s)")
        return {"requested_peers": requested}

    async def set_difficulty(self, difficulty: int) -> dict[str, Any]:
        difficulty = int(difficulty)
        minimum = int(self.config.get("min_difficulty", DEFAULT_CONFIG["min_difficulty"]))
        maximum = int(self.config.get("max_difficulty", DEFAULT_CONFIG["max_difficulty"]))
        if difficulty < minimum or difficulty > maximum:
            raise ValueError(f"difficulty must be between {minimum} and {maximum}")
        was_mining = self.miner.is_mining
        if was_mining:
            await self.miner.stop()
        self.config["difficulty"] = difficulty
        save_config(self.config)
        self.p2p.identity = network_identity(self.config)
        self.log(f"Difficulty set to {difficulty}")
        return {
            "difficulty": difficulty,
            "mining_stopped": was_mining,
            "message": "difficulty updated",
        }

    async def reset_chain(self) -> dict[str, Any]:
        was_mining = self.miner.is_mining
        if was_mining:
            await self.miner.stop()
        genesis_hash = self.blockchain.reset_to_genesis()
        return {
            "height": self.blockchain.height(),
            "tip_hash": genesis_hash,
            "mining_stopped": was_mining,
            "message": "blockchain reset to genesis",
        }

    def list_blocks(self, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        limit = min(max(int(limit), 1), 200)
        offset = max(int(offset), 0)
        return {
            "blocks": self.store.list_block_summaries(limit=limit, offset=offset),
            "limit": limit,
            "offset": offset,
            "height": self.blockchain.height(),
        }

    def block_detail(self, identifier: str) -> dict[str, Any] | None:
        return self.store.get_block_detail(identifier)

    def network_info(self) -> dict[str, Any]:
        identity = network_identity(self.config)
        lan_ip = normalize_host(self.config.get("advertise_ip")) or self._lan_ip()
        lan_ipv6 = normalize_host(self.config.get("advertise_ipv6")) or self._lan_ipv6()
        web_host = normalize_host(self.config["web_host"])
        if web_host == "0.0.0.0":
            web_display_host = lan_ip
        elif web_host == "::":
            web_display_host = lan_ipv6 or "::1"
        else:
            web_display_host = web_host
        web_url = f"http://{format_url_host(web_display_host)}:{int(self.config['web_port'])}"

        p2p_addresses: list[str] = []
        listen_hosts = self.p2p.listen_hosts or configured_listen_hosts(self.config)
        for listen_host in listen_hosts:
            if listen_host == "0.0.0.0":
                advertised_host = lan_ip
            elif listen_host == "::":
                advertised_host = lan_ipv6
            else:
                advertised_host = listen_host
            if not advertised_host:
                continue
            address = format_host_port(advertised_host, int(self.config["listen_port"]))
            if address not in p2p_addresses:
                p2p_addresses.append(address)
        if not p2p_addresses:
            p2p_addresses.append(format_host_port(lan_ip, int(self.config["listen_port"])))
        prefer_ipv6 = is_ipv6_host(self.config["listen_ip"])
        primary_p2p = next(
            (
                address
                for address in p2p_addresses
                if is_ipv6_host(address.rsplit(":", 1)[0]) == prefer_ipv6
            ),
            p2p_addresses[0],
        )
        advertised_ip, _advertised_port = parse_host_port(primary_p2p)
        lan_ips = [lan_ip, *([lan_ipv6] if lan_ipv6 else [])]
        return {
            **identity,
            "lan_ip": lan_ip,
            "lan_ipv6": lan_ipv6,
            "lan_ips": lan_ips,
            "web_url": web_url,
            "p2p_address": primary_p2p,
            "p2p_addresses": p2p_addresses,
            "advertised_ip": advertised_ip,
            "advertise_ip": self.config.get("advertise_ip"),
            "advertise_ipv6": self.config.get("advertise_ipv6"),
            "listen_ip": self.config["listen_ip"],
            "listen_ips": list(self.p2p.listen_hosts),
            "enable_ipv6": bool(self.config.get("enable_ipv6", True)),
            "listen_port": int(self.config["listen_port"]),
            "web_host": self.config["web_host"],
            "web_port": int(self.config["web_port"]),
        }

    def classroom_status(self) -> dict[str, Any]:
        status = self.status()
        network = status["network"]
        own = {
            "name": status["node_name"],
            "ip": network["advertised_ip"],
            "port": network["listen_port"],
            "address": status["wallet"]["address"],
            "status": PeerStatus.SELF,
            "direction": "self",
            "height": status["height"],
            "difficulty": status["difficulty"],
            "target": status["target"],
            "mining_status": status["mining"]["status"],
            "network_id": network["network_id"],
            "chain_params_hash": network["chain_params_hash"],
            "last_seen": int(time.time()),
            "mismatch_reason": None,
        }
        peers = self.store.list_peers()
        mismatches = [
            peer for peer in peers
            if peer.get("status") == PeerStatus.PARAM_MISMATCH
            or "mismatch" in str(peer.get("mismatch_reason") or "")
        ]
        return {
            "self": own,
            "peers": peers,
            "nodes": [own, *peers],
            "mismatch_count": len(mismatches),
            "network": network,
        }

    def security_status(self, limit: int = 100) -> dict[str, Any]:
        events = self.security_events.recent(limit)
        return {
            "count": len(events),
            "events": events,
        }

    def status(self) -> dict[str, Any]:
        wallet = self.default_wallet()
        tip = self.blockchain.tip()
        peers = self.p2p.connection_counts()
        mempool_stats = self.mempool.stats()
        target = self.blockchain.expected_target()
        return {
            "version": self.config["version"],
            "node_name": self.config["node_name"],
            "wallet": {
                "name": wallet["name"],
                "address": wallet["address"],
                "public_key": wallet["public_key"],
                "created_at": wallet["created_at"],
            },
            "balance": self.blockchain.get_balance(wallet["address"]),
            "available_balance": self.blockchain.get_available_balance(wallet["address"]),
            "height": int(tip["height"]),
            "tip_hash": tip["hash"],
            "last_block_time": int(tip["timestamp"]),
            "chain_work": str(self.blockchain.chain_work()),
            "median_time_past": self.blockchain.median_time_past(),
            "orphan_count": len(self.blockchain.orphans),
            "difficulty": self.blockchain.expected_difficulty(),
            "target": target,
            "target_preview": target_preview(target),
            "difficulty_policy": self.blockchain.difficulty_policy(),
            "supply": self.blockchain.supply_policy(),
            "immature_balance": self.blockchain.immature_balance(wallet["address"]),
            "spendable_balance": self.blockchain.spendable_balance(wallet["address"]),
            "network": self.network_info(),
            "mining": {
                "status": self.miner.status,
                "is_mining": self.miner.is_mining,
                "nonce": self.miner.current_nonce,
                "hash": self.miner.current_hash,
            },
            "mempool": mempool_stats,
            "peers": peers,
            "logs": self.events.recent(),
            "security": self.security_status(limit=20),
        }
