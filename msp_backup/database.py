from pathlib import Path
import getpass
import os
import shutil
import subprocess
import sys

from .common import DB_CONFIG_FILE, DB_DUMP_DIR, DB_PASSWORD_CRED, ETC, LEGACY_MYSQL_CRED, LEGACY_PGPASS_CRED, PATHS_FILE, decrypt_credential, encrypt_credential, fail, get_family, info, input_default, logger, read_json, remove_runtime, run_stream, runtime_dir, save_json, secure_runfs, yes_no

class DatabaseBackupManager:
    """Own database configuration and operational workflows."""

    def configure_database(self):
        ETC.mkdir(mode=0o700, parents=True, exist_ok=True)
        print("Database type: 1) MySQL/MariaDB  2) PostgreSQL  3) SQLite  0) Cancel")
        choice = input("Choice: ").strip()
        engines = {"1": "mysql", "2": "postgresql", "3": "sqlite"}
        if choice == "0":
            return False
        if choice not in engines:
            fail("Unknown database engine.")
        engine = engines[choice]
        client = {"mysql": ("mariadb-dump", "mysqldump"), "postgresql": ("pg_dumpall",), "sqlite": ("sqlite3",)}[engine]
        if not any(shutil.which(cmd) for cmd in client):
            family = get_family()
            package = {"mysql": "default-mysql-client" if family == "debian" else "mariadb", "postgresql": "postgresql-client" if family == "debian" else "postgresql", "sqlite": "sqlite3"}[engine]
            if not yes_no(f"Package {package} is required. Install it?"):
                fail("The database client utility is not installed.")
            command = ["apt-get", "update"] if family == "debian" else None
            if command:
                run_stream(command)
                run_stream(["apt-get", "install", "-y", package])
            else:
                manager = "dnf" if shutil.which("dnf") else "yum"
                run_stream([manager, "install", "-y", package])
        secure_runfs()
        Path("/run/msp-backup").mkdir(mode=0o700, exist_ok=True)
        DB_DUMP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(Path("/run/msp-backup"), 0o700)
        os.chmod(DB_DUMP_DIR, 0o700)
        data = {"engine": engine}
        if engine in ("mysql", "postgresql"):
            default_port = "3306" if engine == "mysql" else "5432"
            default_host = "localhost" if engine == "mysql" else "127.0.0.1"
            data["host"] = input_default("Database host", default_host)
            port = input_default("Database port", default_port)
            if not port.isdecimal() or not 1 <= int(port) <= 65535:
                fail("Invalid database port.")
            data["port"] = int(port)
            data["user"] = input("Database user: ").strip()
            if not data["user"]:
                fail("Database user is required.")
            secret = getpass.getpass("Database password: ")
            encrypt_credential("msp-backup-database", secret.encode(), DB_PASSWORD_CRED)
        else:
            path = Path(input("Absolute path to the SQLite file: ").strip())
            if not path.is_absolute() or not path.is_file():
                fail("Enter the absolute path to an existing SQLite file.")
            data["path"] = str(path)
            DB_PASSWORD_CRED.unlink(missing_ok=True)
        save_json(DB_CONFIG_FILE, data)
        paths = PATHS_FILE.read_text().splitlines() if PATHS_FILE.exists() else []
        if str(DB_DUMP_DIR) not in paths:
            paths.append(str(DB_DUMP_DIR))
        PATHS_FILE.write_text("\n".join(paths) + "\n")
        os.chmod(PATHS_FILE, 0o600)
        LEGACY_MYSQL_CRED.unlink(missing_ok=True)
        LEGACY_PGPASS_CRED.unlink(missing_ok=True)
        info(f"{engine} backups configured; the dump will be included in the Restic snapshot.")
        return True

    def escape_mysql(self, value):
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "")

    def escape_pgpass(self, value):
        return value.replace("\\", "\\\\").replace(":", "\\:").replace("\n", "")

    def database_dump(self):
        config = read_json(DB_CONFIG_FILE)
        if not config:
            return
        secure_runfs()
        Path("/run/msp-backup").mkdir(mode=0o700, exist_ok=True)
        DB_DUMP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(Path("/run/msp-backup"), 0o700)
        os.chmod(DB_DUMP_DIR, 0o700)
        engine = config["engine"]
        if engine == "sqlite":
            command = ["sqlite3", config["path"], f".backup '{DB_DUMP_DIR / 'sqlite-database.db.tmp'}'"]
            run_stream(command, label="SQLite backup")
            os.chmod(DB_DUMP_DIR / "sqlite-database.db.tmp", 0o600)
            os.replace(DB_DUMP_DIR / "sqlite-database.db.tmp", DB_DUMP_DIR / "sqlite-database.db")
        else:
            work = runtime_dir()
            environment = os.environ.copy()
            try:
                password = work / "db-password"
                decrypt_credential("msp-backup-database", DB_PASSWORD_CRED, password)
                secret = password.read_text().rstrip("\n")
                if engine in ("mysql", "mariadb"):
                    cnf = work / "mysql.cnf"
                    cnf.write_text("[client]\n" + f'user="{escape_mysql(config["user"])}"\n'
                                   + f'password="{escape_mysql(secret)}"\n'
                                   + f'host="{escape_mysql(config["host"])}"\nport={config["port"]}\n')
                    os.chmod(cnf, 0o600)
                    dump = DB_DUMP_DIR / "mysql-all.sql"
                    tmp = DB_DUMP_DIR / "mysql-all.sql.tmp"
                    binary = shutil.which("mariadb-dump") or shutil.which("mysqldump")
                    if not binary:
                        fail("Neither mariadb-dump nor mysqldump was found.")
                    command = [binary, f"--defaults-extra-file={cnf}", "--single-transaction", "--routines", "--events", "--triggers", "--hex-blob", "--all-databases"]
                else:
                    pgpass = work / "pgpass"
                    pgpass.write_text(f"{escape_pgpass(config['host'])}:{config['port']}:*:{escape_pgpass(config['user'])}:{escape_pgpass(secret)}\n")
                    os.chmod(pgpass, 0o600)
                    dump = DB_DUMP_DIR / "postgresql-all.sql"
                    tmp = DB_DUMP_DIR / "postgresql-all.sql.tmp"
                    binary = shutil.which("pg_dumpall")
                    if not binary:
                        fail("pg_dumpall was not found.")
                    command = [binary, "--no-password", f"--host={config['host']}", f"--port={config['port']}", f"--username={config['user']}"]
                    environment = os.environ.copy()
                    environment["PGPASSFILE"] = str(pgpass)
                with tmp.open("wb") as output:
                    logger.info("Starting %s database dump", engine)
                    result = subprocess.run(command, stdout=output, stderr=subprocess.PIPE, text=True, env=environment)
                if result.stderr:
                    print(result.stderr, end="", file=sys.stderr)
                    logger.warning(result.stderr.rstrip())
                if result.returncode:
                    tmp.unlink(missing_ok=True)
                    fail(f"{engine} dump failed.")
                os.chmod(tmp, 0o600)
                os.replace(tmp, dump)
            finally:
                remove_runtime(work)
        info(f"{engine} dump is ready in the temporary memory-backed directory.")

    def cleanup_database_dump(self):
        for filename in ("mysql-all.sql", "postgresql-all.sql", "sqlite-database.db", "mysql-all.sql.tmp", "postgresql-all.sql.tmp", "sqlite-database.db.tmp"):
            (DB_DUMP_DIR / filename).unlink(missing_ok=True)


database_backup_manager = DatabaseBackupManager()
configure_database = database_backup_manager.configure_database
escape_mysql = database_backup_manager.escape_mysql
escape_pgpass = database_backup_manager.escape_pgpass
database_dump = database_backup_manager.database_dump
cleanup_database_dump = database_backup_manager.cleanup_database_dump
