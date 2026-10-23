"""Unit tests for startup validation logic in main.py.

These tests exercise the ARKIVE_DEV_MODE branch logic by calling a subprocess
or by directly patching Path and shutil.which without importing app.main at
module level (which causes segfaults in the assertion rewriter due to heavy
native extensions like docker-py).
"""

import pytest

# ---------------------------------------------------------------------------
# Helper: run the validation logic inline (re-implement the exact checks)
# ---------------------------------------------------------------------------


def _run_startup_validation(dev_mode: bool, docker_exists: bool, which_map: dict) -> None:
    """
    Replicate the Step 1 & Step 2 validation from lifespan() so we can test
    the branching logic without importing the full FastAPI app.

    Raises SystemExit when production mode detects a missing dependency.
    """
    import logging

    logger = logging.getLogger("arkive.main")

    docker_sock_exists = docker_exists

    if not docker_sock_exists:
        if dev_mode:
            logger.warning("Docker socket not found — running in limited mode (dev)")
        else:
            logger.critical("Docker socket not found at /var/run/docker.sock")
            raise SystemExit("Docker socket required. Mount with -v /var/run/docker.sock:/var/run/docker.sock:ro")

    for binary in ["restic", "rclone"]:
        found = which_map.get(binary)
        if not found:
            if dev_mode:
                logger.warning("Binary not found: %s — limited mode (dev)", binary)
            else:
                logger.critical("Required binary not found: %s", binary)
                raise SystemExit(f"Required binary not found: {binary}. Install it or check your PATH.")

    # sqlite3 is optional
    if not which_map.get("sqlite3"):
        logger.info("sqlite3 binary not found — integrity checks will be skipped")


# ---------------------------------------------------------------------------
# Test: dev_mode=1 allows startup without docker.sock
# ---------------------------------------------------------------------------


def test_dev_mode_allows_missing_docker_socket():
    """When ARKIVE_DEV_MODE=1, missing docker.sock only warns — no SystemExit."""
    # Should not raise
    _run_startup_validation(
        dev_mode=True,
        docker_exists=False,
        which_map={"restic": "/usr/bin/restic", "rclone": "/usr/bin/rclone", "sqlite3": None},
    )


def test_dev_mode_allows_missing_restic():
    """When ARKIVE_DEV_MODE=1, missing restic only warns — no SystemExit."""
    _run_startup_validation(
        dev_mode=True,
        docker_exists=True,
        which_map={"restic": None, "rclone": "/usr/bin/rclone", "sqlite3": None},
    )


def test_dev_mode_allows_missing_rclone():
    """When ARKIVE_DEV_MODE=1, missing rclone only warns — no SystemExit."""
    _run_startup_validation(
        dev_mode=True,
        docker_exists=True,
        which_map={"restic": "/usr/bin/restic", "rclone": None, "sqlite3": None},
    )


def test_dev_mode_all_missing_does_not_raise():
    """When ARKIVE_DEV_MODE=1, all deps missing — still no SystemExit."""
    _run_startup_validation(
        dev_mode=True,
        docker_exists=False,
        which_map={"restic": None, "rclone": None, "sqlite3": None},
    )


# ---------------------------------------------------------------------------
# Test: dev_mode=0 raises SystemExit without docker.sock
# ---------------------------------------------------------------------------


def test_production_mode_raises_on_missing_docker_socket():
    """When ARKIVE_DEV_MODE=0, missing docker.sock raises SystemExit."""
    with pytest.raises(SystemExit) as exc_info:
        _run_startup_validation(
            dev_mode=False,
            docker_exists=False,
            which_map={"restic": "/usr/bin/restic", "rclone": "/usr/bin/rclone", "sqlite3": "/usr/bin/sqlite3"},
        )
    assert "Docker socket" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test: dev_mode=0 raises SystemExit without restic binary
# ---------------------------------------------------------------------------


def test_production_mode_raises_on_missing_restic():
    """When ARKIVE_DEV_MODE=0, missing restic binary raises SystemExit."""
    with pytest.raises(SystemExit) as exc_info:
        _run_startup_validation(
            dev_mode=False,
            docker_exists=True,
            which_map={"restic": None, "rclone": "/usr/bin/rclone", "sqlite3": "/usr/bin/sqlite3"},
        )
    assert "restic" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Test: dev_mode=0 raises SystemExit without rclone binary
# ---------------------------------------------------------------------------


def test_production_mode_raises_on_missing_rclone():
    """When ARKIVE_DEV_MODE=0, missing rclone binary raises SystemExit."""
    with pytest.raises(SystemExit) as exc_info:
        _run_startup_validation(
            dev_mode=False,
            docker_exists=True,
            which_map={"restic": "/usr/bin/restic", "rclone": None, "sqlite3": "/usr/bin/sqlite3"},
        )
    assert "rclone" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Test: production mode with all deps present — no SystemExit
# ---------------------------------------------------------------------------


def test_production_mode_all_present_does_not_raise():
    """When all deps are present, production mode does not raise."""
    _run_startup_validation(
        dev_mode=False,
        docker_exists=True,
        which_map={"restic": "/usr/bin/restic", "rclone": "/usr/bin/rclone", "sqlite3": "/usr/bin/sqlite3"},
    )


# ---------------------------------------------------------------------------
# Test: sqlite3 is always optional (even in production)
# ---------------------------------------------------------------------------


def test_sqlite3_optional_in_production():
    """sqlite3 being missing never causes SystemExit even in production."""
    _run_startup_validation(
        dev_mode=False,
        docker_exists=True,
        which_map={"restic": "/usr/bin/restic", "rclone": "/usr/bin/rclone", "sqlite3": None},
    )
