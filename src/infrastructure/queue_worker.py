import asyncio

from arq import Worker

from src.infrastructure.arq_config import get_redis_settings
from src.infrastructure.logging import get_logger
from src.infrastructure.queue_jobs import WorkerSettings

logger = get_logger(__name__)


class EmbeddedQueueWorker:
    def __init__(self):
        self.worker: Worker | None = None
        self.task: asyncio.Task | None = None

    async def start(self):
        """Starts the Arq worker in a background task."""
        redis_settings = get_redis_settings()
        self.worker = Worker(
            functions=WorkerSettings.functions,
            redis_settings=redis_settings,
            on_startup=WorkerSettings.on_startup,
            on_shutdown=WorkerSettings.on_shutdown,
            on_job_completion=WorkerSettings.on_job_completion,
            max_tries=WorkerSettings.max_tries,
            allow_abort_jobs=True,
        )
        self.task = asyncio.create_task(self.worker.async_run())
        logger.info("embedded_worker_started")

    async def stop(self):
        """Stops the worker gracefully."""
        if self.worker:
            await self.worker.close()
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logger.info("embedded_worker_stopped")
