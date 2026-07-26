from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from app.core.block import (
    MEDIAN_TIME_SPAN,
    format_work,
    header_work,
    median_time_past,
    parse_work,
)
from app.core.transaction import estimate_transaction_bytes
from app.status import LEGACY_PEER_STATUS


class SQLiteStore:
    """Small synchronized SQLite wrapper for the simulator.

    MVP stores private keys in plaintext to keep the teaching flow simple.
    Never reuse this code for real assets.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    def init_schema(self) -> None:
        with self.lock, self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS wallet_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    address TEXT NOT NULL UNIQUE,
                    public_key TEXT NOT NULL,
                    private_key_plain_for_mvp TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    is_default INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS blocks (
                    height INTEGER PRIMARY KEY,
                    hash TEXT NOT NULL UNIQUE,
                    prev_hash TEXT NOT NULL,
                    merkle_root TEXT NOT NULL,
                    timestamp INTEGER NOT NULL,
                    difficulty INTEGER NOT NULL,
                    target TEXT,
                    nonce INTEGER NOT NULL,
                    version INTEGER NOT NULL,
                    chain_work TEXT,
                    block_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS transactions (
                    tx_id TEXT PRIMARY KEY,
                    block_hash TEXT NOT NULL,
                    type TEXT NOT NULL,
                    sender TEXT,
                    receiver TEXT NOT NULL,
                    amount REAL NOT NULL,
                    fee REAL NOT NULL,
                    timestamp INTEGER NOT NULL,
                    tx_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS mempool (
                    tx_id TEXT PRIMARY KEY,
                    sender TEXT NOT NULL,
                    receiver TEXT NOT NULL,
                    amount REAL NOT NULL,
                    fee REAL NOT NULL,
                    timestamp INTEGER NOT NULL,
                    tx_json TEXT NOT NULL,
                    received_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS peers (
                    ip TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    name TEXT,
                    address TEXT,
                    direction TEXT,
                    status TEXT,
                    last_seen INTEGER,
                    network_id TEXT,
                    chain_params_hash TEXT,
                    height INTEGER,
                    chain_work TEXT,
                    tip_hash TEXT,
                    difficulty INTEGER,
                    target TEXT,
                    mining_status TEXT,
                    web_port INTEGER,
                    mismatch_reason TEXT,
                    PRIMARY KEY (ip, port)
                );

                CREATE TABLE IF NOT EXISTS banned_peers (
                    ip TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    reason TEXT,
                    banned_at INTEGER NOT NULL,
                    PRIMARY KEY (ip, port)
                );
                """
            )
            self._ensure_columns(
                "blocks",
                {
                    "target": "TEXT",
                    "chain_work": "TEXT",
                },
            )
            # Older databases stored Chinese display labels in peers.status.
            for legacy, modern in LEGACY_PEER_STATUS.items():
                self.conn.execute(
                    "UPDATE peers SET status = ? WHERE status = ?", (modern, legacy)
                )
            self.conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_transactions_sender
                    ON transactions(sender);
                CREATE INDEX IF NOT EXISTS idx_transactions_receiver
                    ON transactions(receiver);
                CREATE INDEX IF NOT EXISTS idx_transactions_block_hash
                    ON transactions(block_hash);
                CREATE INDEX IF NOT EXISTS idx_mempool_sender
                    ON mempool(sender);
                """
            )
            self._ensure_columns(
                "peers",
                {
                    "network_id": "TEXT",
                    "chain_params_hash": "TEXT",
                    "height": "INTEGER",
                    "chain_work": "TEXT",
                    "tip_hash": "TEXT",
                    "difficulty": "INTEGER",
                    "target": "TEXT",
                    "mining_status": "TEXT",
                    "web_port": "INTEGER",
                    "mismatch_reason": "TEXT",
                },
            )

    def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        existing = {
            str(row["name"])
            for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, definition in columns.items():
            if name not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def save_wallet(self, wallet: Any, make_default: bool = True) -> None:
        with self.lock, self.conn:
            if make_default:
                self.conn.execute("UPDATE wallet_keys SET is_default = 0")
            self.conn.execute(
                """
                INSERT OR REPLACE INTO wallet_keys
                (name, address, public_key, private_key_plain_for_mvp, created_at, is_default)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    wallet.name,
                    wallet.address,
                    wallet.public_key,
                    wallet.private_key,
                    wallet.created_at,
                    1 if make_default else int(wallet.is_default),
                ),
            )

    def get_default_wallet(self) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(
                """
                SELECT name, address, public_key, private_key_plain_for_mvp AS private_key,
                       created_at, is_default
                FROM wallet_keys
                WHERE is_default = 1
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            return dict(row) if row else None

    def list_wallets(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT name, address, public_key, private_key_plain_for_mvp AS private_key,
                       created_at, is_default
                FROM wallet_keys
                ORDER BY id DESC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def get_wallet(self, address: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(
                """
                SELECT name, address, public_key, private_key_plain_for_mvp AS private_key,
                       created_at, is_default
                FROM wallet_keys
                WHERE address = ?
                """,
                (str(address),),
            ).fetchone()
            return dict(row) if row else None

    def set_default_wallet(self, address: str) -> bool:
        """Switch which key the node signs and mines with.

        Generating a wallet used to silently replace the default and the old
        one became unreachable from the console even though its balance was
        still on chain.
        """
        with self.lock, self.conn:
            existing = self.conn.execute(
                "SELECT 1 FROM wallet_keys WHERE address = ? LIMIT 1", (str(address),)
            ).fetchone()
            if existing is None:
                return False
            self.conn.execute("UPDATE wallet_keys SET is_default = 0")
            self.conn.execute(
                "UPDATE wallet_keys SET is_default = 1 WHERE address = ?", (str(address),)
            )
            return True

    def insert_block(self, height: int, block_hash: str, block: dict[str, Any]) -> None:
        header = block["header"]
        with self.lock, self.conn:
            if int(height) == 0:
                cumulative = 0
            else:
                previous = self.conn.execute(
                    "SELECT chain_work FROM blocks WHERE height = ?", (int(height) - 1,)
                ).fetchone()
                cumulative = parse_work(previous["chain_work"] if previous else None)
                cumulative += header_work(header)
            self.conn.execute(
                """
                INSERT INTO blocks
                (height, hash, prev_hash, merkle_root, timestamp, difficulty, target,
                 nonce, version, chain_work, block_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    height,
                    block_hash,
                    header["prev_hash"],
                    header["merkle_root"],
                    header["timestamp"],
                    header["difficulty"],
                    header.get("target"),
                    header["nonce"],
                    header["version"],
                    format_work(cumulative),
                    json.dumps(block, ensure_ascii=True, sort_keys=True),
                ),
            )
            for tx in block.get("transactions", []):
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO transactions
                    (tx_id, block_hash, type, sender, receiver, amount, fee, timestamp, tx_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tx["tx_id"],
                        block_hash,
                        tx["type"],
                        tx.get("sender"),
                        tx["receiver"],
                        float(tx["amount"]),
                        float(tx.get("fee", 0.0)),
                        int(tx["timestamp"]),
                        json.dumps(tx, ensure_ascii=True, sort_keys=True),
                    ),
                )

    def get_tip(self) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM blocks ORDER BY height DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def get_block_by_hash(self, block_hash: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM blocks WHERE hash = ?", (block_hash,)
            ).fetchone()
            return dict(row) if row else None

    def get_block_by_height(self, height: int) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM blocks WHERE height = ?", (int(height),)
            ).fetchone()
            return dict(row) if row else None

    def has_block_hash(self, block_hash: str) -> bool:
        return self.get_block_by_hash(block_hash) is not None

    def tip_chain_work(self) -> int:
        """Cumulative work of the stored chain, used by the longest-chain rule."""
        with self.lock:
            row = self.conn.execute(
                "SELECT chain_work FROM blocks ORDER BY height DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return 0
            if row["chain_work"] is not None:
                return parse_work(row["chain_work"])
        return self.recompute_chain_work()

    def recompute_chain_work(self) -> int:
        """Backfill chain_work for databases written before the column existed."""
        with self.lock, self.conn:
            rows = self.conn.execute(
                "SELECT height, block_json FROM blocks ORDER BY height ASC"
            ).fetchall()
            cumulative = 0
            for row in rows:
                if int(row["height"]) > 0:
                    header = json.loads(row["block_json"])["header"]
                    cumulative += header_work(header)
                self.conn.execute(
                    "UPDATE blocks SET chain_work = ? WHERE height = ?",
                    (format_work(cumulative), int(row["height"])),
                )
            return cumulative

    def recent_timestamps(self, count: int = MEDIAN_TIME_SPAN) -> list[int]:
        """Timestamps of the newest ``count`` blocks, oldest first."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT timestamp FROM blocks ORDER BY height DESC LIMIT ?",
                (int(count),),
            ).fetchall()
        return [int(row["timestamp"]) for row in reversed(rows)]

    def median_time_past(self) -> int | None:
        return median_time_past(self.recent_timestamps())

    def get_block_json_by_hash(self, block_hash: str) -> dict[str, Any] | None:
        row = self.get_block_by_hash(block_hash)
        return json.loads(row["block_json"]) if row else None

    def list_block_summaries(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT
                    b.height,
                    b.hash,
                    b.prev_hash,
                    b.merkle_root,
                    b.timestamp,
                    b.difficulty,
                    b.target,
                    b.nonce,
                    b.version,
                    COUNT(t.tx_id) AS tx_count,
                    COALESCE(SUM(t.fee), 0) AS total_fees
                FROM blocks b
                LEFT JOIN transactions t ON t.block_hash = b.hash
                GROUP BY b.height
                ORDER BY b.height DESC
                LIMIT ? OFFSET ?
                """,
                (int(limit), int(offset)),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_block_detail(self, identifier: str | int) -> dict[str, Any] | None:
        if isinstance(identifier, int) or str(identifier).isdigit():
            row = self.get_block_by_height(int(identifier))
        else:
            row = self.get_block_by_hash(str(identifier))
        if not row:
            return None

        block = json.loads(row["block_json"])
        with self.lock:
            tx_rows = self.conn.execute(
                """
                SELECT tx_json
                FROM transactions
                WHERE block_hash = ?
                ORDER BY rowid ASC
                """,
                (row["hash"],),
            ).fetchall()
        transactions = [json.loads(tx_row["tx_json"]) for tx_row in tx_rows]
        if len(transactions) != len(block.get("transactions", [])):
            transactions = block.get("transactions", [])

        return {
            **dict(row),
            "transactions": transactions,
            "tx_count": len(transactions),
            "total_fees": round(
                sum(float(tx.get("fee", 0.0)) for tx in transactions if tx.get("type") != "coinbase"),
                8,
            ),
            "block": block,
        }

    def get_blocks_from_height(self, from_height: int, limit: int = 10000) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT block_json FROM blocks WHERE height >= ? ORDER BY height ASC LIMIT ?",
                (int(from_height), int(limit)),
            ).fetchall()
            return [json.loads(row["block_json"]) for row in rows]

    def replace_chain(self, blocks: list[dict[str, Any]], block_hashes: list[str]) -> None:
        if len(blocks) != len(block_hashes):
            raise ValueError("blocks and hashes length mismatch")
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM blocks")
            self.conn.execute("DELETE FROM transactions")
            self.conn.execute("DELETE FROM mempool")
            for height, (block, block_hash) in enumerate(zip(blocks, block_hashes)):
                self.insert_block(height, block_hash, block)

    def is_tx_confirmed(self, tx_id: str) -> bool:
        with self.lock:
            row = self.conn.execute(
                "SELECT 1 FROM transactions WHERE tx_id = ? LIMIT 1", (tx_id,)
            ).fetchone()
            return row is not None

    def confirmed_balance(self, address: str) -> float:
        with self.lock:
            income = self.conn.execute(
                "SELECT COALESCE(SUM(amount), 0) AS value FROM transactions WHERE receiver = ?",
                (address,),
            ).fetchone()["value"]
            spend = self.conn.execute(
                """
                SELECT COALESCE(SUM(amount + fee), 0) AS value
                FROM transactions
                WHERE sender = ?
                """,
                (address,),
            ).fetchone()["value"]
            return round(float(income) - float(spend), 8)

    # ------------------------------------------------------------------
    # transaction lookup
    #
    # The console could previously only search by block height or block hash,
    # so a student who had just sent a transaction had no way to find it again.
    # ------------------------------------------------------------------

    def get_transaction(self, tx_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(
                """
                SELECT t.tx_id, t.block_hash, t.type, t.sender, t.receiver,
                       t.amount, t.fee, t.timestamp, t.tx_json,
                       b.height AS block_height, b.timestamp AS block_time
                FROM transactions t
                LEFT JOIN blocks b ON b.hash = t.block_hash
                WHERE t.tx_id = ?
                """,
                (str(tx_id),),
            ).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["tx"] = json.loads(record.pop("tx_json"))
        record["state"] = "confirmed"
        return record

    def get_mempool_transaction(self, tx_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT tx_json, received_at FROM mempool WHERE tx_id = ?",
                (str(tx_id),),
            ).fetchone()
        if row is None:
            return None
        tx = json.loads(row["tx_json"])
        return {
            "tx_id": tx["tx_id"],
            "block_hash": None,
            "block_height": None,
            "block_time": None,
            "type": tx.get("type"),
            "sender": tx.get("sender"),
            "receiver": tx.get("receiver"),
            "amount": float(tx.get("amount", 0.0)),
            "fee": float(tx.get("fee", 0.0)),
            "timestamp": int(tx.get("timestamp", 0)),
            "received_at": int(row["received_at"]),
            "tx": tx,
            "state": "pending",
        }

    def list_transactions(
        self,
        address: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clause = ""
        params: list[Any] = []
        if address:
            clause = "WHERE t.sender = ? OR t.receiver = ?"
            params = [address, address]
        params.extend([int(limit), int(offset)])
        with self.lock:
            rows = self.conn.execute(
                f"""
                SELECT t.tx_id, t.block_hash, t.type, t.sender, t.receiver,
                       t.amount, t.fee, t.timestamp,
                       b.height AS block_height, b.timestamp AS block_time
                FROM transactions t
                LEFT JOIN blocks b ON b.hash = t.block_hash
                {clause}
                ORDER BY b.height DESC, t.rowid DESC
                LIMIT ? OFFSET ?
                """,
                tuple(params),
            ).fetchall()
        return [{**dict(row), "state": "confirmed"} for row in rows]

    def count_transactions(self, address: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS value FROM transactions"
        params: tuple[Any, ...] = ()
        if address:
            sql += " WHERE sender = ? OR receiver = ?"
            params = (address, address)
        with self.lock:
            return int(self.conn.execute(sql, params).fetchone()["value"])

    def list_pending_transactions(self, address: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT tx_json, received_at FROM mempool"
        params: tuple[Any, ...] = ()
        if address:
            sql += " WHERE sender = ? OR receiver = ?"
            params = (address, address)
        sql += " ORDER BY fee DESC, timestamp ASC"
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        results = []
        for row in rows:
            tx = json.loads(row["tx_json"])
            results.append(
                {
                    "tx_id": tx["tx_id"],
                    "block_hash": None,
                    "block_height": None,
                    "block_time": None,
                    "type": tx.get("type"),
                    "sender": tx.get("sender"),
                    "receiver": tx.get("receiver"),
                    "amount": float(tx.get("amount", 0.0)),
                    "fee": float(tx.get("fee", 0.0)),
                    "timestamp": int(tx.get("timestamp", 0)),
                    "received_at": int(row["received_at"]),
                    "state": "pending",
                }
            )
        return results

    def find_addresses(self, prefix: str, limit: int = 10) -> list[str]:
        pattern = f"{prefix}%"
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT DISTINCT address FROM (
                    SELECT sender AS address FROM transactions WHERE sender LIKE ?
                    UNION
                    SELECT receiver AS address FROM transactions WHERE receiver LIKE ?
                )
                WHERE address IS NOT NULL
                LIMIT ?
                """,
                (pattern, pattern, int(limit)),
            ).fetchall()
        return [str(row["address"]) for row in rows]

    def address_summary(self, address: str) -> dict[str, Any]:
        with self.lock:
            received = self.conn.execute(
                "SELECT COALESCE(SUM(amount), 0) AS value, COUNT(*) AS count "
                "FROM transactions WHERE receiver = ?",
                (address,),
            ).fetchone()
            sent = self.conn.execute(
                "SELECT COALESCE(SUM(amount), 0) AS value, "
                "COALESCE(SUM(fee), 0) AS fees, COUNT(*) AS count "
                "FROM transactions WHERE sender = ?",
                (address,),
            ).fetchone()
        return {
            "address": address,
            "received": round(float(received["value"]), 8),
            "received_count": int(received["count"]),
            "sent": round(float(sent["value"]), 8),
            "fees_paid": round(float(sent["fees"]), 8),
            "sent_count": int(sent["count"]),
        }

    # ------------------------------------------------------------------
    # chart data
    # ------------------------------------------------------------------

    def block_series(self, limit: int = 100) -> list[dict[str, Any]]:
        """Recent blocks with the fields the charts need, oldest first."""
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT b.height, b.timestamp, b.difficulty, b.target, b.hash,
                       COUNT(t.tx_id) AS tx_count,
                       COALESCE(SUM(CASE WHEN t.type = 'coinbase' THEN t.amount ELSE 0 END), 0)
                           AS coinbase_amount,
                       COALESCE(SUM(CASE WHEN t.type != 'coinbase' THEN t.fee ELSE 0 END), 0)
                           AS fees
                FROM blocks b
                LEFT JOIN transactions t ON t.block_hash = b.hash
                GROUP BY b.height
                ORDER BY b.height DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def total_issued(self) -> float:
        with self.lock:
            row = self.conn.execute(
                "SELECT COALESCE(SUM(amount), 0) AS value FROM transactions WHERE type = 'coinbase'"
            ).fetchone()
        return round(float(row["value"]), 8)

    def immature_coinbase_total(self, address: str, mature_below_height: int) -> float:
        """Coinbase paid to ``address`` in blocks newer than the maturity depth."""
        with self.lock:
            row = self.conn.execute(
                """
                SELECT COALESCE(SUM(t.amount), 0) AS value
                FROM transactions t
                JOIN blocks b ON b.hash = t.block_hash
                WHERE t.type = 'coinbase' AND t.receiver = ? AND b.height > ?
                """,
                (address, int(mature_below_height)),
            ).fetchone()
        return round(float(row["value"]), 8)

    def add_mempool_transaction(self, tx: dict[str, Any], received_at: int) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO mempool
                (tx_id, sender, receiver, amount, fee, timestamp, tx_json, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tx["tx_id"],
                    tx["sender"],
                    tx["receiver"],
                    float(tx["amount"]),
                    float(tx["fee"]),
                    int(tx["timestamp"]),
                    json.dumps(tx, ensure_ascii=True, sort_keys=True),
                    int(received_at),
                ),
            )

    def is_tx_in_mempool(self, tx_id: str) -> bool:
        with self.lock:
            row = self.conn.execute(
                "SELECT 1 FROM mempool WHERE tx_id = ? LIMIT 1", (tx_id,)
            ).fetchone()
            return row is not None

    def list_mempool_transactions(self, limit: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT tx_json FROM mempool ORDER BY fee DESC, timestamp ASC"
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (int(limit),)
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
            return [json.loads(row["tx_json"]) for row in rows]

    def pending_outgoing(self, address: str, exclude_tx_id: str | None = None) -> float:
        sql = "SELECT COALESCE(SUM(amount + fee), 0) AS value FROM mempool WHERE sender = ?"
        params: list[Any] = [address]
        if exclude_tx_id:
            sql += " AND tx_id != ?"
            params.append(exclude_tx_id)
        with self.lock:
            row = self.conn.execute(sql, tuple(params)).fetchone()
            return round(float(row["value"]), 8)

    def mempool_stats(self) -> dict[str, int]:
        txs = self.list_mempool_transactions()
        return {
            "count": len(txs),
            "bytes": sum(estimate_transaction_bytes(tx) for tx in txs),
        }

    def remove_mempool_transactions(self, tx_ids: list[str]) -> None:
        if not tx_ids:
            return
        with self.lock, self.conn:
            self.conn.executemany("DELETE FROM mempool WHERE tx_id = ?", [(tx_id,) for tx_id in tx_ids])

    def upsert_peer(
        self,
        ip: str,
        port: int,
        name: str | None,
        address: str | None,
        direction: str | None,
        status: str,
        last_seen: int,
        network_id: str | None = None,
        chain_params_hash: str | None = None,
        height: int | None = None,
        chain_work: str | None = None,
        tip_hash: str | None = None,
        difficulty: int | None = None,
        target: str | None = None,
        mining_status: str | None = None,
        web_port: int | None = None,
        mismatch_reason: str | None = None,
    ) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO peers (
                    ip, port, name, address, direction, status, last_seen,
                    network_id, chain_params_hash, height, chain_work, tip_hash,
                    difficulty, target, mining_status, web_port, mismatch_reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ip, port) DO UPDATE SET
                    name = excluded.name,
                    address = excluded.address,
                    direction = excluded.direction,
                    status = excluded.status,
                    last_seen = excluded.last_seen,
                    network_id = excluded.network_id,
                    chain_params_hash = excluded.chain_params_hash,
                    height = excluded.height,
                    chain_work = excluded.chain_work,
                    tip_hash = excluded.tip_hash,
                    difficulty = excluded.difficulty,
                    target = excluded.target,
                    mining_status = excluded.mining_status,
                    web_port = excluded.web_port,
                    mismatch_reason = excluded.mismatch_reason
                """,
                (
                    ip,
                    int(port),
                    name,
                    address,
                    direction,
                    status,
                    int(last_seen),
                    network_id,
                    chain_params_hash,
                    height,
                    chain_work,
                    tip_hash,
                    difficulty,
                    target,
                    mining_status,
                    web_port,
                    mismatch_reason,
                ),
            )

    def list_peers(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT ip, port, name, address, direction, status, last_seen,
                       network_id, chain_params_hash, height, chain_work, tip_hash,
                       difficulty, target, mining_status, web_port, mismatch_reason
                FROM peers
                ORDER BY ip, port
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def delete_peer(self, ip: str, port: int) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "DELETE FROM peers WHERE ip = ? AND port = ?",
                (ip, int(port)),
            )

    # ------------------------------------------------------------------
    # peer bans
    #
    # The security page used to only *report* misbehaving nodes. A teacher
    # could see which student was flooding invalid blocks and had no way to
    # make it stop.
    # ------------------------------------------------------------------

    def ban_peer(self, ip: str, port: int, reason: str | None, banned_at: int) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO banned_peers (ip, port, reason, banned_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(ip, port) DO UPDATE SET
                    reason = excluded.reason,
                    banned_at = excluded.banned_at
                """,
                (ip, int(port), reason, int(banned_at)),
            )

    def unban_peer(self, ip: str, port: int) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "DELETE FROM banned_peers WHERE ip = ? AND port = ?", (ip, int(port))
            )

    def is_peer_banned(self, ip: str, port: int) -> bool:
        with self.lock:
            row = self.conn.execute(
                "SELECT 1 FROM banned_peers WHERE ip = ? AND port = ? LIMIT 1",
                (ip, int(port)),
            ).fetchone()
        return row is not None

    def list_banned_peers(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT ip, port, reason, banned_at FROM banned_peers ORDER BY ip, port"
            ).fetchall()
        return [dict(row) for row in rows]
