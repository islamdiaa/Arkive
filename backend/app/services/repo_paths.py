"""Helpers for building restic repository paths from Arkive targets."""

import posixpath

_OBJECT_STORE_TYPES = {"b2", "s3", "wasabi"}


def build_repo_path(target: dict, target_id_fallback: str = "TARGET") -> str:
    """Return the effective restic repository path for a target."""
    if target.get("type") == "local":
        local_path = target.get("config", {}).get("path", "/data/local-backup")
        return f"{local_path}/arkive-repo"

    config = target.get("config", {}) or {}
    path_parts: list[str] = []

    bucket = str(config.get("bucket", "") or "").strip().strip("/")
    if target.get("type") in _OBJECT_STORE_TYPES and bucket:
        path_parts.append(bucket)

    remote_path = str(config.get("remote_path", "") or "").strip().strip("/")
    if remote_path:
        path_parts.append(remote_path)

    path_parts.append("arkive-backups")
    repo_path = posixpath.join(*path_parts)
    return f"rclone:{target.get('id', target_id_fallback)}:{repo_path}"
