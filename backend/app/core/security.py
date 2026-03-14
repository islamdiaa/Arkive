"""Cryptographic operations — API keys, browser sessions, and config encryption."""

import hashlib
import hmac
import json
import os
import secrets
import string
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

DEFAULT_KEYFILE_DIR = "/config"
BROWSER_SESSION_TTL = 60 * 60 * 24 * 30
BROWSER_SESSION_COOKIE = "arkive_session"

# Module-level cache: set by _load_fernet_from_dir() or lazily by _get_fernet()
_fernet_instance: Fernet | None = None


def _resolve_keyfile_path() -> Path:
    """Return keyfile path, respecting ARKIVE_CONFIG_DIR env var."""
    config_dir = os.environ.get("ARKIVE_CONFIG_DIR", DEFAULT_KEYFILE_DIR)
    return Path(config_dir) / ".keyfile"


def _get_fernet() -> Fernet:
    """Load or create the master Fernet key, using cached instance if available."""
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance
    keyfile = _resolve_keyfile_path()
    if keyfile.exists():
        # Verify permissions are correct
        current_mode = os.stat(keyfile).st_mode
        if current_mode & 0o777 != 0o600:
            os.chmod(keyfile, 0o600)
        key = keyfile.read_bytes().strip()
    else:
        key = Fernet.generate_key()
        keyfile.parent.mkdir(parents=True, exist_ok=True)
        # Use O_EXCL to atomically create file, preventing TOCTOU race
        try:
            fd = os.open(str(keyfile), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, key)
            finally:
                os.close(fd)
        except FileExistsError:
            # Another process created it between our check and create — read it
            key = keyfile.read_bytes().strip()
    _fernet_instance = Fernet(key)
    return _fernet_instance


def _reset_fernet() -> None:
    """Reset the cached Fernet instance (for testing)."""
    global _fernet_instance
    _fernet_instance = None


def generate_api_key() -> str:
    """Generate a 32-byte random hex API key prefixed with ark_."""
    return f"ark_{secrets.token_hex(32)}"


def hash_api_key(key: str) -> str:
    """SHA-256 hash for storage."""
    return hashlib.sha256(key.encode()).hexdigest()


def verify_api_key(key: str, hashed: str) -> bool:
    """SHA-256 hash and constant-time compare."""
    return hmac.compare_digest(hash_api_key(key), hashed)


def generate_browser_session(api_key_hash: str) -> str:
    """Create an encrypted browser session bound to the current API-key hash."""
    payload = {
        "kind": "browser_session",
        "api_key_hash": api_key_hash,
        "issued_at": int(time.time()),
    }
    return _get_fernet().encrypt(json.dumps(payload).encode()).decode()


def verify_browser_session(token: str, expected_api_key_hash: str, ttl_seconds: int = BROWSER_SESSION_TTL) -> bool:
    """Validate an encrypted browser session and bind it to the active API key."""
    if not token:
        return False
    try:
        decrypted = _get_fernet().decrypt(token.encode(), ttl=ttl_seconds)
        payload = json.loads(decrypted.decode())
    except (InvalidToken, json.JSONDecodeError, TypeError, ValueError):
        return False

    if payload.get("kind") != "browser_session":
        return False
    return hmac.compare_digest(str(payload.get("api_key_hash", "")), expected_api_key_hash)


def encrypt_value(plaintext: str) -> str:
    """Fernet symmetric encrypt, return with enc:v1: prefix."""
    f = _get_fernet()
    token = f.encrypt(plaintext.encode()).decode()
    return f"enc:v1:{token}"


def decrypt_value(ciphertext: str) -> str:
    """If prefixed with enc:v1:, Fernet decrypt. Otherwise return as-is."""
    if not is_encrypted(ciphertext):
        return ciphertext
    f = _get_fernet()
    token = ciphertext[len("enc:v1:") :]
    return f.decrypt(token.encode()).decode()


