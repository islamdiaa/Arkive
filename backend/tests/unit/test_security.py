"""Tests for security module."""

import os
import time


def test_api_key_generation():
    """Test API key generation format."""
    from app.core.security import generate_api_key

    key = generate_api_key()
    assert key.startswith("ark_")
    assert len(key) > 20


def test_api_key_hashing():
    """Test API key hashing is deterministic."""
    from app.core.security import generate_api_key, hash_api_key

    key = generate_api_key()
    h1 = hash_api_key(key)
    h2 = hash_api_key(key)
    assert h1 == h2
    assert h1 != key


def test_encryption_roundtrip(tmp_path):
    """Test encrypt/decrypt roundtrip."""
    old_config_dir = os.environ.get("ARKIVE_CONFIG_DIR")
    os.environ["ARKIVE_CONFIG_DIR"] = str(tmp_path)
    try:
        from app.core.security import _load_fernet_from_dir, _reset_fernet, decrypt_value, encrypt_value

        _reset_fernet()
        _load_fernet_from_dir(str(tmp_path))

        original = "my-secret-password-123"
        encrypted = encrypt_value(original)
        assert encrypted != original
        decrypted = decrypt_value(encrypted)
        assert decrypted == original
    finally:
        _reset_fernet()
        if old_config_dir:
            os.environ["ARKIVE_CONFIG_DIR"] = old_config_dir
        else:
            os.environ.pop("ARKIVE_CONFIG_DIR", None)


def test_clear_rate_limit_clears_attempts():
    """clear_rate_limit removes both failed attempts and lockout for the IP."""
    from app.core.dependencies import _failed_attempts, _lockouts, clear_rate_limit

    _failed_attempts["1.2.3.4"] = [time.time()] * 5
    _lockouts["1.2.3.4"] = time.time() + 60
    clear_rate_limit("1.2.3.4")
    assert "1.2.3.4" not in _failed_attempts
    assert "1.2.3.4" not in _lockouts


def test_clear_rate_limit_unknown_ip():
    """clear_rate_limit on an unknown IP should not raise."""
    from app.core.dependencies import clear_rate_limit

    clear_rate_limit("unknown.ip")  # should not raise


def test_clear_rate_limit_does_not_affect_other_ips():
    """clear_rate_limit only removes the targeted IP, leaving others intact."""
    from app.core.dependencies import _failed_attempts, clear_rate_limit

    _failed_attempts["1.1.1.1"] = [time.time()]
    _failed_attempts["2.2.2.2"] = [time.time()]
    clear_rate_limit("1.1.1.1")
    assert "1.1.1.1" not in _failed_attempts
    assert "2.2.2.2" in _failed_attempts
