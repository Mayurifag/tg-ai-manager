import asyncio
import configparser
import os
import shlex
import signal
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.web import _build_tg_adapter


def test_importing_app_does_not_replace_process_signal_handlers():
    import importlib

    before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    importlib.import_module("src.app")
    assert {sig: signal.getsignal(sig) for sig in before} == before


async def test_database_failure_does_not_start_a_new_telegram_account():
    settings = SimpleNamespace(TG_API_ID=123, TG_API_HASH="synthetic")
    repo = SimpleNamespace(
        get_user=AsyncMock(side_effect=OSError("database unavailable"))
    )
    with pytest.raises(OSError):
        await _build_tg_adapter(settings, repo)


async def test_configured_hypercorn_mode_runs_asgi_shutdown_on_sigterm(tmp_path):
    config = configparser.ConfigParser()
    config.read("docker/supervisord.conf")
    command = shlex.split(config["program:app"]["command"])
    assert command[command.index("--workers") + 1] == "0"

    marker = tmp_path / "lifespan.txt"
    (tmp_path / "synthetic_asgi.py").write_text(
        """import os
from pathlib import Path

async def app(scope, receive, send):
    if scope['type'] != 'lifespan':
        return
    marker = Path(os.environ['LIFESPAN_MARKER'])
    while True:
        event = await receive()
        if event['type'] == 'lifespan.startup':
            marker.write_text('started')
            await send({'type': 'lifespan.startup.complete'})
        elif event['type'] == 'lifespan.shutdown':
            marker.write_text(marker.read_text() + ',shutdown')
            await send({'type': 'lifespan.shutdown.complete'})
            return
"""
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "hypercorn",
        "synthetic_asgi:app",
        "--bind",
        "127.0.0.1:0",
        "--workers",
        "0",
        cwd=tmp_path,
        env={**os.environ, "LIFESPAN_MARKER": str(marker)},
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:

        async def wait_until_listening():
            while line := await process.stderr.readline():
                if b"Running on" in line:
                    return
            raise AssertionError("Hypercorn exited before listening")

        await asyncio.wait_for(wait_until_listening(), timeout=10)
        process.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(process.wait(), timeout=10) == 0
        assert marker.read_text() == "started,shutdown"
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
