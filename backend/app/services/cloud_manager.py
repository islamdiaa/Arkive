"""Cloud storage manager — rclone config generation and target management."""

import json
import logging
import os

from app.core.config import ArkiveConfig
from app.core.security import decrypt_value
from app.utils.subprocess_runner import run_command

logger = logging.getLogger("arkive.cloud_manager")


class CloudManager:
    """Manages rclone configuration for cloud storage targets."""

    def __init__(self, config: ArkiveConfig):
        self.config = config
        self.rclone_config = config.rclone_config

    async def write_target_config(self, target: dict) -> None:
        """Write or update rclone config section for a target."""
        target_type = target.get("type", "")
        target_id = target.get("id", "")
        cfg = target.get("config", {})

        if target_type == "local":
            return  # Local targets don't need rclone config

        lines = self._read_config()
        lines = self._remove_section(lines, target_id)

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

        lines.extend(section)
        self._write_config(lines)
        logger.info("Wrote rclone config for target %s (%s)", target_id, target_type)

    async def remove_target_config(self, target_id: str) -> None:
        """Remove a target's rclone config section."""
        lines = self._read_config()
        lines = self._remove_section(lines, target_id)
        self._write_config(lines)

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
            env=env,
            timeout=30,
        )

        if result.returncode == 0:
            return {
                "status": "ok",
                "message": "Connection successful",
                "latency_ms": int(result.duration_seconds * 1000),
            }
        return {"status": "error", "message": result.stderr[:200]}

    def _read_config(self) -> list[str]:
        if self.rclone_config.exists():
            return self.rclone_config.read_text().splitlines()
        return []

    def _write_config(self, lines: list[str]) -> None:
        self.rclone_config.parent.mkdir(parents=True, exist_ok=True)
        self.rclone_config.write_text("\n".join(lines) + "\n")
        os.chmod(self.rclone_config, 0o600)

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
        if val and val.startswith("enc:v1:"):
            return decrypt_value(val)
        return val

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
        token = self._format_oauth_token(self._decrypt_cfg(cfg, "token"))
        return [
            f"[{target_id}]",
            "type = dropbox",
            f"token = {token}",
            "",
        ]

    def _gdrive_section(self, target_id: str, cfg: dict) -> list[str]:
        token = self._format_oauth_token(self._decrypt_cfg(cfg, "token"))
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
        folder_id = cfg.get("folder_id", "")
        if folder_id:
            lines.append(f"root_folder_id = {folder_id}")
        lines.append("")
        return lines

    def _s3_section(self, target_id: str, cfg: dict) -> list[str]:
        lines = [
            f"[{target_id}]",
            "type = s3",
            f"provider = {cfg.get('provider', 'Other')}",
            "env_auth = false",
            f"access_key_id = {self._decrypt_cfg(cfg, 'access_key')}",
            f"secret_access_key = {self._decrypt_cfg(cfg, 'secret_key')}",
            f"endpoint = {cfg.get('endpoint', '')}",
        ]
        region = cfg.get("region", "")
        if region:
            lines.append(f"region = {region}")
        lines.append("")
        return lines

    def _wasabi_section(self, target_id: str, cfg: dict) -> list[str]:
        """Wasabi S3-compatible storage."""
        region = cfg.get("region", "us-east-1")
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

    async def _sftp_section(self, target_id: str, cfg: dict) -> list[str]:
        password = self._decrypt_cfg(cfg, "password")
        # Re-encrypt with rclone obscure
        obscured = password
        if password:
            # Use stdin to pass the password to rclone obscure to avoid
            # it appearing in the process list and to prevent injection
            # if the password starts with a dash or contains special chars
            result = await run_command(
                ["rclone", "obscure", "-"],
                timeout=10,
                input_data=password,
            )
            if result.returncode == 0 and result.stdout.strip():
                obscured = result.stdout.strip()
            else:
                # Fallback: pass as positional arg with -- separator
                result = await run_command(
                    ["rclone", "obscure", "--", password],
                    timeout=10,
                )
                if result.returncode == 0:
                    obscured = result.stdout.strip()

        return [
            f"[{target_id}]",
            "type = sftp",
            f"host = {cfg.get('host', '')}",
            f"port = {cfg.get('port', '22')}",
            f"user = {cfg.get('username', '')}",
            f"pass = {obscured}",
            "",
        ]
