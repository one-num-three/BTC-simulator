from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from app.core.blockchain import Blockchain
from app.core.transaction import estimate_transaction_bytes
from app.storage.sqlite_store import SQLiteStore

LogFn = Callable[[str], None]


class Mempool:
    """Pending transactions, with a fee market.

    The previous version accepted transactions until a byte limit was reached
    and then rejected everything new, forever, regardless of how much each one
    paid. That is backwards: a full mempool is exactly when fees are supposed
    to start deciding who gets in. It also never expired anything, so a
    transaction whose sender had gone offline sat there for the life of the
    process, and there was no way to bump a stuck transaction.
    """

    def __init__(self, store: SQLiteStore, blockchain: Blockchain, log: LogFn | None = None):
        self.store = store
        self.blockchain = blockchain
        self.log = log or (lambda _message: None)

    @property
    def max_bytes(self) -> int:
        return int(self.blockchain.config["mempool_max_bytes"])

    @property
    def expiry_seconds(self) -> int:
        return max(int(self.blockchain.config.get("mempool_expiry_seconds", 3600)), 0)

    @property
    def min_relay_fee(self) -> float:
        return max(float(self.blockchain.config.get("min_relay_fee", 0.0)), 0.0)

    @staticmethod
    def fee_rate(tx: dict[str, Any]) -> float:
        """Fee per byte. Miners sort by this, not by the raw fee."""
        size = max(estimate_transaction_bytes(tx), 1)
        return float(tx.get("fee", 0.0)) / size

    def expire_old(self, now: int | None = None) -> int:
        """Drop transactions that have been waiting too long."""
        if self.expiry_seconds <= 0:
            return 0
        cutoff = int(now or time.time()) - self.expiry_seconds
        expired = self.store.expired_mempool_tx_ids(cutoff)
        if expired:
            self.store.remove_mempool_transactions(expired)
            self.log(f"Mempool expired {len(expired)} transaction(s) older than {self.expiry_seconds}s")
        return len(expired)

    def _make_room(self, incoming: dict[str, Any]) -> tuple[bool, str]:
        """Evict the cheapest transactions until the newcomer fits.

        Returns (fits, reason). A transaction that pays less per byte than
        everything already queued does not get to push anyone out.
        """
        incoming_size = estimate_transaction_bytes(incoming)
        if incoming_size > self.max_bytes:
            return False, "transaction is larger than the whole mempool limit"

        current = self.store.mempool_stats()["bytes"]
        if current + incoming_size <= self.max_bytes:
            return True, ""

        incoming_rate = self.fee_rate(incoming)
        # cheapest first: these are the eviction candidates
        candidates = sorted(
            self.store.list_mempool_transactions(),
            key=lambda tx: (self.fee_rate(tx), -int(tx.get("timestamp", 0))),
        )
        evicted: list[str] = []
        freed = 0
        for tx in candidates:
            if current - freed + incoming_size <= self.max_bytes:
                break
            if self.fee_rate(tx) >= incoming_rate:
                # nothing left that is cheaper than the newcomer
                break
            evicted.append(tx["tx_id"])
            freed += estimate_transaction_bytes(tx)

        if current - freed + incoming_size > self.max_bytes:
            return False, (
                "mempool is full and this transaction's fee rate is too low to "
                "replace anything in it"
            )
        if evicted:
            self.store.remove_mempool_transactions(evicted)
            self.log(f"Mempool evicted {len(evicted)} low-fee transaction(s) to make room")
        return True, ""

    def _replacement_target(self, tx: dict[str, Any]) -> dict[str, Any] | None:
        """Find a queued transaction this one is trying to replace (RBF).

        The simulator has no sequence numbers, so "same sender, same receiver,
        same amount, higher fee" is the signal. It is enough to demonstrate why
        fee bumping exists.
        """
        for existing in self.store.list_mempool_transactions():
            if existing["tx_id"] == tx["tx_id"]:
                continue
            if (
                existing.get("sender") == tx.get("sender")
                and existing.get("receiver") == tx.get("receiver")
                and abs(float(existing.get("amount", 0)) - float(tx.get("amount", 0))) < 1e-8
                and float(tx.get("fee", 0)) > float(existing.get("fee", 0))
            ):
                return existing
        return None

    def add_transaction(self, tx: dict[str, Any], source: str = "local") -> tuple[bool, str]:
        try:
            self.expire_old()

            if float(tx.get("fee", 0.0)) < self.min_relay_fee:
                raise ValueError(
                    f"fee is below the relay minimum of {self.min_relay_fee}"
                )

            replaced = self._replacement_target(tx)
            if replaced is not None:
                # Free the old one first so the balance check below sees the
                # funds it was holding.
                self.store.remove_mempool_transactions([replaced["tx_id"]])

            try:
                self.blockchain.validate_transfer_transaction(tx)
            except Exception:
                if replaced is not None:
                    # put the original back; the replacement was not valid
                    self.store.add_mempool_transaction(replaced, received_at=int(time.time()))
                raise

            fits, reason = self._make_room(tx)
            if not fits:
                if replaced is not None:
                    self.store.add_mempool_transaction(replaced, received_at=int(time.time()))
                raise ValueError(reason)

            self.store.add_mempool_transaction(tx, received_at=int(time.time()))
        except Exception as exc:
            self.log(f"Transaction rejected from {source}: {exc}")
            return False, str(exc)

        if replaced is not None:
            self.log(
                f"Transaction {tx['tx_id'][:16]} replaced {replaced['tx_id'][:16]} "
                f"(fee {replaced['fee']} -> {tx['fee']})"
            )
        self.log(f"Transaction added from {source}: {tx['tx_id'][:16]}")
        return True, tx["tx_id"]

    def remove_confirmed(self, tx_ids: list[str]) -> None:
        self.store.remove_mempool_transactions(tx_ids)

    def stats(self) -> dict[str, Any]:
        stats = self.store.mempool_stats()
        transactions = self.store.list_mempool_transactions()
        rates = [self.fee_rate(tx) for tx in transactions]
        stats["max_bytes"] = self.max_bytes
        stats["usage"] = round(stats["bytes"] / self.max_bytes, 6) if self.max_bytes else 0.0
        stats["min_fee_rate"] = round(min(rates), 10) if rates else 0.0
        stats["max_fee_rate"] = round(max(rates), 10) if rates else 0.0
        stats["total_fees"] = round(sum(float(tx.get("fee", 0.0)) for tx in transactions), 8)
        return stats

    def ordered(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Highest fee rate first, which is the order a miner would pick."""
        transactions = self.store.list_mempool_transactions()
        transactions.sort(
            key=lambda tx: (self.fee_rate(tx), -int(tx.get("timestamp", 0))), reverse=True
        )
        return transactions[:limit] if limit is not None else transactions
