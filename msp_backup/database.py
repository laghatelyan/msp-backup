from pathlib import Path
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import quote

from .common import BackupError, DB_CONFIG_FILE, DB_DUMP_DIR, LEGACY_DB_DUMP_DIR, DB_PASSWORD_CRED, fail, info, logger, ui
from .configuration import read_json, save_json
from .credentials import decrypt_credential, remove_runtime, runtime_dir
from .system import secure_runfs
from .database_config import DatabaseConfigurationWorkflow

class DatabaseBackupManager:
    """Coordinate database dump preparation, engine exports, and cleanup."""

    def __init__(self, configuration=None):
        self.configuration = configuration or DatabaseConfigurationWorkflow()

    def configure_database(self):
        return self.configuration.configure_database()

    def escape_mysql(self, value):
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "")

    def escape_pgpass(self, value):
        return value.replace("\\", "\\\\").replace(":", "\\:").replace("\n", "")

    def database_dump(self):
        config = read_json(DB_CONFIG_FILE)
        if not config:
            self.cleanup_database_dump()
            return
        secure_runfs()
        DB_DUMP_DIR.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        DB_DUMP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chown(DB_DUMP_DIR.parent, 0, 0)
        os.chmod(DB_DUMP_DIR.parent, 0o700)
        os.chown(DB_DUMP_DIR, 0, 0)
        os.chmod(DB_DUMP_DIR, 0o700)
        self.cleanup_database_dump()
        engine = config["engine"]
        try:
            if engine == "sqlite":
                inventory = self.dump_sqlite_databases(Path(config["path"]))
            else:
                work = runtime_dir()
                environment = os.environ.copy()
                try:
                    password = work / "db-password"
                    decrypt_credential("msp-backup-database", DB_PASSWORD_CRED, password)
                    secret = password.read_text().rstrip("\n")
                    if engine == "mysql":
                        credentials = work / "mysql.cnf"
                        credentials.write_text("[client]\n" + f'user="{escape_mysql(config["user"])}"\n'
                                               + f'password="{escape_mysql(secret)}"\n'
                                               + f'host="{escape_mysql(config["host"])}"\nport={config["port"]}\n')
                        os.chmod(credentials, 0o600)
                        inventory = self.dump_mysql_databases(credentials)
                    else:
                        credentials = work / "pgpass"
                        credentials.write_text(f"{escape_pgpass(config['host'])}:{config['port']}:*:{escape_pgpass(config['user'])}:{escape_pgpass(secret)}\n")
                        os.chmod(credentials, 0o600)
                        environment["PGPASSFILE"] = str(credentials)
                        inventory = self.dump_postgresql_databases(config, environment)
                finally:
                    remove_runtime(work)
            inventory["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            save_json(DB_DUMP_DIR / "database-manifest.json", inventory)
        except Exception:
            self.cleanup_database_dump()
            raise
        info(f"{engine}: {len(inventory['databases'])} database dump(s) are ready separately in the protected temporary directory {DB_DUMP_DIR}.")

    @staticmethod
    def discover_sqlite_files(source):
        if source.is_file():
            return [(source.name, source)]
        found = []
        for path in source.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                with path.open("rb") as handle:
                    if handle.read(16) == b"SQLite format 3\x00":
                        found.append((path.relative_to(source).as_posix(), path))
            except OSError as exc:
                logger.warning("Skipping unreadable SQLite candidate %s: %s", path, exc)
        return found

    def dump_sqlite_databases(self, source):
        databases = self.discover_sqlite_files(source)
        if not databases:
            fail(f"No SQLite database files were found under {source}.")
        estimated = sum(path.stat().st_size for _, path in databases)
        self.ensure_dump_capacity(estimated, factor=1.1, reserve=4 * 1024 * 1024, label="SQLite")
        for relative_name, database in databases:
            destination = DB_DUMP_DIR / f"sqlite-{quote(relative_name, safe='-_.')}.db"
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            with sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True) as source_db:
                with sqlite3.connect(temporary) as backup_db:
                    source_db.backup(backup_db)
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
        return {"engine": "sqlite", "databases": [name for name, _ in databases],
                "format": "one online backup per SQLite file"}

    @staticmethod
    def run_query(command, environment=None):
        result = subprocess.run(command, capture_output=True, text=True, env=environment)
        if result.returncode:
            fail(f"Database discovery failed: {result.stderr.strip() or f'exit code {result.returncode}'}")
        return result.stdout

    @staticmethod
    def dump_to_file(command, destination, environment=None, label="database dump"):
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        with temporary.open("wb") as output:
            logger.info("Starting %s", label)
            result = subprocess.run(command, stdout=output, stderr=subprocess.PIPE, text=True, env=environment)
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
            logger.warning(result.stderr.rstrip())
        if result.returncode:
            temporary.unlink(missing_ok=True)
            fail(f"{label} failed: {result.stderr.strip() or f'exit code {result.returncode}'}")
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)

    @staticmethod
    def ensure_dump_capacity(estimated_bytes, *, factor, reserve, label):
        free = shutil.disk_usage(DB_DUMP_DIR).free
        required = int(estimated_bytes * factor) + reserve
        if free < required:
            fail(f"Not enough disk space for {label} database dumps in {DB_DUMP_DIR}: {free} bytes free, approximately {required} bytes recommended. Free space on this filesystem or reduce the database set before retrying.")
        ui.status(f"Disk space check passed for {label} dumps ({free:,} bytes free; about {required:,} bytes recommended).", "success")

    def dump_mysql_databases(self, credentials):
        client = shutil.which("mariadb") or shutil.which("mysql")
        dumper = shutil.which("mariadb-dump") or shutil.which("mysqldump")
        if not client or not dumper:
            fail("MySQL/MariaDB client and dump utilities are required.")
        base = [client, f"--defaults-extra-file={credentials}", "--batch", "--skip-column-names"]
        query = "SELECT s.SCHEMA_NAME, COALESCE(SUM(t.DATA_LENGTH + t.INDEX_LENGTH), 0) FROM INFORMATION_SCHEMA.SCHEMATA s LEFT JOIN INFORMATION_SCHEMA.TABLES t ON s.SCHEMA_NAME=t.TABLE_SCHEMA WHERE s.SCHEMA_NAME NOT IN ('information_schema','performance_schema','mysql','sys') GROUP BY s.SCHEMA_NAME ORDER BY s.SCHEMA_NAME"
        rows = self.run_query([*base, f"--execute={query}"]).splitlines()
        entries = []
        for row in rows:
            name, _, size = row.partition("\t")
            if name:
                entries.append((name, int(size or 0)))
        databases = [name for name, _ in entries]
        self.ensure_dump_capacity(sum(size for _, size in entries), factor=2.0, reserve=32 * 1024 * 1024,
                                  label="MySQL/MariaDB")
        for name in databases:
            destination = DB_DUMP_DIR / f"mysql-{quote(name, safe='-_.')}.sql"
            command = [dumper, f"--defaults-extra-file={credentials}", "--single-transaction", "--routines", "--events", "--triggers", "--hex-blob", "--databases", name]
            self.dump_to_file(command, destination, label=f"MySQL/MariaDB database {name}")
        # These are created and managed by the database server. In particular,
        # MariaDB's mariadb.sys account is the definer of the mysql.user view
        # on current releases and is not a restorable application account.
        system_accounts = ("mariadb.sys", "mysql.sys", "mysql.session", "mysql.infoschema")
        excluded = ", ".join("'" + name.replace("'", "''") + "'" for name in system_accounts)
        accounts_query = ("SELECT CONCAT(QUOTE(User), '@', QUOTE(Host)) FROM mysql.user "
                          f"WHERE User NOT IN ({excluded}) ORDER BY User, Host")
        accounts = [account for account in self.run_query([*base, f"--execute={accounts_query}"]).splitlines() if account]
        account_sql = ["-- MySQL/MariaDB accounts and grants; restore separately on a compatible server."]
        for account in accounts:
            create_query = f"SHOW CREATE USER {account}"
            create_rows = self.run_query([*base, f"--execute={create_query}"]).splitlines()
            if not create_rows or "\t" not in create_rows[0]:
                fail(f"Could not export account definition for {account}.")
            account_sql.append(create_rows[0].split("\t", 1)[1].rstrip(";") + ";")
            grants_query = f"SHOW GRANTS FOR {account}"
            grants = self.run_query([*base, f"--execute={grants_query}"]).splitlines()
            account_sql.extend(grant.rstrip(";") + ";" for grant in grants if grant)
        accounts_file = DB_DUMP_DIR / "mysql-users.sql"
        accounts_file.write_text("\n".join(account_sql) + "\n", encoding="utf-8")
        os.chmod(accounts_file, 0o600)
        return {"engine": "mysql", "databases": databases, "globals": accounts_file.name,
                "accounts": len(accounts), "system_accounts_excluded": list(system_accounts),
                "format": "one SQL file per database"}

    def dump_postgresql_databases(self, config, environment):
        psql, dumper, globals_dumper = shutil.which("psql"), shutil.which("pg_dump"), shutil.which("pg_dumpall")
        if not all((psql, dumper, globals_dumper)):
            fail("PostgreSQL requires psql, pg_dump, and pg_dumpall.")
        connection = [f"--host={config['host']}", f"--port={config['port']}", f"--username={config['user']}"]
        query = "SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname"
        names = self.run_query([psql, "--no-psqlrc", "--tuples-only", "--no-align", *connection,
                                "--dbname=postgres", f"--command={query}"], environment).splitlines()
        databases = [name.strip() for name in names if name.strip()]
        size_query = "SELECT pg_database_size(datname) FROM pg_database WHERE datistemplate = false ORDER BY datname"
        sizes = self.run_query([psql, "--no-psqlrc", "--tuples-only", "--no-align", *connection,
                                "--dbname=postgres", f"--command={size_query}"], environment).splitlines()
        estimated = sum(int(size.strip()) for size in sizes if size.strip().isdecimal())
        self.ensure_dump_capacity(estimated, factor=1.1, reserve=32 * 1024 * 1024, label="PostgreSQL")
        globals_file = DB_DUMP_DIR / "postgresql-globals.sql"
        self.dump_to_file([globals_dumper, "--globals-only", "--no-password", *connection], globals_file,
                          environment, "PostgreSQL global roles")
        for name in databases:
            destination = DB_DUMP_DIR / f"postgresql-{quote(name, safe='-_.')}.dump"
            command = [dumper, "--format=custom", "--no-password", *connection, f"--dbname={name}"]
            self.dump_to_file(command, destination, environment, f"PostgreSQL database {name}")
        return {"engine": "postgresql", "databases": databases, "globals": globals_file.name,
                "format": "one custom archive per database"}

    def cleanup_database_dump(self):
        for directory in (DB_DUMP_DIR, LEGACY_DB_DUMP_DIR):
            if not directory.is_dir() or directory.is_symlink():
                continue
            for path in directory.iterdir():
                if path.is_file() and (path.name.startswith(("mysql-", "postgresql-", "sqlite-")) or path.name == "database-manifest.json"):
                    path.unlink(missing_ok=True)


database_backup_manager = DatabaseBackupManager()
configure_database = database_backup_manager.configure_database
escape_mysql = database_backup_manager.escape_mysql
escape_pgpass = database_backup_manager.escape_pgpass
database_dump = database_backup_manager.database_dump
cleanup_database_dump = database_backup_manager.cleanup_database_dump
