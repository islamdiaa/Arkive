"""MariaDB/MySQL accounts dump: application users, roles and their grants.

``mysqldump --databases`` dumps schemas and rows, not the accounts in the
``mysql`` system schema, so a server restored from database dumps alone has
no application user and the app cannot log in. This module builds one SQL
file per server that recreates those accounts (authentication included) and
their privileges.

Mechanism (the same on MariaDB 10.4+ and MySQL 8):

1. ``SELECT VERSION()`` tells MariaDB from MySQL.
2. ``mysql.user`` lists the accounts (user and host hex-encoded, so no name
   can break the parsing; MariaDB also says which are roles).
3. Per account, ``SHOW CREATE USER`` (authentication plugin, password hash,
   TLS requirements, resource limits, lock and expiry) and ``SHOW GRANTS``.
   MySQL 8.0.17+ prints binary password hashes (caching_sha2_password) as hex
   literals with ``print_identified_with_as_hex``; without it they contain
   raw control bytes. ``mariadb-dump --system=users`` is not used: it has no
   way to leave out root and the image's own accounts, and MySQL has no
   equivalent at all (``mysqlpump`` is gone in 8.4).

The file is written to restore onto a server that may already have some of
the accounts (the image's init scripts create ``MYSQL_USER``):
``CREATE USER IF NOT EXISTS`` followed by ``ALTER USER`` with the same
clauses, so an existing account takes the dumped password too, then every
grant, then default roles (which need the role grants first). Grants are
additive: nothing on the new server is revoked.

Accounts that belong to the server or the image rather than to an
application are left out (``excluded_reason``). ``root`` in particular: its
password on the new server comes from that container's environment, which
the image's entrypoint, healthcheck, Arkive's own dumps and the restore
commands all use; loading the old hash over it would lock them out whenever
the two differ.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

ACCOUNTS_KIND = "accounts"
# db_name / file label of the per-server accounts dump.
ACCOUNTS_DB_NAME = "accounts"
ACCOUNTS_HEADER = "-- Arkive MariaDB/MySQL accounts dump"
# Starts like mysqldump's own trailer ("-- Dump completed on ..."), so every
# completeness check that knows mysqldump dumps recognises this file too.
ACCOUNTS_TRAILER_PREFIX = "-- Dump completed (Arkive accounts dump"

VERSION_SQL = "SELECT VERSION()"
MARIADB_LIST_SQL = "SELECT HEX(User), HEX(Host), is_role FROM mysql.user ORDER BY User, Host"
MYSQL_LIST_SQL = "SELECT HEX(User), HEX(Host), 'N' FROM mysql.user ORDER BY User, Host"

# Accounts the server itself creates (definer/system accounts).
SYSTEM_USERS = frozenset(
    {"mariadb.sys", "mysql.sys", "mysql.session", "mysql.infoschema", "mysql.pxc.internal.session"}
)
# Maintenance accounts of the MariaDB image (healthcheck.sh, random password
# kept in the data directory) and of Debian packages (/etc/mysql/debian.cnf).
# Their passwords live next to the server they were created for.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
IMAGE_USERS = frozenset({"healthcheck", "debian-sys-maint"})

_STATEMENT_PREFIXES = ("GRANT ", "REVOKE ", "SET DEFAULT ROLE ")
_ACCOUNT = r"`(?:[^`]|``)*`(?:@`(?:[^`]|``)*`)?"
# MySQL's SHOW CREATE USER puts DEFAULT ROLE after the authentication clause;
# ALTER USER does not accept it there, and the roles must be granted first.
_DEFAULT_ROLE_RE = re.compile(rf" DEFAULT ROLE ({_ACCOUNT}(?:,{_ACCOUNT})*)")
_MYSQL_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class AccountsDumpError(Exception):
    """The accounts could not be dumped completely; nothing may be written."""


@dataclass
class Account:
    user: str
    host: str
    is_role: bool = False

    @property
    def is_public(self) -> bool:
        """MariaDB 10.11+'s PUBLIC pseudo-role (grants to every account)."""
        return self.is_role and self.user == "PUBLIC" and self.host == ""

    @property
    def quoted(self) -> str:
        if self.is_public:
            return "PUBLIC"
        user = quote_identifier(self.user)
        return user if self.is_role and not self.host else f"{user}@{quote_identifier(self.host)}"

    @property
    def label(self) -> str:
        return self.user if self.is_role and not self.host else f"{self.user}@{self.host}"


@dataclass
class AccountsDump:
    server_version: str
    flavor: str  # mariadb | mysql
    included: list[Account] = field(default_factory=list)
    excluded: list[tuple[Account, str]] = field(default_factory=list)
    sql: str = ""


def quote_identifier(value: str) -> str:
    return "`" + value.replace("`", "``") + "`"


def excluded_reason(account: Account) -> str | None:
    """Why an account is left out of the dump, or None to dump it."""
    if account.is_public:
        return None
    if account.user == "root":
        return "root: its password comes from the new container's environment"
    if account.user in SYSTEM_USERS:
        return "system account"
    if account.user in IMAGE_USERS and account.host in LOCAL_HOSTS:
        return "maintenance account of the image"
    if account.user == "":
        return "anonymous account"
    return None


def _flavor(version: str) -> str:
    return "mariadb" if "mariadb" in version.lower() else "mysql"


