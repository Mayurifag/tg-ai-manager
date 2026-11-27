import asyncio
import secrets
import time
from dataclasses import dataclass, field
from functools import partial

from src.adapters.telegram.sticker_catalog import StickerCatalog
from src.adapters.telegram.sticker_copy import Replacement, StickerCopy
from src.adapters.telegram.sticker_edits import StickerEdits
from src.adapters.telegram.sticker_media import StickerMedia
from src.adapters.telegram.sticker_rpc import StickerError, StickerRPC, Uncertain
from src.adapters.telegram.sticker_similarity import scan_duplicates
from src.domain.errors import AccountChanged
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)
STATUS_TTL = 900
PREFETCH_INTERVAL = 300


@dataclass
class Operation:
    kind: str
    state: str = "running"
    message: str = "Starting…"
    result: dict = field(default_factory=dict)
    retry: dict | None = None
    replacement: Replacement | None = None
    task: asyncio.Task | None = None
    expires: float = 0
    cancel_requested: bool = False

    def snapshot(self):
        return {
            "kind": self.kind,
            "state": self.state,
            "message": self.message,
            "retry": bool(self.retry),
            **self.result,
        }


class StickerWorkspace:
    """Account-scoped sticker services and their short-lived operations."""

    def __init__(self, client, media, queue):
        self.token = secrets.token_urlsafe(18)
        self._closed = False
        self._operations = {}
        self._prefetch_task = None
        self._prefetch_after = 0.0
        rpc = StickerRPC(client, queue, self.check)
        self._catalog = StickerCatalog(rpc)
        self._media = StickerMedia(client, media, self._catalog, self.check)
        edits = StickerEdits(self._catalog, rpc)
        copies = StickerCopy(self._catalog, self._media, rpc, edits)
        self._actions = {
            "copy": copies.copy,
            "finish_copy": copies.finish,
            "edit": edits.edit,
            "install": edits.install,
            "uninstall": edits.uninstall,
            "rename": edits.rename,
            "reorder": edits.reorder,
            "scan": partial(scan_duplicates, self._catalog, self._media),
        }

    def retire(self):
        self._closed = True
        if self._prefetch_task and not self._prefetch_task.done():
            self._prefetch_task.cancel()
        self._catalog.retire()
        for op in self._operations.values():
            if op.task and not op.task.done():
                op.task.cancel()

    async def close(self):
        await asyncio.gather(
            *((self._prefetch_task,) if self._prefetch_task else ()),
            *(op.task for op in self._operations.values() if op.task),
            return_exceptions=True,
        )
        await self._catalog.close()
        self._operations.clear()

    def check(self):
        if self._closed:
            raise AccountChanged()

    def prefetch(self):
        self.check()
        now = time.monotonic()
        if now < self._prefetch_after or (
            self._prefetch_task and not self._prefetch_task.done()
        ):
            return
        self._prefetch_after = now + PREFETCH_INTERVAL
        self._prefetch_task = asyncio.create_task(self._prefetch())

    async def _prefetch(self):
        try:
            await self._media.prefetch()
        except AccountChanged:
            pass
        except Exception as error:
            logger.warning("sticker_prefetch_failed", category=type(error).__name__)

    async def _read(self, operation):
        try:
            return await operation
        finally:
            # A retired account can cancel the shared lookup or download.
            self.check()

    async def summaries(self):
        return await self._read(self._catalog.summaries())

    async def items(self, source):
        return await self._read(self._catalog.items(source))

    async def artwork(self, source, doc_id, mode):
        return await self._read(self._media.artwork(source, doc_id, mode))

    def grid_artwork(self, entries):
        return self._media.grid_artwork(entries)

    def operation(self, op_id):
        self.check()
        now = time.monotonic()
        for key, op in list(self._operations.items()):
            if op.expires and now >= op.expires:
                del self._operations[key]
        return self._operations.get(op_id)

    def start(self, kind, args):
        self.operation(None)
        if len(self._operations) >= 64:
            raise StickerError("Too many recent sticker operations. Try again later.")
        if any(
            op.state == "running" and (kind == "scan") == (op.kind == "scan")
            for op in self._operations.values()
        ):
            raise StickerError(
                "A duplicate scan is already running."
                if kind == "scan"
                else "A sticker change is already running."
            )
        op_id = secrets.token_urlsafe(16)
        op = self._operations[op_id] = Operation(kind)
        op.task = asyncio.create_task(self._run(op, args))
        return op_id

    async def _run(self, op, args):
        try:
            await self._actions[op.kind](op, **args)
            op.state = "done"
        except asyncio.CancelledError:
            op.state, op.message = "canceled", "Duplicate scan canceled."
            raise
        except Uncertain as error:
            op.state, op.message = "uncertain", str(error)
        except StickerError as error:
            op.state, op.message = "failed", str(error)
        except Exception as error:
            logger.warning(
                "sticker_operation_error", kind=op.kind, category=type(error).__name__
            )
            op.state, op.message = (
                "failed",
                "Could not complete the operation. Refresh to check what changed.",
            )
        finally:
            op.expires = time.monotonic() + STATUS_TTL
