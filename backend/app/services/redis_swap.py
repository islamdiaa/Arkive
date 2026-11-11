"""Shell steps that put a Redis RDB dump in place, shared by guided restore and the restore plan.

Redis reads its data file when it starts and writes it again as it shuts
down, so the file is replaced only while Redis is stopped. With the
append-only file on, Redis loads that instead of the data file (moving it
aside alone makes Redis start empty), so it is rebuilt from the dump:
Redis 7 and later read an RDB "base" file listed in the append-only folder's
manifest, older versions an append-only file that starts with RDB data.
Both were checked on real Redis 6.2, 7.4 and 8.0
(scripts/restore-roundtrip/).

The functions take shell words that are already quoted (``shlex.quote(path)``)
or are shell expressions (``"$D/dump.rdb"``), and return POSIX sh.
"""

from __future__ import annotations

# Appended to a quoted path: the copy gets the time the step runs in its
# name, so the same steps can be run again (with the safety copy, to go back).
KEEP_SUFFIX = '".before-restore-$(date +%Y%m%d-%H%M%S)"'
KEEP_SUFFIX_TEXT = ".before-restore-<date>-<time>"


def keep_copy(path: str) -> str:
    """Copy ``path`` aside (when it exists), keeping owner and mode."""
    return f"if [ -e {path} ]; then cp -a {path} {path}{KEEP_SUFFIX}; fi"


def move_aside(path: str) -> str:
    """Move ``path`` (an append-only file or folder) aside when it exists."""
    return f"if [ -e {path} ]; then mv {path} {path}{KEEP_SUFFIX}; fi"


def aof_folder_from_rdb(rdb: str, folder: str, base_name: str, base_path: str, manifest: str) -> str:
    """Redis 7+: a new append-only folder whose base is the RDB file ``rdb``.

    ``base_name`` is the manifest entry (``<appendfilename>.1.base.rdb``),
    ``base_path`` and ``manifest`` the files inside ``folder``.
    """
    return f"mkdir {folder} && cp {rdb} {base_path} && printf 'file %s seq 1 type b\\n' {base_name} > {manifest}"


def aof_file_from_rdb(rdb: str, aof_file: str) -> str:
    """Redis before 7: an append-only file that starts with RDB data (Redis reads it as a preamble)."""
    return f"cp {rdb} {aof_file}"
