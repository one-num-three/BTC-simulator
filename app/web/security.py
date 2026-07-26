"""Who is allowed to change this node.

Before this module every endpoint was unauthenticated, including
``POST /api/transactions``. That endpoint signs with the node's stored private
key, so any device on the classroom LAN could empty a classmate's wallet with
one curl command -- and ``POST /api/chain/reset`` could wipe their chain. The
``?administrator=true`` query parameter was purely cosmetic: it only unhid
buttons in the browser and was never checked by the server.

The model here is deliberately simple, because this is a teaching tool that has
to keep working when the network is a school Wi-Fi with no DNS and no CA:

* **Reads are open.** Anyone on the LAN can watch the chain, the peers and the
  security page. That openness is the point of the classroom demo.
* **Writes require trust.** A request is trusted when it comes from the machine
  the node runs on (loopback), or when it carries the node's admin token.

The token lives in a file next to the database, never in the tracked config
file, and is printed once at startup so a teacher can drive a remote node.
"""

from __future__ import annotations

import hmac
import secrets
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

TOKEN_HEADER = "x-admin-token"
TOKEN_QUERY = "token"
TOKEN_FILENAME = "admin_token"

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}


def load_or_create_token(token_dir: Path) -> str:
    """Read the node's admin token, creating it on first run."""
    token_dir.mkdir(parents=True, exist_ok=True)
    token_path = token_dir / TOKEN_FILENAME
    if token_path.exists():
        existing = token_path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    token = secrets.token_urlsafe(24)
    token_path.write_text(token + "\n", encoding="utf-8")
    try:
        token_path.chmod(0o600)
    except OSError:
        # Windows and some network filesystems do not support chmod.
        pass
    return token


def is_loopback_client(request: Request) -> bool:
    client = request.client
    if client is None:
        return False
    host = str(client.host or "").strip().lower()
    if host.startswith("::ffff:"):
        host = host[len("::ffff:") :]
    return host in LOOPBACK_HOSTS or host == "127.0.0.1"


def presented_token(request: Request) -> str | None:
    header = request.headers.get(TOKEN_HEADER)
    if header:
        return header.strip()
    query = request.query_params.get(TOKEN_QUERY)
    if query:
        return query.strip()
    return None


class AdminGuard:
    """Dependency that gates every state-changing endpoint."""

    def __init__(self, config: dict[str, Any], token: str):
        self.config = config
        self.token = token

    @property
    def trust_loopback(self) -> bool:
        return bool(self.config.get("trust_loopback_admin", True))

    @property
    def enforced(self) -> bool:
        return bool(self.config.get("require_admin_for_writes", True))

    def is_trusted(self, request: Request) -> bool:
        if not self.enforced:
            return True
        if self.trust_loopback and is_loopback_client(request):
            return True
        supplied = presented_token(request)
        if not supplied:
            return False
        # constant-time compare so the token cannot be guessed byte by byte
        return hmac.compare_digest(supplied, self.token)

    def __call__(self, request: Request) -> None:
        if self.is_trusted(request):
            return
        raise HTTPException(
            status_code=403,
            detail=(
                "This node only accepts changes from its own machine. "
                "Supply the admin token via the X-Admin-Token header or ?token= "
                "to control it from another computer."
            ),
        )
