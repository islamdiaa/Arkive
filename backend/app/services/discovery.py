"""Container discovery engine using docker-py."""

import asyncio
import logging
import os
import re
import time
from pathlib import Path

import yaml

from app.core.config import ArkiveConfig
from app.models.discovery import DiscoveredContainer, DiscoveredDatabase
from app.services.db_dumper import postgres_user_from_env

logger = logging.getLogger("arkive.discovery")

# Image regex patterns for DB detection
POSTGRES_PATTERNS = [
    re.compile(r"^postgres(:\S+)?$"), re.compile(r"^library/postgres"),
    re.compile(r"^bitnami/postgresql"), re.compile(r"^timescale/timescaledb"),
    re.compile(r"immich.*postgres"),
]
MYSQL_PATTERNS = [
    re.compile(r"^mysql(:\S+)?$"), re.compile(r"^mariadb(:\S+)?$"),
    re.compile(r"^library/mysql"), re.compile(r"^library/mariadb"),
    re.compile(r"^linuxserver/mariadb"), re.compile(r"^bitnami/mariadb"),
]
MONGO_PATTERNS = [
    re.compile(r"^mongo(:\S+)?$"), re.compile(r"^library/mongo"), re.compile(r"^bitnami/mongodb"),
]
REDIS_PATTERNS = [
    re.compile(r"^redis(:\S+)?$"), re.compile(r"^library/redis"),
    re.compile(r"^bitnami/redis"), re.compile(r"^valkey/valkey"),
]

# Every database in a Postgres cluster is dumped, not only the one named by the
# container env. Enumerate connectable, non-template databases owned by the
# connecting role (superusers are members of every role, so they see all);
# databases a non-superuser cannot read would only fail every dump.
PG_LIST_DATABASES_SQL = (
    "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate "
    "AND has_database_privilege(datname, 'CONNECT') AND pg_has_role(datdba, 'MEMBER') "
    "ORDER BY 1"
)
# The "postgres" maintenance database is only dumped when it holds user tables
# (or is the database the container is configured with).
PG_HAS_USER_TABLES_SQL = (
    "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_class c "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
    "AND n.nspname NOT LIKE 'pg\\_%')"
)
PG_MAINTENANCE_DB = "postgres"
# Keep a hung server from stalling discovery.
_PSQL_ENV = {"PGCONNECT_TIMEOUT": "5", "PGOPTIONS": "-c statement_timeout=15000"}

SQLITE_HEADER = b"SQLite format 3\x00"
SKIP_DIRS = {
    "media", "movies", "tv", "downloads", "music", "audiobooks", "photos",
    "transcode", "cache", "tmp", "node_modules", ".git",
    # Backup artifact directories should never be rediscovered as live DBs.
    "dump", "dumps", "backup", "backups", "restore", "restores",
}
SKIP_FILES = {"Thumbs.db"}
MAX_DEPTH = 3


