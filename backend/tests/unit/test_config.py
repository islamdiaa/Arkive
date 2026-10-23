"""Tests for core configuration."""

import os
from pathlib import Path


def test_config_defaults():
    """Test ArkiveConfig default values."""
    from app.core.config import ArkiveConfig

    config = ArkiveConfig()
    assert config.port == 8200
    assert config.db_path.name == "arkive.db"


def test_config_env_override():
    """Test config can be overridden via env vars."""
    os.environ["ARKIVE_PORT"] = "9999"
    os.environ["ARKIVE_LOG_LEVEL"] = "DEBUG"
    try:
        from importlib import reload

        import app.core.config as config_mod

        reload(config_mod)
        config = config_mod.ArkiveConfig()
        assert config.port == 9999
        assert config.log_level == "DEBUG"
    finally:
        del os.environ["ARKIVE_PORT"]
        del os.environ["ARKIVE_LOG_LEVEL"]


def test_config_dirs():
    """Test ensure_dirs creates required directories."""
    from app.core.config import ArkiveConfig

    config = ArkiveConfig()
    config.config_dir = Path("/tmp/arkive-test-config")
    config.ensure_dirs()
    assert config.dump_dir.exists()
    assert config.log_dir.exists()
    # Cleanup
    import shutil

    shutil.rmtree("/tmp/arkive-test-config", ignore_errors=True)
