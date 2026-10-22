"""Arkive — Automated disaster recovery for Unraid servers."""
import os

# Release builds pass the image tag in via ARKIVE_VERSION (see Dockerfile).
__version__ = os.environ.get("ARKIVE_VERSION") or "0.4.0"
