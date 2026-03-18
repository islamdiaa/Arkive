"""Async subprocess runner with timeout and logging."""

import asyncio
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass

from app.utils.redact import redact_credentials

logger = logging.getLogger("arkive.subprocess")


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    command: str


def _kill_process_group(pid: int) -> None:
    """Kill the entire process group rooted at *pid*.

    Uses SIGKILL via os.killpg so that child processes spawned by the
    subprocess are also terminated.  Silently ignores ProcessLookupError
    (process already exited).
    """
    try:
        if hasattr(os, 'killpg'):
            # POSIX: kill entire process group
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        else:
            # Windows: no process groups, kill single process only
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        # Fallback: the process may not be a group leader on some platforms
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def run_command(
    cmd: list[str],
    timeout: int = 300,
    env: dict | None = None,
    cwd: str | None = None,
    input_data: str | None = None,
    cancel_check=None,
    cancel_poll_interval: float = 0.5,
) -> CommandResult:
    """Run a subprocess command asynchronously with timeout."""
    cmd_str = " ".join(cmd)
    logger.debug("Running: %s", cmd_str)
    start = time.monotonic()

    # On POSIX, start the child in its own process group so that on
    # timeout/cancel we can kill the whole tree (parent + children).
    preexec = os.setsid if sys.platform != "win32" else None

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if input_data is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
            preexec_fn=preexec,
        )

        communicate_task = asyncio.create_task(
            process.communicate(input=input_data.encode() if input_data else None)
        )
        deadline = time.monotonic() + timeout

        while True:
            wait_timeout = min(cancel_poll_interval, max(deadline - time.monotonic(), 0))
            if cancel_check and cancel_check():
                logger.warning("Command cancelled: %s", cmd_str)
                try:
                    if process.pid is not None:
                        _kill_process_group(process.pid)
                    await process.wait()
                except Exception:
                    pass
                if not communicate_task.done():
                    communicate_task.cancel()
                    try:
                        await communicate_task
                    except BaseException:
                        pass
                duration = time.monotonic() - start
                return CommandResult(
                    returncode=-2,
                    stdout="",
                    stderr="Command cancelled",
                    duration_seconds=round(duration, 2),
                    command=cmd_str,
                )

            if wait_timeout <= 0:
                raise asyncio.TimeoutError

            done, _ = await asyncio.wait({communicate_task}, timeout=wait_timeout)
            if communicate_task in done:
                stdout_bytes, stderr_bytes = await communicate_task
                break

        duration = time.monotonic() - start
        result = CommandResult(
            returncode=process.returncode or 0,
            stdout=stdout_bytes.decode(errors="replace"),
            stderr=stderr_bytes.decode(errors="replace"),
            duration_seconds=round(duration, 2),
            command=cmd_str,
        )
        if result.returncode != 0:
            redacted_stderr = redact_credentials(result.stderr[:500])
            logger.warning("Command failed (rc=%d): %s\nstderr: %s", result.returncode, cmd_str, redacted_stderr)
        else:
            logger.debug("Command succeeded in %.2fs: %s", duration, cmd_str)
        return result

    except asyncio.TimeoutError:
        duration = time.monotonic() - start
        logger.error("Command timed out after %ds: %s", timeout, cmd_str)
        try:
            if process.pid is not None:
                _kill_process_group(process.pid)
            await process.wait()
        except Exception:
            pass
        return CommandResult(
            returncode=-1,
            stdout="",
            stderr=f"Command timed out after {timeout}s",
            duration_seconds=round(duration, 2),
            command=cmd_str,
        )
    except Exception as e:
        duration = time.monotonic() - start
        logger.error("Command error: %s — %s", cmd_str, str(e))
        return CommandResult(
            returncode=-1,
            stdout="",
            stderr=str(e),
            duration_seconds=round(duration, 2),
            command=cmd_str,
        )