def _supports_hex_hashes(version: str) -> bool:
    """MySQL 8.0.17+ can print binary password hashes as hex literals."""
    match = _MYSQL_VERSION_RE.match(version)
    if not match:
        return False
    return tuple(int(part) for part in match.groups()) >= (8, 0, 17)


def parse_accounts(rows: list[str], flavor: str) -> list[Account]:
    accounts: list[Account] = []
    for row in rows:
        parts = row.split("\t")
        if len(parts) != 3:
            raise AccountsDumpError(f"unexpected account listing row: {row[:120]!r}")
        try:
            user = bytes.fromhex(parts[0]).decode("utf-8")
            host = bytes.fromhex(parts[1]).decode("utf-8")
        except ValueError as e:
            raise AccountsDumpError(f"unexpected account listing row: {row[:120]!r}") from e
        accounts.append(Account(user=user, host=host, is_role=flavor == "mariadb" and parts[2].strip() == "Y"))
    return accounts


def _lines(text: str) -> list[str]:
    return [line for line in text.split("\n") if line != ""]


def _check_statement(line: str, account: Account) -> str:
    if _CONTROL_RE.search(line):
        raise AccountsDumpError(
            f"{account.label}: SHOW output contains control characters "
            "(a binary password hash this server version cannot print safely)"
        )
    return line


def dump_accounts(run_sql: Callable[[str], str]) -> AccountsDump:
    """Build the accounts dump of one server.

    ``run_sql(sql)`` runs ``sql`` with the server's client in batch mode
    (``-N -B -r``: no column names, tab-separated, unescaped) and returns its
    stdout; it raises (any exception) when the client fails. Any failure is
    an AccountsDumpError or the caller's exception: a partial dump is never
    returned.
    """
    version_lines = _lines(run_sql(VERSION_SQL))
    if len(version_lines) != 1:
        raise AccountsDumpError("could not read the server version")
    version = version_lines[0].strip()
    flavor = _flavor(version)
    hex_hashes = flavor == "mysql" and _supports_hex_hashes(version)
    listing = run_sql(MARIADB_LIST_SQL if flavor == "mariadb" else MYSQL_LIST_SQL)
    accounts = parse_accounts(_lines(listing), flavor)
    if not any(a.user == "root" for a in accounts):
        # Every server has root unless it was renamed; an empty or filtered
        # listing (missing privileges on some forks) must not pass as "no accounts".
        raise AccountsDumpError("the account listing does not include root; the listing is incomplete")

    dump = AccountsDump(server_version=version, flavor=flavor)
    roles: list[str] = []
    users: list[str] = []
    grants: list[str] = []
    default_roles: list[str] = []
    for account in accounts:
        reason = excluded_reason(account)
        if reason:
            dump.excluded.append((account, reason))
            continue
        dump.included.append(account)
        if account.is_role:
            output = _lines(run_sql(f"SHOW GRANTS FOR {account.quoted}"))
            if not account.is_public:
                roles.append(f"CREATE ROLE IF NOT EXISTS {account.quoted};")
        else:
            prefix = "SET SESSION print_identified_with_as_hex = ON; " if hex_hashes else ""
            output = _lines(run_sql(f"{prefix}SHOW CREATE USER {account.quoted}; SHOW GRANTS FOR {account.quoted}"))
            if not output or not output[0].startswith("CREATE USER "):
                raise AccountsDumpError(f"{account.label}: SHOW CREATE USER returned no statement")
            create = _check_statement(output.pop(0), account)
            if "<secret>" in create:
                raise AccountsDumpError(f"{account.label}: the server hid the password hash (missing privileges)")
            match = _DEFAULT_ROLE_RE.search(create)
            if match:
                default_roles.append(f"ALTER USER {account.quoted} DEFAULT ROLE {match.group(1)};")
                create = create[: match.start()] + create[match.end() :]
            clauses = create[len("CREATE USER ") :]
            users.append(f"CREATE USER IF NOT EXISTS {clauses};")
            users.append(f"ALTER USER {clauses};")
        if not output:
            raise AccountsDumpError(f"{account.label}: SHOW GRANTS returned nothing")
        for line in output:
            line = _check_statement(line, account)
            if not line.startswith(_STATEMENT_PREFIXES):
                raise AccountsDumpError(f"{account.label}: unexpected SHOW GRANTS output: {line[:120]!r}")
            if line.startswith("SET DEFAULT ROLE "):
                default_roles.append(f"{line};")
            else:
                grants.append(f"{line};")

    included_users = [a for a in dump.included if not a.is_role]
    header = [
        ACCOUNTS_HEADER,
        f"-- Server version: {version}",
        f"-- Accounts: {', '.join(a.label for a in dump.included) or 'none'}",
        "-- Not dumped: " + (", ".join(f"{a.label} ({why})" for a, why in dump.excluded) or "none"),
        "-- Restore after this server's databases (table and column grants need the tables).",
        "",
    ]
    body = roles + users + grants + default_roles
    trailer = (
        f"{ACCOUNTS_TRAILER_PREFIX}: {len(included_users)} users, {len(dump.included) - len(included_users)} roles)"
    )
    dump.sql = "\n".join([*header, *body, "", trailer]) + "\n"
    return dump
