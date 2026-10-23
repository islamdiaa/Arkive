"""Recovery kit: everything needed to open the backups if this server is lost.

The restic password lives in Arkive's database, encrypted with a keyfile on
the same disk. If that disk dies and the password isn't stored anywhere else,
every snapshot is unreadable. The kit packages the password, where each
repository lives, and how to restore with or without Arkive.
"""

import asyncio
import logging
from datetime import datetime, timezone

import aiosqlite

from app import __version__
from app.core.config import ArkiveConfig
from app.core.security import decrypt_config, decrypt_value
from app.services.host_identity import resolve_hostname
from app.services.repo_paths import build_repo_path
from app.utils.mounts import host_path_for

logger = logging.getLogger("arkive.recovery_kit")

# Target config keys never copied into the kit's per-target details.
_SECRET_MARKERS = ("key", "secret", "password", "token", "credential", "pass")


def _public_details(config: dict) -> dict:
    return {
        k: v for k, v in config.items()
        if isinstance(v, (str, int, float, bool))
        and not any(marker in k.lower() for marker in _SECRET_MARKERS)
    }


async def _encryption_password(db: aiosqlite.Connection) -> str:
    cursor = await db.execute(
        "SELECT key, value FROM settings WHERE key IN ('encryption_password', 'restic_password')"
    )
    values = {row[0]: row[1] for row in await cursor.fetchall()}
    raw = values.get("encryption_password") or values.get("restic_password") or ""
    return decrypt_value(raw) if raw else ""


async def build_recovery_kit(db: aiosqlite.Connection, config: ArkiveConfig, docker_client=None) -> dict:
    password = await _encryption_password(db)
    if not password:
        raise ValueError("No encryption password is configured yet")

    keyfile = config.config_dir / ".keyfile"
    try:
        keyfile_text = keyfile.read_text().strip()
    except OSError:
        keyfile_text = ""

    cursor = await db.execute("SELECT id, name, type, enabled, config FROM storage_targets ORDER BY created_at")
    targets = []
    for target_id, name, target_type, enabled, raw_config in await cursor.fetchall():
        try:
            target_config = decrypt_config(raw_config or "{}", str(config.config_dir))
        except Exception as exc:
            logger.warning("Recovery kit: cannot decrypt config for target %s: %s", target_id, exc)
            target_config = {}
        target = {"id": target_id, "type": target_type, "config": target_config}
        repository = build_repo_path(target)
        host_path = None
        if target_type == "local":
            host_path = await asyncio.to_thread(host_path_for, repository, docker_client)
        targets.append({
            "id": target_id,
            "name": name,
            "type": target_type,
            "enabled": bool(enabled),
            "repository": repository,
            "host_path": host_path,
            "details": _public_details(target_config),
        })

    cursor = await db.execute("SELECT value FROM settings WHERE key = 'server_name'")
    row = await cursor.fetchone()
    configured_name = str(row[0]).strip() if row and row[0] else ""
    try:
        server_name = await asyncio.wait_for(
            asyncio.to_thread(resolve_hostname, docker_client=docker_client,
                              settings={"server_name": configured_name}),
            timeout=10)
    except Exception:
        server_name = configured_name

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    instructions = _instructions(targets, str(config.dump_dir))
    kit = {
        "generated_at": generated_at,
        "server_name": server_name,
        "arkive_version": __version__,
        "encryption_password": password,
        "config_keyfile": keyfile_text,
        "targets": targets,
        "instructions": instructions,
    }
    kit["text"] = render_text(kit)
    return kit


def _instructions(targets: list[dict], dump_dir: str) -> list[str]:
    steps = [
        "Keep this kit somewhere other than this server: a password manager, or printed and "
        "stored safely. Anyone holding it can read your backups.",
        "Restore with Arkive: install Arkive on any machine, choose the same storage target in "
        "setup and enter the encryption password above. Existing snapshots are picked up as-is.",
        "Restore without Arkive: install restic (https://restic.net), then run "
        "`restic -r <repository> snapshots` and `restic -r <repository> restore latest --target /restore`, "
        "entering the encryption password when asked (or exporting RESTIC_PASSWORD).",
    ]
    if any(t["repository"].startswith("rclone:") for t in targets):
        steps.append(
            "Cloud repositories are reached through rclone: run `rclone config`, create a remote whose "
            "name is the part between `rclone:` and the next colon in the repository string, using your "
            "provider credentials (not included in this kit)."
        )
    if any(t["type"] == "local" for t in targets):
        steps.append(
            "Local repositories: the host path is where the repository lives on this server's disks. "
            "Copy that folder to the new machine (or mount the disk) and point restic at it."
        )
    steps.append(
        f"Database dumps are inside each snapshot under {dump_dir}. The file "
        "arkive-dumps-manifest.json there lists which dump belongs to which container and database."
    )
    steps.append(
        "The config keyfile is only needed to bring back Arkive's own settings with their stored "
        "credentials (arkive_self.db in the dump folder): place it at /config/.keyfile next to the "
        "restored database. Your backups themselves only need the encryption password."
    )
    return steps


def render_text(kit: dict) -> str:
    lines = [
        "ARKIVE RECOVERY KIT",
        "===================",
        "",
        "Contains secrets. Store it somewhere other than the server it protects.",
        "",
        f"Server:          {kit.get('server_name') or '(unknown)'}",
        f"Generated:       {kit['generated_at']}",
        f"Arkive version:  {kit['arkive_version']}",
        "",
        "ENCRYPTION PASSWORD (restic)",
        "----------------------------",
        kit["encryption_password"],
        "",
        "REPOSITORIES",
        "------------",
    ]
    if not kit["targets"]:
        lines.append("(no storage targets configured)")
    for target in kit["targets"]:
        state = "" if target["enabled"] else " (disabled)"
        lines.append(f"* {target['name']} [{target['type']}]{state}")
        lines.append(f"    repository: {target['repository']}")
        if target.get("host_path"):
            lines.append(f"    host path:  {target['host_path']}")
        for key, value in sorted(target["details"].items()):
            lines.append(f"    {key}: {value}")
    lines += ["", "HOW TO RESTORE", "--------------"]
    for i, step in enumerate(kit["instructions"], 1):
        lines.append(f"{i}. {step}")
    lines += [
        "",
        "CONFIG KEYFILE (/config/.keyfile)",
        "---------------------------------",
        kit["config_keyfile"] or "(not found)",
        "",
    ]
    return "\n".join(lines)
