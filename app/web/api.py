from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import resolve_project_path
from app.runtime import NodeService
from app.web.security import AdminGuard, load_or_create_token

#: How often the live status socket pushes. The payload is diffed against the
#: last frame, so an idle node sends a few bytes rather than the whole status
#: object (which carries 300 log lines) every second.
STATUS_PUSH_SECONDS = 1.0


class SendTransactionRequest(BaseModel):
    receiver: str = Field(min_length=1)
    amount: float = Field(gt=0)
    fee: float = Field(ge=0)
    note: str | None = Field(default=None, max_length=280)


class WalletGenerateRequest(BaseModel):
    name: str = Field(default="default", max_length=64)


class WalletSelectRequest(BaseModel):
    address: str = Field(min_length=1)


class WalletImportRequest(BaseModel):
    private_key: str = Field(min_length=1, max_length=128)
    name: str = Field(default="imported", max_length=64)
    make_default: bool = True


class PeerRequest(BaseModel):
    ip: str = Field(min_length=1, max_length=128)
    port: int = Field(ge=1, le=65535)


class DifficultyRequest(BaseModel):
    difficulty: int = Field(ge=0, le=255)


def create_web_app(service: NodeService) -> FastAPI:
    token_dir = resolve_project_path(
        service.config, service.config["storage"]["path"]
    ).parent
    admin_token = load_or_create_token(token_dir)
    guard = AdminGuard(service.config, admin_token)
    service.admin_token = admin_token

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Replaces the deprecated @app.on_event("startup"/"shutdown") pair.
        await service.start()
        try:
            yield
        finally:
            await service.shutdown()

    app = FastAPI(
        title="BTC Simulator",
        version=service.config["version"],
        lifespan=lifespan,
    )
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    def require_admin(request: Request) -> None:
        """Thin wrapper so FastAPI can resolve the `Request` annotation.

        `Depends(guard)` on the AdminGuard instance itself does not work: FastAPI
        reads `call.__globals__` to resolve string annotations (PEP 563), and an
        instance has no `__globals__`, so `request` was silently demoted to a
        required query parameter and every guarded route 422'd.
        """
        guard(request)

    admin = Depends(require_admin)

    # ---------------------------------------------------------------- reads

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(static_dir / "favicon.svg", media_type="image/svg+xml")

    @app.get("/api/session")
    async def session(request: Request) -> dict[str, Any]:
        """Whether this browser may change the node.

        The console asks the server instead of trusting `?administrator=true`,
        which anyone could type.
        """
        return {
            "is_admin": guard.is_trusted(request),
            "enforced": guard.enforced,
            "trust_loopback": guard.trust_loopback,
        }

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return service.status()

    @app.get("/api/wallet")
    async def wallet() -> dict[str, Any]:
        return service.status()["wallet"]

    @app.get("/api/wallets")
    async def wallets() -> dict[str, Any]:
        return {"wallets": service.list_wallets()}

    @app.get("/api/mempool")
    async def mempool() -> dict[str, Any]:
        return {"transactions": service.mempool.ordered(), "stats": service.mempool.stats()}

    @app.get("/api/blocks")
    async def blocks(limit: int = 50, offset: int = 0) -> dict[str, Any]:
        return service.list_blocks(limit=limit, offset=offset)

    @app.get("/api/blocks/{identifier}")
    async def block_detail(identifier: str) -> dict[str, Any]:
        detail = service.block_detail(identifier)
        if detail is None:
            raise HTTPException(status_code=404, detail="block not found")
        return detail

    @app.get("/api/transactions")
    async def transactions(
        address: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        return service.transaction_history(address=address, limit=limit, offset=offset)

    @app.get("/api/transactions/{tx_id}")
    async def transaction_detail(tx_id: str) -> dict[str, Any]:
        detail = service.transaction_detail(tx_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="transaction not found")
        return detail

    @app.get("/api/search")
    async def search(q: str) -> dict[str, Any]:
        return service.search(q)

    @app.get("/api/proof/{tx_id}")
    async def merkle_proof(tx_id: str) -> dict[str, Any]:
        proof = service.merkle_proof(tx_id)
        if proof is None:
            raise HTTPException(status_code=404, detail="transaction not found in any block")
        return proof

    @app.get("/api/stats")
    async def stats(window: int = 100) -> dict[str, Any]:
        return service.chart_stats(window=window)

    @app.get("/api/peers")
    async def peers() -> dict[str, Any]:
        return {
            "connections": service.p2p.connection_counts(),
            "peers": service.store.list_peers(),
        }

    @app.get("/api/classroom")
    async def classroom() -> dict[str, Any]:
        return service.classroom_status()

    @app.get("/api/security-events")
    async def security_events(limit: int = 100) -> dict[str, Any]:
        return service.security_status(limit=limit)

    @app.get("/api/lab")
    async def lab() -> dict[str, Any]:
        return service.lab_tasks()

    # --------------------------------------------------------------- writes

    @app.post("/api/wallet/generate", dependencies=[admin])
    async def generate_wallet(payload: WalletGenerateRequest) -> dict[str, Any]:
        return service.generate_new_wallet(payload.name)

    @app.post("/api/wallet/select", dependencies=[admin])
    async def select_wallet(payload: WalletSelectRequest) -> dict[str, Any]:
        try:
            return service.select_wallet(payload.address)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/wallet/import", dependencies=[admin])
    async def import_wallet(payload: WalletImportRequest) -> dict[str, Any]:
        try:
            return service.import_wallet(
                payload.private_key, payload.name, make_default=payload.make_default
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/wallet/export", dependencies=[admin])
    async def export_wallet(address: str | None = None) -> dict[str, Any]:
        try:
            return service.export_wallet(address)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/transactions", dependencies=[admin])
    async def send_transaction(payload: SendTransactionRequest) -> dict[str, Any]:
        accepted, message, tx = await service.create_transaction(
            payload.receiver,
            payload.amount,
            payload.fee,
            payload.note,
        )
        if not accepted:
            raise HTTPException(status_code=400, detail=message)
        return {"tx_id": message, "tx": tx}

    @app.post("/api/mining/start", dependencies=[admin])
    async def start_mining() -> dict[str, Any]:
        started = await service.miner.start()
        return {"started": started, "status": service.miner.status}

    @app.post("/api/mining/stop", dependencies=[admin])
    async def stop_mining() -> dict[str, Any]:
        stopped = await service.miner.stop()
        return {"stopped": stopped, "status": service.miner.status}

    @app.post("/api/peers", dependencies=[admin])
    async def connect_peer(payload: PeerRequest) -> dict[str, Any]:
        accepted, message = await service.connect_peer(payload.ip, payload.port)
        if not accepted and "already" not in message and "self" not in message:
            raise HTTPException(status_code=400, detail=message)
        return {"connected": accepted, "message": message}

    @app.delete("/api/peers", dependencies=[admin])
    async def forget_peer(ip: str, port: int) -> dict[str, Any]:
        return await service.forget_peer(ip, port)

    @app.post("/api/peers/ban", dependencies=[admin])
    async def ban_peer(payload: PeerRequest) -> dict[str, Any]:
        return await service.ban_peer(payload.ip, payload.port)

    @app.post("/api/peers/unban", dependencies=[admin])
    async def unban_peer(payload: PeerRequest) -> dict[str, Any]:
        return service.unban_peer(payload.ip, payload.port)

    @app.post("/api/sync", dependencies=[admin])
    async def sync() -> dict[str, Any]:
        return await service.sync_blocks()

    @app.post("/api/settings/difficulty", dependencies=[admin])
    async def set_difficulty(payload: DifficultyRequest) -> dict[str, Any]:
        try:
            return await service.set_difficulty(payload.difficulty)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/chain/reset", dependencies=[admin])
    async def reset_chain() -> dict[str, Any]:
        return await service.reset_chain()

    # ------------------------------------------------------------ live feed

    @app.websocket("/ws/events")
    async def websocket_events(websocket: WebSocket) -> None:
        await websocket.accept()
        previous: dict[str, Any] = {}
        try:
            while True:
                current = service.status()
                patch = _status_patch(previous, current)
                if patch:
                    await websocket.send_json(patch)
                previous = current
                await asyncio.sleep(STATUS_PUSH_SECONDS)
        except (WebSocketDisconnect, ConnectionError, RuntimeError):
            return

    return app


def _status_patch(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Send only the top-level sections that actually changed.

    The old socket re-sent the entire status object -- including the full log
    buffer and the security feed -- every second whether or not anything moved.
    On an idle node that was tens of kilobytes per second per open tab.
    """
    if not previous:
        return {"full": True, **current}
    patch = {
        key: value
        for key, value in current.items()
        if previous.get(key) != value
    }
    if not patch:
        return {}
    return {"full": False, **patch}