class DiscoveryEngine:
    """Discovers Docker containers and their databases."""

    def __init__(self, docker_client, config: ArkiveConfig):
        self.docker = docker_client
        self.config = config
        self.profiles: list[dict] = []
        # Per-scan cache of enumerated Postgres databases, keyed by container
        # name + configured database (a companion DB container is reached from
        # its app container too).
        self._pg_database_cache: dict[str, list[DiscoveredDatabase]] = {}
        self._load_profiles()

    def _load_profiles(self) -> None:
        """Load YAML profiles from profiles directory."""
        profiles_dir = self.config.profiles_dir
        if not profiles_dir.exists():
            logger.warning("Profiles directory not found: %s", profiles_dir)
            return
        for f in sorted(profiles_dir.glob("*.yaml")):
            try:
                profile = yaml.safe_load(f.read_text())
                if profile:
                    self.profiles.append(profile)
            except Exception as e:
                logger.error("Failed to load profile %s: %s", f.name, e)

    def _match_profile(self, image: str) -> dict | None:
        """Match container image against profiles."""
        for profile in self.profiles:
            if profile.get("name") == "_fallback":
                continue
            patterns = profile.get("image_patterns", [])
            for pattern in patterns:
                # Profiles use glob-style wildcards (e.g. "postgres:*")
                regex = pattern.replace("*", ".*")
                if re.search(regex, image):
                    return profile
        return None

    def _get_fallback_profile(self) -> dict | None:
        """Get the fallback profile."""
        for profile in self.profiles:
            if profile.get("name") == "_fallback":
                return profile
        return None

    def _rewrite_path(self, host_path: str) -> str:
        """Rewrite Unraid cache/disk paths to /mnt/user/ paths."""
        if host_path.startswith("/mnt/cache/"):
            return host_path.replace("/mnt/cache/", "/mnt/user/", 1)
        match = re.match(r"^/mnt/disk\d+/(.+)$", host_path)
        if match:
            return f"/mnt/user/{match.group(1)}"
        return host_path

    def _get_env_vars(self, container) -> dict[str, str]:
        """Extract environment variables from container."""
        env_list = container.attrs.get("Config", {}).get("Env", [])
        env_dict = {}
        for item in env_list:
            if "=" in item:
                key, _, value = item.partition("=")
                env_dict[key] = value
        return env_dict

    def _get_image_name(self, container) -> str:
        """Get the image name from container."""
        try:
            tags = container.image.tags
            if tags:
                return tags[0]
        except Exception:
            pass
        return container.attrs.get("Config", {}).get("Image", "unknown")

    def _get_mounts(self, container) -> list[dict]:
        """Get container mounts with path rewriting."""
        mounts = container.attrs.get("Mounts", [])
        result = []
        for m in mounts:
            source = self._rewrite_path(m.get("Source", ""))
            result.append({
                "type": m.get("Type", ""),
                "source": source,
                "destination": m.get("Destination", ""),
                "rw": m.get("RW", True),
            })
        return result

    @staticmethod
    def _normalize_hint(value: str) -> str:
        """Normalize compose/container identifiers for loose matching."""
        return re.sub(r"[^a-z0-9]+", "", (value or "").lower())

    @staticmethod
    def _compose_project(container) -> str:
        """Get docker compose project label when available."""
        return str(container.labels.get("com.docker.compose.project", "") or "")

    @staticmethod
    def _compose_service(container) -> str:
        """Get docker compose service label when available."""
        return str(container.labels.get("com.docker.compose.service", "") or "")

    def _compose_depends_on_services(self, container) -> list[str]:
        """Parse docker compose depends_on label into service names."""
        raw = str(container.labels.get("com.docker.compose.depends_on", "") or "").strip()
        if not raw:
            return []
        services: list[str] = []
        for item in raw.split(","):
            service = item.split(":", 1)[0].strip()
            if service:
                services.append(service)
        return services

    def _find_companion_container(
        self,
        source_container,
        all_containers: list,
        *,
        hint: str,
        expected_db_type: str,
    ):
        """Resolve a companion DB container by compose service/name hint."""
        if not hint:
            return None

        normalized_hint = self._normalize_hint(hint)
        source_project = self._compose_project(source_container)
        best_match = None
        best_score = -1

        for candidate in all_containers:
            if candidate.name == source_container.name:
                continue
            candidate_image_type = self._detect_image_type(self._get_image_name(candidate))
            if candidate_image_type != expected_db_type:
                continue

            score = 0
            candidate_project = self._compose_project(candidate)
            if source_project and candidate_project == source_project:
                score += 100
            elif source_project and candidate_project and candidate_project != source_project:
                continue

            names_to_match = {
                candidate.name,
                self._compose_service(candidate),
                str(candidate.attrs.get("Config", {}).get("Hostname", "") or ""),
            }

            networks = candidate.attrs.get("NetworkSettings", {}).get("Networks", {}) or {}
            for network_data in networks.values():
                for alias in network_data.get("Aliases", []) or []:
                    names_to_match.add(str(alias or ""))

            normalized_names = {
                self._normalize_hint(name)
                for name in names_to_match
                if name
            }

            if normalized_hint in normalized_names:
                score += 50
            else:
                composed_prefixes = {
                    self._normalize_hint(f"{source_project}-{hint}"),
                    self._normalize_hint(f"{source_project}_{hint}"),
                }
                candidate_name_normalized = self._normalize_hint(candidate.name)
                if any(candidate_name_normalized.startswith(prefix) for prefix in composed_prefixes if prefix):
                    score += 25

            if score > best_score:
                best_match = candidate
                best_score = score

        return best_match if best_score > 0 else None

    def _scan_sqlite_files(self, host_path: str) -> list[str]:
        """Scan a host path for SQLite files with depth limit."""
        found = []
        if not os.path.isdir(host_path):
            return found

        base_depth = host_path.rstrip("/").count("/")
        for dirpath, dirnames, filenames in os.walk(host_path):
            current_depth = dirpath.rstrip("/").count("/") - base_depth
            if current_depth >= MAX_DEPTH:
                dirnames.clear()
                continue
            # Skip known large directories
            dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS]

            for fname in filenames:
                if fname in SKIP_FILES:
                    continue
                if fname.endswith(("-journal", "-wal", "-shm")):
                    continue
                if fname.endswith((".sqlite3", ".db")):
                    fpath = os.path.join(dirpath, fname)
                    try:
                        with open(fpath, "rb") as f:
                            header = f.read(16)
                            if header == SQLITE_HEADER:
                                found.append(fpath)
                    except (PermissionError, OSError):
                        continue
        return found

    def _should_scan_sqlite_mount(self, source: str) -> bool:
        """Only scan app-specific mounts, not broad share roots like /mnt/user/appdata."""
        shares_root = Path(self.config.user_shares_path)
        normalized = Path(os.path.normpath(source))
        try:
            relative = normalized.relative_to(shares_root)
        except ValueError:
            return False
        return len(relative.parts) >= 2

    @staticmethod
    def _is_current_container(container) -> bool:
        """Skip the Arkive container itself to avoid rediscovering its own config volume."""
        current_container_id = os.environ.get("HOSTNAME", "").strip()
        if not current_container_id:
            return False
        container_id = str(getattr(container, "id", "") or "")
        return container_id.startswith(current_container_id)

    def _detect_postgres(self, container, env: dict, mounts: list[dict]) -> list[DiscoveredDatabase]:
        """Detect Postgres databases: the env-configured one plus every other
        database in the cluster (see ``_postgres_databases``)."""
        db_name = env.get("POSTGRES_DB") or env.get("DB_DATABASE_NAME") or env.get("DB_NAME") or "postgres"
        return self._postgres_databases(container, env, db_name)

    def _psql_query(self, container, user: str, database: str, sql: str) -> list[str] | None:
        """Run a read-only query with psql inside ``container``; None on failure."""
        try:
            exit_code, output = container.exec_run(
                ["psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-U", user, "-d", database, "-c", sql],
                environment=_PSQL_ENV, demux=True,
            )
        except Exception as e:
            logger.debug("psql in %s failed: %s", getattr(container, "name", "?"), e)
            return None
        stdout, stderr = output if isinstance(output, tuple) else (output, b"")
        if exit_code != 0 or not isinstance(stdout, (bytes, bytearray)):
            if isinstance(stderr, (bytes, bytearray)) and stderr:
                logger.debug("psql in %s exited %s: %s", getattr(container, "name", "?"), exit_code,
                             stderr.decode(errors="replace").strip()[:300])
            return None
        return [line for line in stdout.decode(errors="replace").splitlines() if line.strip()]

    def _enumerate_postgres_databases(self, container, env: dict, configured_db: str) -> list[str] | None:
        """Database names in the container's cluster, or None when they can't be listed."""
        if getattr(container, "status", None) != "running":
            return None
        user = postgres_user_from_env(env)
        for connect_db in dict.fromkeys([configured_db, PG_MAINTENANCE_DB]):
            names = self._psql_query(container, user, connect_db, PG_LIST_DATABASES_SQL)
            if names is not None:
                return names
        return None

    def _postgres_databases(self, container, env: dict, configured_db: str) -> list[DiscoveredDatabase]:
        """All databases to dump for a Postgres container.

        The configured database always comes first (so nothing discovered
        before enumeration existed disappears). Other databases come from
        ``PG_LIST_DATABASES_SQL``; the ``postgres`` maintenance database only
        when it contains user tables. When enumeration fails (container not
        running, psql missing, auth error) only the configured database is
        returned, as before.
        """
        cache = self.__dict__.setdefault("_pg_database_cache", {})
        cache_key = f"{container.name}\x00{configured_db}"
        cached = cache.get(cache_key)
        if cached is not None:
            return list(cached)

        names = [configured_db]
        enumerated = self._enumerate_postgres_databases(container, env, configured_db)
        if enumerated is None:
            logger.info("Postgres %s: could not enumerate databases; using %s only",
                        container.name, configured_db)
        else:
            user = postgres_user_from_env(env)
            for name in enumerated:
                if name in names:
                    continue
                if name == PG_MAINTENANCE_DB:
                    has_tables = self._psql_query(container, user, PG_MAINTENANCE_DB, PG_HAS_USER_TABLES_SQL)
                    if has_tables != ["t"]:
                        logger.debug("Postgres %s: skipping empty maintenance database", container.name)
                        continue
                names.append(name)

        databases = [
            DiscoveredDatabase(container_name=container.name, db_type="postgres", db_name=name, host_path=None)
            for name in names
        ]
        cache[cache_key] = databases
        return list(databases)

    def _detect_mysql(self, container, env: dict) -> list[DiscoveredDatabase]:
        """Detect MySQL/MariaDB databases from env vars."""
        db_name = env.get("MYSQL_DATABASE") or env.get("MARIADB_DATABASE") or "mysql"
        return [DiscoveredDatabase(
            container_name=container.name,
            db_type="mariadb",
            db_name=db_name,
            host_path=None,
        )]

    def _detect_mongo(self, container, env: dict) -> list[DiscoveredDatabase]:
        """Detect MongoDB databases."""
        db_name = env.get("MONGO_INITDB_DATABASE") or "admin"
        return [DiscoveredDatabase(
            container_name=container.name,
            db_type="mongodb",
            db_name=db_name,
            host_path=None,
        )]

    def _detect_redis(self, container) -> list[DiscoveredDatabase]:
        """Detect Redis databases."""
        return [DiscoveredDatabase(
            container_name=container.name,
            db_type="redis",
            db_name="redis",
            host_path=None,
        )]

    def _detect_sqlite_from_mounts(self, container, mounts: list[dict]) -> list[DiscoveredDatabase]:
        """Detect SQLite databases from bind mounts."""
        dbs = []
        for mount in mounts:
            if mount["type"] != "bind":
                continue
            source = mount["source"]
            if not self._should_scan_sqlite_mount(source):
                continue
            sqlite_files = self._scan_sqlite_files(source)
            for fpath in sqlite_files:
                db_name = os.path.basename(fpath)
                dbs.append(DiscoveredDatabase(
                    container_name=container.name,
                    db_type="sqlite",
                    db_name=db_name,
                    host_path=fpath,
                ))
        return dbs

    def _detect_from_profile(self, container, profile: dict, mounts: list[dict], env: dict, all_containers: list) -> list[DiscoveredDatabase]:
        """Detect databases based on profile definition."""
        dbs = []
        for db_def in profile.get("databases", []):
            db_type = db_def.get("type", "")

            if db_type == "sqlite":
                container_path = db_def.get("container_path") or db_def.get("path", "")
                # Find the host path by matching container mount destination
                host_path = None
                for mount in mounts:
                    dest = mount["destination"]
                    if container_path.startswith(dest):
                        relative = container_path[len(dest):].lstrip("/")
                        host_path = os.path.join(mount["source"], relative)
                        break
                if host_path and os.path.exists(host_path):
                    dbs.append(DiscoveredDatabase(
                        container_name=container.name,
                        db_type="sqlite",
                        db_name=os.path.basename(container_path),
                        host_path=host_path,
                    ))
                else:
                    logger.warning("Profile %s: SQLite path %s not found on host", profile["name"], container_path)

            elif db_type == "postgres":
                # Check for companion container
                companion_pattern = db_def.get("companion_container_pattern")
                target_container = container
                target_env = env
                target_resolved = False

                if companion_pattern:
                    for c in all_containers:
                        if re.search(companion_pattern, c.name):
                            target_container = c
                            target_env = self._get_env_vars(c)
                            target_resolved = True
                            break
                else:
                    env_vars = db_def.get("env_vars", {})
                    db_host_keys = env_vars.get("db_host", ["DB_HOSTNAME", "DB_HOST", "POSTGRES_HOST"])
                    db_host = next((env.get(key, "").strip() for key in db_host_keys if env.get(key, "").strip()), "")
                    if db_host:
                        companion = self._find_companion_container(
                            container,
                            all_containers,
                            hint=db_host,
                            expected_db_type="postgres",
                        )
                        if companion is not None:
                            target_container = companion
                            target_env = self._get_env_vars(companion)
                            target_resolved = True

                    if not target_resolved:
                        for service_name in self._compose_depends_on_services(container):
                            companion = self._find_companion_container(
                                container,
                                all_containers,
                                hint=service_name,
                                expected_db_type="postgres",
                            )
                            if companion is not None:
                                target_container = companion
                                target_env = self._get_env_vars(companion)
                                target_resolved = True
                                break

                if not target_resolved and self._detect_image_type(self._get_image_name(container)) != "postgres":
                    logger.info(
                        "Profile %s: skipping postgres discovery for %s; no companion postgres container found",
                        profile["name"],
                        container.name,
                    )
                    continue

                env_vars = db_def.get("env_vars", {})
                db_name_keys = env_vars.get("db_name", ["POSTGRES_DB", "DB_DATABASE_NAME", "DB_NAME"])

                db_name = None
                for key in db_name_keys:
                    if key in target_env:
                        db_name = target_env[key]
                        break
                if not db_name:
                    db_name = db_def.get("default_db", "postgres")

                dbs.extend(self._postgres_databases(target_container, target_env, db_name))

            elif db_type in ("mysql", "mariadb"):
                env_vars = db_def.get("env_vars", {})
                db_name_keys = env_vars.get("db_name", ["MYSQL_DATABASE", "MARIADB_DATABASE"])
                db_name = None
                for key in db_name_keys:
                    if key in env:
                        db_name = env[key]
                        break
                if not db_name:
                    db_name = db_def.get("default_db", "mysql")
                dbs.append(DiscoveredDatabase(
                    container_name=container.name,
                    db_type="mariadb",
                    db_name=db_name,
                    host_path=None,
                ))

            elif db_type == "mongodb":
                env_vars = db_def.get("env_vars", {})
                db_name_keys = env_vars.get("db_name", ["MONGO_INITDB_DATABASE"])
                db_name = None
                for key in db_name_keys:
                    if key in env:
                        db_name = env[key]
                        break
                if not db_name:
                    db_name = db_def.get("default_db", "admin")
                dbs.append(DiscoveredDatabase(
                    container_name=container.name,
                    db_type="mongodb",
                    db_name=db_name,
                    host_path=None,
                ))

            elif db_type == "redis":
                dbs.append(DiscoveredDatabase(
                    container_name=container.name,
                    db_type="redis",
                    db_name="redis",
                    host_path=None,
                ))

        return dbs

    def _detect_image_type(self, image: str) -> str | None:
        """Detect database type from image name."""
        for pattern in POSTGRES_PATTERNS:
            if pattern.search(image):
                return "postgres"
        for pattern in MYSQL_PATTERNS:
            if pattern.search(image):
                return "mysql"
        for pattern in MONGO_PATTERNS:
            if pattern.search(image):
                return "mongodb"
        for pattern in REDIS_PATTERNS:
            if pattern.search(image):
                return "redis"
        return None

    async def scan(self) -> list[DiscoveredContainer]:
        """Scan all Docker containers and detect databases.

        docker-py calls and the SQLite filesystem walk are blocking, so the
        scan runs in a worker thread to keep the API responsive.
        """
        return await asyncio.to_thread(self._scan_blocking)

    def _scan_blocking(self) -> list[DiscoveredContainer]:
        start = time.monotonic()
        self._pg_database_cache = {}
        containers = self.docker.containers.list(all=True)
        all_containers = containers
        discovered = []

        for container in containers:
            try:
                if self._is_current_container(container):
                    continue
                image = self._get_image_name(container)
                env = self._get_env_vars(container)
                mounts = self._get_mounts(container)
                profile = self._match_profile(image)
                priority = "medium"
                profile_name = None

                databases: list[DiscoveredDatabase] = []

                if profile:
                    profile_name = profile["name"]
                    priority = profile.get("priority", "medium")
                    databases = self._detect_from_profile(container, profile, mounts, env, all_containers)
                else:
                    # Auto-detection
                    db_type = self._detect_image_type(image)
                    if db_type == "postgres":
                        databases = self._detect_postgres(container, env, mounts)
                    elif db_type in ("mysql", "mariadb"):
                        databases = self._detect_mysql(container, env)
                    elif db_type == "mongodb":
                        databases = self._detect_mongo(container, env)
                    elif db_type == "redis":
                        databases = self._detect_redis(container)

                    # Also scan for SQLite files in bind mounts
                    if container.status == "running":
                        sqlite_dbs = self._detect_sqlite_from_mounts(container, mounts)
                        databases.extend(sqlite_dbs)

                compose_project = container.labels.get("com.docker.compose.project")
                ports = []
                port_bindings = container.attrs.get("NetworkSettings", {}).get("Ports", {})
                if port_bindings:
                    for port, bindings in port_bindings.items():
                        if bindings:
                            for b in bindings:
                                ports.append(f"{b.get('HostPort', '')}:{port}")

                discovered.append(DiscoveredContainer(
                    name=container.name,
                    image=image,
                    status=container.status,
                    databases=databases,
                    profile=profile_name,
                    priority=priority,
                    ports=ports,
                    mounts=mounts,
                    compose_project=compose_project,
                ))

            except Exception as e:
                logger.error("Error scanning container %s: %s", container.name, e)

        duration = time.monotonic() - start
        logger.info("Discovery scan completed: %d containers, %d databases in %.2fs",
                     len(discovered),
                     sum(len(c.databases) for c in discovered),
                     duration)
        return discovered
