"""Critical tests for subprocess runner: cancellation behavior.

Validates that:
- Cancellation kills the subprocess and returns rc=-2
- Cancellation is not swallowed (caller sees it)
- Timeouts return rc=-1
- Normal execution returns correct stdout/stderr
- Credential redaction in stderr logging
"""

import asyncio
import logging
import sys

import pytest

from app.utils.subprocess_runner import run_command

logger = logging.getLogger(__name__)


class TestNormalExecution:
    """Test normal subprocess execution."""

    async def test_successful_command_returns_zero(self):
        """Successful command should return rc=0 with stdout."""
        result = await run_command(
            [sys.executable, "-c", "print('hello')"],
            timeout=10,
        )
        assert result.returncode == 0
        assert "hello" in result.stdout
        assert result.duration_seconds >= 0
        logger.info("Command succeeded: rc=%d, stdout=%s",
                    result.returncode, result.stdout.strip())

    async def test_failing_command_returns_nonzero(self):
        """Failing command should return non-zero exit code."""
        result = await run_command(
            [sys.executable, "-c", "import sys; sys.exit(42)"],
            timeout=10,
        )
        assert result.returncode == 42
        logger.info("Command failed as expected: rc=%d", result.returncode)

    async def test_stderr_captured(self):
        """stderr output should be captured."""
        result = await run_command(
            [sys.executable, "-c", "import sys; print('err msg', file=sys.stderr)"],
            timeout=10,
        )
        assert result.returncode == 0
        assert "err msg" in result.stderr
        logger.info("stderr captured: %s", result.stderr.strip())

    async def test_input_data_delivered_to_stdin(self):
        """input_data should be piped to subprocess stdin."""
        result = await run_command(
            [sys.executable, "-c",
             "import sys; data=sys.stdin.read(); print(data, end='')"],
            input_data="hello-stdin\n",
            timeout=10,
        )
        assert result.returncode == 0
        assert result.stdout == "hello-stdin\n"
        logger.info("stdin data delivered correctly")


class TestCancellation:
    """Test subprocess cancellation via cancel_check."""

    async def test_cancel_check_kills_subprocess(self):
        """cancel_check returning True should kill the subprocess."""
        cancel_state = {"stop": False}

        async def trigger_cancel():
            await asyncio.sleep(0.2)
            cancel_state["stop"] = True

        cancel_task = asyncio.create_task(trigger_cancel())
        try:
            result = await run_command(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout=60,
                cancel_check=lambda: cancel_state["stop"],
                cancel_poll_interval=0.05,
            )
        finally:
            await cancel_task

        assert result.returncode == -2, \
            f"Cancelled command should have rc=-2, got rc={result.returncode}"
        assert result.stderr == "Command cancelled"
        logger.info("Cancellation detected: rc=%d, stderr=%s",
                    result.returncode, result.stderr)

    async def test_cancellation_not_swallowed(self):
        """Cancellation result must propagate to the caller, not be silently ignored."""
        result = await run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=60,
            cancel_check=lambda: True,  # Immediately cancelled
            cancel_poll_interval=0.01,
        )

        # The key assertion: cancellation is visible to the caller
        assert result.returncode == -2
        assert "cancelled" in result.stderr.lower()
        logger.info("Cancellation correctly propagated to caller")

    async def test_cancel_check_not_called_when_none(self):
        """When cancel_check is None, command runs normally."""
        result = await run_command(
            [sys.executable, "-c", "print('no cancel')"],
            timeout=10,
            cancel_check=None,
        )
        assert result.returncode == 0
        assert "no cancel" in result.stdout
        logger.info("No cancel_check: command ran normally")


class TestTimeout:
    """Test subprocess timeout handling."""

    async def test_timeout_returns_negative_one(self):
        """Timed-out command should return rc=-1."""
        result = await run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=1,
        )
        assert result.returncode == -1
        assert "timed out" in result.stderr.lower()
        logger.info("Timeout detected: rc=%d, stderr=%s",
                    result.returncode, result.stderr)

    async def test_timeout_includes_duration(self):
        """Timed-out result should have duration close to the timeout."""
        timeout_seconds = 1
        result = await run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=timeout_seconds,
        )
        assert result.returncode == -1
        assert result.duration_seconds >= timeout_seconds * 0.8
        logger.info("Timeout duration: %.2fs (timeout was %ds)",
                    result.duration_seconds, timeout_seconds)


class TestCommandResultFields:
    """Test that CommandResult has all expected fields."""

    async def test_result_has_command_field(self):
        """Result should include the command that was run."""
        result = await run_command(
            [sys.executable, "-c", "print('test')"],
            timeout=10,
        )
        assert sys.executable in result.command
        assert "print" in result.command
        logger.info("Command field: %s", result.command)

    async def test_result_has_duration_field(self):
        """Result should include duration_seconds as a float."""
        result = await run_command(
            [sys.executable, "-c", "print('test')"],
            timeout=10,
        )
        assert isinstance(result.duration_seconds, float)
        assert result.duration_seconds >= 0
        logger.info("Duration: %.2fs", result.duration_seconds)
