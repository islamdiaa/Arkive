"""Tests for app.utils.subprocess_runner."""

import asyncio
import os
import sys

import pytest

from app.utils.subprocess_runner import run_command


@pytest.mark.asyncio
async def test_run_command_passes_input_data_to_stdin():
    """input_data should be delivered to the subprocess stdin pipe."""
    result = await run_command(
        [
            sys.executable,
            "-c",
            "import sys; data=sys.stdin.read(); print(data, end='')",
        ],
        input_data="hello-stdin\n",
        timeout=10,
    )

    assert result.returncode == 0
    assert result.stdout == f"hello-stdin{os.linesep}"


@pytest.mark.asyncio
async def test_streams_stdout_before_process_finishes_and_preserves_output():
    first_line = asyncio.Event()
    lines = []

    async def on_line(line):
        lines.append(line)
        first_line.set()

    task = asyncio.create_task(
        run_command(
            [
                sys.executable,
                "-c",
                "import sys,time; print('ready',flush=True); time.sleep(.5); sys.stderr.write('error'); print('done',end='')",
            ],
            stdout_callback=on_line,
        )
    )
    await asyncio.wait_for(first_line.wait(), timeout=5)
    assert not task.done()
    result = await task
    assert lines == ["ready", "done"]
    assert result.stdout == f"ready{os.linesep}done"
    assert result.stderr == "error"
    assert result.returncode == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_streaming_keeps_timeout_and_cancellation(cancel):
    async def on_line(line):
        pass

    result = await run_command(
        [sys.executable, "-c", "import time; time.sleep(20)"],
        timeout=0.2,
        cancel_check=(lambda: True) if cancel else None,
        stdout_callback=on_line,
    )
    assert result.returncode == (-2 if cancel else -1)


@pytest.mark.asyncio
async def test_streaming_callback_failure_does_not_fail_command():
    async def on_line(line):
        raise ValueError("unavailable progress display")

    result = await run_command([sys.executable, "-c", "print('done')"], stdout_callback=on_line)
    assert result.returncode == 0
    assert result.stdout == f"done{os.linesep}"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_unlimited_timeout_still_allows_completion_and_cancellation(cancel):
    async def on_line(line):
        pass

    result = await run_command(
        [sys.executable, "-c", "import time; time.sleep(.2); print('done')"],
        timeout=None,
        cancel_check=(lambda: True) if cancel else None,
        stdout_callback=on_line,
    )
    assert result.returncode == (-2 if cancel else 0)
