"""Cloud storage manager — rclone config generation and target management."""

import asyncio
import json
import logging
import os
import re
import tempfile
from pathlib import Path

from app.core.config import ArkiveConfig
from app.core.security import decrypt_value, encrypt_value  # noqa: F401 (encrypt_value re-exported for callers)
from app.utils.redact import redact_credentials
from app.utils.subprocess_runner import run_command

logger = logging.getLogger("arkive.cloud_manager")

# rclone remote (section) names are used verbatim as INI section headers and in
# ``rclone:<id>:`` repository strings, so they are restricted to a safe charset.
_SECTION_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Any control character (CR/LF in particular) inside a value would let a field
# start a new INI line — e.g. ``ssh = <command>`` for sftp remotes.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


class RcloneConfigError(ValueError):
    """Raised when a target cannot be rendered into a safe rclone section."""


def _safe_value(field: str, value) -> str:
    """Return ``value`` as a string suitable for a single INI line.

    Raises RcloneConfigError if the value contains control characters, which
    would otherwise allow injecting additional rclone directives or sections.
    """
    if value is None:
        return ""
    text = str(value)
    if _CONTROL_CHARS_RE.search(text):
        raise RcloneConfigError(f"Invalid value for '{field}': control characters are not allowed")
    return text


class CloudManager:
    """Manages rclone configuration for cloud storage targets."""

    def __init__(self, config: ArkiveConfig):
        self.config = config
        self.rclone_config = config.rclone_config
        # Serializes read-modify-write cycles on rclone.conf within this process.
        self._config_lock = asyncio.Lock()

    async def write_target_config(self, target: dict) -> None:
        """Write or update the rclone config section for a target (idempotent).

        ``target`` must carry the decrypted provider config under ``config``.
        Raises RcloneConfigError if the target id or any value is unsafe, and
        RuntimeError if the sftp password cannot be obscured.
        """
        target_type = target.get("type", "")
        target_id = str(target.get("id", ""))
        cfg = target.get("config", {}) or {}

        if target_type == "local":
            return  # Local targets don't need rclone config

        if not _SECTION_NAME_RE.match(target_id):
            raise RcloneConfigError(f"Invalid target id for rclone section: {target_id!r}")

        # Build the section first — sftp needs an awaited `rclone obscure`, and we
        # don't want to hold the file lock (or a stale file snapshot) across it.
        if target_type == "b2":
            section = self._b2_section(target_id, cfg)
        elif target_type == "dropbox":
            section = self._dropbox_section(target_id, cfg)
        elif target_type == "gdrive":
            section = self._gdrive_section(target_id, cfg)
        elif target_type == "s3":
            section = self._s3_section(target_id, cfg)
        elif target_type == "sftp":
            section = await self._sftp_section(target_id, cfg)
        elif target_type == "wasabi":
            section = self._wasabi_section(target_id, cfg)
        else:
            logger.warning("Unknown target type: %s", target_type)
            return

        async with self._config_lock:
            lines = self._read_config()
            lines = self._remove_section(lines, target_id)
            # Keep a blank separator between sections without accumulating blanks.
            while lines and not lines[-1].strip():
                lines.pop()
            if lines:
                lines.append("")
            lines.extend(section)
            self._write_config(lines)
        logger.info("Wrote rclone config for target %s (%s)", target_id, target_type)

    async def remove_target_config(self, target_id: str) -> None:
        """Remove a target's rclone config section (no-op if absent)."""
        async with self._config_lock:
            if not self.rclone_config.exists():
                return
            lines = self._read_config()
            new_lines = self._remove_section(lines, target_id)
            if new_lines != lines:
                self._write_config(new_lines)

    async def test_target(self, target: dict) -> dict:
        """Test connectivity to a storage target."""
        target_type = target.get("type", "")
        target_id = target.get("id", "")

        if target_type == "local":
            path = target.get("config", {}).get("path", "")
            if os.path.isdir(path) and os.access(path, os.W_OK):
                return {"status": "ok", "message": "Local path accessible"}
            return {"status": "error", "message": f"Path not accessible: {path}"}

        env = os.environ.copy()
        env["RCLONE_CONFIG"] = str(self.rclone_config)

        result = await run_command(
            ["rclone", "lsd", f"{target_id}:"],
            env=env, timeout=30,
        )

        if result.returncode == 0:
            return {"status": "ok", "message": "Connection successful",
                    "latency_ms": int(result.duration_seconds * 1000)}
        return {"status": "error", "message": redact_credentials(result.stderr[:200])}

    def _read_config(self) -> list[str]:
        if self.rclone_config.exists():
            return self.rclone_config.read_text().splitlines()
        return []

    def _write_config(self, lines: list[str]) -> None:
        """Atomically replace rclone.conf (temp file created 0600, then os.replace).

        Readers (a restic/rclone process starting mid-write) see either the old
        or the new file, never a truncated one, and secrets are never briefly
        world-readable.
        """
        target = Path(self.rclone_config)
        target.parent.mkdir(parents=True, exist_ok=True)
        content = "\n".join(lines).rstrip("\n") + "\n" if lines else ""
        fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), prefix=".rclone.conf.", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, target)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        os.chmod(target, 0o600)

    def _remove_section(self, lines: list[str], section_name: str) -> list[str]:
        result = []
        skip = False
        for line in lines:
            if line.strip() == f"[{section_name}]":
                skip = True
                continue
            if skip and line.strip().startswith("["):
                skip = False
            if not skip:
                result.append(line)
        return result

    def _decrypt_cfg(self, cfg: dict, key: str) -> str:
        val = cfg.get(key, "")
        if isinstance(val, str) and val.startswith("enc:v1:"):
            val = decrypt_value(val)
        return _safe_value(key, val)

    def _get(self, cfg: dict, key: str, default="") -> str:
        return _safe_value(key, cfg.get(key, default))

    def _b2_section(self, target_id: str, cfg: dict) -> list[str]:
        return [
            f"[{target_id}]",
            "type = b2",
            f"account = {self._decrypt_cfg(cfg, 'key_id')}",
            f"key = {self._decrypt_cfg(cfg, 'app_key')}",
            "",
        ]

    @staticmethod
    def _format_oauth_token(token: str) -> str:
        """Ensure OAuth token is in rclone JSON format.

        Rclone expects: {"access_token":"...","token_type":"bearer",...}
        If the token is already JSON, use it as-is. Otherwise wrap it.
        """
        if not token:
            return ""
        # Already JSON?
        if token.strip().startswith("{"):
            return token
        # Bare access token — wrap in rclone format
        return json.dumps({"access_token": token, "token_type": "bearer"})

    def _dropbox_section(self, target_id: str, cfg: dict) -> list[str]:
        token = _safe_value("token", self._format_oauth_token(self._decrypt_cfg(cfg, "token")))
        return [
            f"[{target_id}]",
            "type = dropbox",
            f"token = {token}",
            "",
        ]

    def _gdrive_section(self, target_id: str, cfg: dict) -> list[str]:
        token = _safe_value("token", self._format_oauth_token(self._decrypt_cfg(cfg, "token")))
        client_id = self._decrypt_cfg(cfg, "client_id")
        client_secret = self._decrypt_cfg(cfg, "client_secret")
        lines = [
            f"[{target_id}]",
            "type = drive",
            f"token = {token}",
        ]
        if client_id:
            lines.append(f"client_id = {client_id}")
        if client_secret:
            lines.append(f"client_secret = {client_secret}")
        folder_id = self._get(cfg, "folder_id")
        if folder_id:
            lines.append(f"root_folder_id = {folder_id}")
        lines.append("")
        return lines

    def _s3_section(self, target_id: str, cfg: dict) -> list[str]:
        lines = [
            f"[{target_id}]",
            "type = s3",
            f"provider = {self._get(cfg, 'provider', 'Other') or 'Other'}",
            "env_auth = false",
            f"access_key_id = {self._decrypt_cfg(cfg, 'access_key')}",
            f"secret_access_key = {self._decrypt_cfg(cfg, 'secret_key')}",
            f"endpoint = {self._get(cfg, 'endpoint')}",
        ]
        region = self._get(cfg, "region")
        if region:
            lines.append(f"region = {region}")
        lines.append("")
        return lines

    def _wasabi_section(self, target_id: str, cfg: dict) -> list[str]:
        """Wasabi S3-compatible storage."""
        region = self._get(cfg, "region", "us-east-1") or "us-east-1"
        return [
            f"[{target_id}]",
            "type = s3",
            "provider = Wasabi",
            "env_auth = false",
            f"access_key_id = {self._decrypt_cfg(cfg, 'access_key')}",
            f"secret_access_key = {self._decrypt_cfg(cfg, 'secret_key')}",
            f"endpoint = s3.{region}.wasabisys.com",
            f"region = {region}",
            "",
        ]

    async def _obscure(self, password: str) -> str:
        """Obscure a password with ``rclone obscure -`` (password via stdin only).

        There is deliberately no argv fallback: passing the secret on the
        command line exposes it in the process list and in command logs.
        """
        result = await run_command(
            ["rclone", "obscure", "-"],
            timeout=30,
            input_data=password,
        )
        obscured = result.stdout.strip() if result.stdout else ""
        if result.returncode != 0 or not obscured:
            detail = redact_credentials((result.stderr or "").strip()[:200]).replace(password, "***")
            raise RuntimeError(
                f"rclone obscure failed (rc={result.returncode}); sftp password was not written"
                + (f": {detail}" if detail else "")
            )
        return _safe_value("pass", obscured)

    async def _sftp_section(self, target_id: str, cfg: dict) -> list[str]:
        password = self._decrypt_cfg(cfg, "password")
        host = self._get(cfg, "host")
        port = self._get(cfg, "port", "22") or "22"
        user = _safe_value("username", cfg.get("username") or cfg.get("user") or "")
        key_file = self._get(cfg, "key_file")

        lines = [
            f"[{target_id}]",
            "type = sftp",
            f"host = {host}",
            f"port = {port}",
            f"user = {user}",
        ]
        if password:
            lines.append(f"pass = {await self._obscure(password)}")
        if key_file:
            lines.append(f"key_file = {key_file}")
        lines.append("")
        return lines