def is_encrypted(value: str) -> bool:
    """Check for enc:v1: prefix."""
    return value.startswith("enc:v1:")


def generate_password(length: int = 24) -> str:
    """Cryptographically random alphanumeric password."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


# --- Config dict encryption (from v2) ---


def encrypt_config(config_dict: dict, config_dir: str | None = None) -> str:
    """
    Encrypt a config dictionary for storage.

    Returns a string prefixed with 'enc:v1:' followed by the Fernet-encrypted
    JSON payload. This allows detection of encrypted vs plaintext configs
    in the database.

    If config_dir is provided, uses that directory's keyfile. Otherwise uses
    the default KEYFILE_PATH.
    """
    if config_dir is not None:
        fernet = _load_fernet_from_dir(config_dir)
    else:
        fernet = _get_fernet()
    config_json = json.dumps(config_dict).encode("utf-8")
    encrypted = fernet.encrypt(config_json)
    return f"enc:v1:{encrypted.decode('utf-8')}"


def decrypt_config(encrypted_str: str, config_dir: str | None = None) -> dict:
    """
    Decrypt a config string from storage.

    Detects the 'enc:v1:' prefix to determine if decryption is needed.
    Falls back to plain JSON parsing for unencrypted legacy configs.

    If config_dir is provided, uses that directory's keyfile. Otherwise uses
    the default KEYFILE_PATH.
    """
    if encrypted_str and encrypted_str.startswith("enc:v1:"):
        if config_dir is not None:
            fernet = _load_fernet_from_dir(config_dir)
        else:
            fernet = _get_fernet()
        token = encrypted_str[len("enc:v1:") :].encode("utf-8")
        decrypted = fernet.decrypt(token)
        return json.loads(decrypted.decode("utf-8"))
    else:
        # Legacy plain JSON config
        try:
            return json.loads(encrypted_str) if encrypted_str else {}
        except (json.JSONDecodeError, TypeError):
            return {}


# --- SSE one-time tokens ---

_sse_tokens: dict[str, float] = {}
_sse_tokens_lock = threading.Lock()
_SSE_TOKEN_TTL = 60
_SSE_TOKEN_MAX = 10_000  # cap to prevent memory exhaustion


def generate_sse_token() -> str:
    with _sse_tokens_lock:
        prune_sse_tokens()
        if len(_sse_tokens) >= _SSE_TOKEN_MAX:
            raise ValueError("SSE token store full — too many outstanding tokens")
        token = secrets.token_urlsafe(32)
        _sse_tokens[token] = time.time() + _SSE_TOKEN_TTL
    return token


def verify_sse_token(token: str) -> bool:
    with _sse_tokens_lock:
        expiry = _sse_tokens.pop(token, None)
    if expiry is None:
        return False
    return time.time() < expiry


def prune_sse_tokens() -> None:
    """Remove expired tokens. Caller must hold _sse_tokens_lock."""
    now = time.time()
    expired = [t for t, exp in _sse_tokens.items() if exp <= now]
    for t in expired:
        del _sse_tokens[t]


def _reset_sse_tokens() -> None:
    """Clear all SSE tokens. Used by test fixtures."""
    with _sse_tokens_lock:
        _sse_tokens.clear()


def _load_fernet_from_dir(config_dir: str) -> Fernet:
    """Load (or create) the Fernet key from a specific config directory.

    Also sets the module-level _fernet_instance cache so that subsequent
    calls to _get_fernet() / encrypt_value() / decrypt_value() use this key.
    """
    global _fernet_instance
    keyfile_path = Path(config_dir) / ".keyfile"

    if not keyfile_path.exists():
        key = Fernet.generate_key()
        keyfile_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(keyfile_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
    else:
        # Verify permissions are correct
        current_mode = os.stat(keyfile_path).st_mode
        if current_mode & 0o777 != 0o600:
            os.chmod(keyfile_path, 0o600)

    key_data = keyfile_path.read_bytes().strip()
    _fernet_instance = Fernet(key_data)
    return _fernet_instance
