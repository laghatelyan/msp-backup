from pathlib import Path
from datetime import datetime
import posixpath
from urllib.parse import urlsplit
import json
import getpass
import os
import re
import secrets
import shutil
import subprocess
import sys
import time


from .common import APP_DIR, APP_SYSTEMD_DIR, BACKUP_TAG, CONFIG_FILE, DB_CONFIG_FILE, DB_DUMP_DIR, ETC, INSTALL_PATH, LEGACY_MYSQL_CRED, LEGACY_PGPASS_CRED, LEGACY_RESTIC_BIN, LOG_DIR, LOGROTATE, LOG_FILE, OLD_ETC, PACKAGE_DIR, PATHS_FILE, RESTIC_BIN, RESTIC_INSTALL_BIN, RESTIC_PASSWORD_CRED, S3_CREDENTIALS_CRED, SMTP_CONFIG_FILE, SMTP_PASSWORD_CRED, SYSTEMD_DIR, UPDATE_CONFIG_FILE, VERSION, config_store, credential_store, encrypt_credential, fail, info, input_positive_int, link_systemd_unit, load_config, read_json, remove_old_secret, run_stream, save_json, system_manager, ui, yes_no
from .common import BackupError, logger
from .restic import ensure_restic, restic, restic_installer, restic_manager, update_restic
from .database import configure_database, database_backup_manager
from .notifications import configure_log_rotation, configure_notifications, notification_manager
from .updates import check_script_update, configure_update_url, install_script_update_timer, update_manager

class BackupWorkflowManager:
    """Own setup, backup, restore, mount, and interactive workflows."""

    def ask_until_valid(self, prompt, validator, *, default=None, secret=False):
        while True:
            suffix = f" [{default}]" if default is not None else ""
            value = getpass.getpass(f"{prompt}{suffix}: ") if secret else input(f"{prompt}{suffix}: ")
            if not value and default is not None:
                value = str(default)
            try:
                return validator(value)
            except (BackupError, ValueError, OSError) as exc:
                ui.status(f"{exc} Please try again.", "error")

    @staticmethod
    def choice_validator(choices, message="Choose one of the listed options."):
        def validate(value):
            if value not in choices:
                raise ValueError(message)
            return value
        return validate

    @staticmethod
    def validate_region(value):
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9-]+", value):
            raise ValueError("Enter a valid region.")
        return value

    @staticmethod
    def validate_endpoint(value):
        endpoint = value.strip().rstrip("/")
        parsed = urlsplit(endpoint)
        if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Enter an S3 endpoint URL such as https://s3.example.com.")
        return endpoint, parsed.scheme

    @staticmethod
    def validate_bucket(value):
        value = value.strip()
        if not value or any(char.isspace() for char in value) or "/" in value:
            raise ValueError("Enter a bucket name without spaces or slashes.")
        return value

    def migrate_legacy_secrets(self):
        ETC.mkdir(mode=0o700, parents=True, exist_ok=True)
        old_roots = [ETC, OLD_ETC, Path("/etc/restic-wizard")]
        simple = [("repository-password", RESTIC_PASSWORD_CRED, "msp-backup-restic"),
                  ("smtp-password", SMTP_PASSWORD_CRED, "msp-backup-smtp"),
                  ("mysql.cnf", LEGACY_MYSQL_CRED, "msp-backup-database-mysql-config"),
                  ("pgpass", LEGACY_PGPASS_CRED, "msp-backup-database-pgpass")]
        for root in old_roots:
            for filename, target, name in simple:
                source = root / filename
                if not source.is_file():
                    continue
                if not target.exists():
                    info(f"Migrating legacy secret {source.name} to encrypted storage.")
                    encrypt_credential(name, source.read_bytes(), target)
                remove_old_secret(source)
        old_dump = Path("/var/lib/msp-backup/db-dumps")
        for filename in ("mysql-all.sql", "postgresql-all.sql", "sqlite-database.db", "mysql-all.sql.tmp", "postgresql-all.sql.tmp", "sqlite-database.db.tmp"):
            remove_old_secret(old_dump / filename)

    def import_legacy_config(self):
        migrated = False
        if OLD_ETC.is_dir():
            ETC.mkdir(mode=0o700, parents=True, exist_ok=True)
            movable = (
                "config.json", "database.json", "notifications.json", "update.json", "paths.txt",
                "restic-password.cred", "database-password.cred", "smtp-password.cred",
                "database-mysql-config.cred", "database-pgpass.cred", "credential.key",
                "mount.path", "mount.pid", "mount.log",
            )
            for name in movable:
                source = OLD_ETC / name
                destination = ETC / name
                if not source.is_file() or destination.exists():
                    continue
                if name == "paths.txt":
                    content = source.read_text().replace("/var/lib/msp-backup/db-dumps", str(DB_DUMP_DIR))
                    destination.write_text(content)
                    source.unlink()
                else:
                    shutil.move(str(source), str(destination))
                os.chown(destination, 0, 0)
                os.chmod(destination, 0o600)
                migrated = True
        old_log_dir = Path("/var/log/msp-backup")
        log_migration_marker = ETC / ".legacy-logs-imported"
        if old_log_dir.is_dir() and not log_migration_marker.exists():
            LOG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            for source in old_log_dir.glob("msp-backup.log*"):
                destination = LOG_DIR / source.name
                if not source.is_file():
                    continue
                if source.name == LOG_FILE.name and destination.exists():
                    with source.open("rb") as old_log, destination.open("ab") as new_log:
                        shutil.copyfileobj(old_log, new_log)
                    source.unlink()
                    migrated = True
                elif not destination.exists():
                    shutil.move(str(source), str(destination))
                    migrated = True
                if destination.exists():
                    os.chown(destination, 0, 0)
                    os.chmod(destination, 0o600)
            log_migration_marker.touch(mode=0o600, exist_ok=True)
            os.chown(log_migration_marker, 0, 0)

        legacy_roots = [ETC, OLD_ETC, Path("/etc/restic-wizard")]
        legacy = next((root for root in legacy_roots if (root / "config").is_file()), None)
        if CONFIG_FILE.exists() or legacy is None:
            return migrated
        ETC.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Legacy shell config is parsed as data; it is never sourced or executed.
        values = {}
        old_config = legacy / "config"
        if old_config.exists():
            import shlex
            for line in old_config.read_text().splitlines():
                if "=" in line:
                    key, raw = line.split("=", 1)
                    try:
                        values[key] = shlex.split(raw)[0]
                    except (ValueError, IndexError):
                        pass
        config = {}
        if values.get("RESTIC_REPOSITORY"):
            config["repository"] = values["RESTIC_REPOSITORY"]
        for key in ("KEEP_DAILY", "KEEP_WEEKLY", "KEEP_MONTHLY"):
            try:
                config[key.lower()] = int(values.get(key, "0"))
            except ValueError:
                config[key.lower()] = {"KEEP_DAILY": 7, "KEEP_WEEKLY": 4, "KEEP_MONTHLY": 12}[key]
        if config.get("repository"):
            save_json(CONFIG_FILE, config)
        paths = legacy / "paths.txt"
        if paths.exists():
            items = [line.replace("/var/lib/msp-backup/db-dumps", str(DB_DUMP_DIR)) for line in paths.read_text().splitlines() if line]
            PATHS_FILE.write_text("\n".join(items) + "\n")
            os.chmod(PATHS_FILE, 0o600)
            paths.unlink()
        db_legacy = legacy / "database.conf"
        if db_legacy.exists():
            old_db = {}
            for line in db_legacy.read_text().splitlines():
                if "=" in line:
                    key, raw = line.split("=", 1)
                    try:
                        old_db[key] = shlex.split(raw)[0]
                    except (ValueError, IndexError):
                        pass
            engine = old_db.get("DB_ENGINE", "")
            if engine:
                migrated_db = {"engine": engine, "host": old_db.get("DB_HOST", "localhost"),
                               "port": int(old_db.get("DB_PORT", "3306" if engine == "mysql" else "5432")),
                               "user": old_db.get("DB_USER", "")}
                if old_db.get("DB_PATH"):
                    migrated_db["path"] = old_db["DB_PATH"]
                save_json(DB_CONFIG_FILE, migrated_db)
            db_legacy.unlink()
        notify_legacy = legacy / "notifications.conf"
        if notify_legacy.exists():
            old_notify = {}
            for line in notify_legacy.read_text().splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    old_notify[key] = value
            if old_notify.get("NOTIFY_ENABLED") == "1":
                save_json(SMTP_CONFIG_FILE, {"enabled": True, "host": old_notify.get("SMTP_HOST", ""),
                          "port": int(old_notify.get("SMTP_PORT", "587")), "tls": old_notify.get("SMTP_TLS", "starttls"),
                          "auth": old_notify.get("SMTP_AUTH") == "1", "user": old_notify.get("SMTP_USER", ""),
                          "from": old_notify.get("SMTP_FROM", ""), "to": old_notify.get("SMTP_TO", ""),
                          "mode": old_notify.get("NOTIFY_MODE", "failures")})
            notify_legacy.unlink()
        if old_config.exists():
            old_config.unlink()
        info("Legacy MSP Backup configuration imported; review the settings before running.")
        subprocess.run(["systemctl", "disable", "--now", "restic-backup.timer", "restic-update.timer"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True

    def write_units(self, config):
        if not config or not config.get("repository") or not PATHS_FILE.is_file():
            fail("Configure the repository and backup paths first (menu item 1).")
        service = f"""[Unit]
    Description=MSP Backup (Restic)
    Wants=network-online.target
    After=network-online.target
    OnFailure=msp-backup-notify-failure.service

    [Service]
    Type=oneshot
    ExecStartPre={INSTALL_PATH} --db-dump
    ExecStart={INSTALL_PATH} --run-backup
    ExecStartPost={INSTALL_PATH} --db-clean
    ExecStartPost={INSTALL_PATH} --prune
    ExecStartPost={INSTALL_PATH} --notify-success
    Nice=10
    IOSchedulingClass=best-effort
    IOSchedulingPriority=7
    """
        timer = """[Unit]
    Description=Run MSP Backup daily

    [Timer]
    OnCalendar=*-*-* 02:30:00
    RandomizedDelaySec=30m
    Persistent=true

    [Install]
    WantedBy=timers.target
    """
        failure = f"""[Unit]
    Description=Send MSP Backup failure notification

    [Service]
    Type=oneshot
    ExecStart={INSTALL_PATH} --notify-failure
    """
        APP_SYSTEMD_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
        units = {
            "msp-backup.service": service,
            "msp-backup.timer": timer,
            "msp-backup-notify-failure.service": failure,
        }
        for name, content in units.items():
            path = APP_SYSTEMD_DIR / name
            path.write_text(content)
            os.chmod(path, 0o644)
            os.chown(path, 0, 0)
            link_systemd_unit(path)
        run_stream(["systemctl", "daemon-reload"])
        run_stream(["systemctl", "enable", "--now", "msp-backup.timer"])

    def collect_repository_settings(self):
        current = read_json(CONFIG_FILE, {})
        current_repo = current.get("repository", "")
        current_s3 = current.get("backend") == "s3" or current_repo.startswith("s3:")
        saved_endpoint = current.get("s3_endpoint", "")
        if current_s3:
            default_type = "2" if current_repo.startswith("s3:s3.") and not saved_endpoint.startswith(("http://", "https://")) else "3"
        else:
            default_type = "1"
        choice = ui.select("REPOSITORY BACKEND", [
            ("1", "Local path or another Restic backend", "SFTP, REST, etc."),
            ("2", "Amazon S3"),
            ("3", "S3-compatible storage", "Wasabi, Backblaze, MinIO, etc."),
            ("0", "Cancel"),
        ], "Use ↑/↓ or left/right, then Enter.", initial=int(default_type) - 1)
        if choice in (None, "0"):
            fail("Repository setup cancelled.")
        if choice == "1":
            def validate_repository(value):
                value = value.strip()
                if not value or "'" in value or "\n" in value or value.startswith("s3:"):
                    raise ValueError("Enter a repository address; select an S3 option for S3 storage.")
                if value.startswith("/"):
                    local = Path(os.path.realpath(value))
                    if local == Path("/"):
                        raise ValueError("The filesystem root / cannot be used as a repository.")
                    local.mkdir(mode=0o700, parents=True, exist_ok=True)
                    os.chown(local, 0, 0)
                    os.chmod(local, 0o700)
                    return str(local), local
                return value, None
            default_repo = current_repo if current_repo and not current_s3 else None
            repository, local_repo = self.ask_until_valid("Restic repository (local path or backend)", validate_repository, default=default_repo)
            return {"repository": repository, "backend": "restic"}, local_repo

        if choice == "2":
            region = self.ask_until_valid("AWS region", self.validate_region, default=current.get("region", "us-east-1"))
            endpoint = f"s3.{region}.amazonaws.com"
        else:
            endpoint, scheme = self.ask_until_valid("S3 endpoint (for example https://s3.example.com)", self.validate_endpoint, default=saved_endpoint or None)
            if scheme == "http":
                ui.status("HTTP does not encrypt S3 traffic. Prefer HTTPS.", "warning")
            region = self.ask_until_valid("S3 region", self.validate_region, default=current.get("region", "us-east-1"))
        bucket = self.ask_until_valid("S3 bucket name", self.validate_bucket, default=current.get("bucket", None) if current_s3 else None)
        prefix = input(f"Repository prefix inside the bucket [{current.get('prefix', '')}]: ").strip().strip("/") or current.get("prefix", "")
        repository = f"s3:{endpoint}/{bucket}" + (f"/{prefix}" if prefix else "")

        existing = {}
        if S3_CREDENTIALS_CRED.is_file():
            work = credential_store.runtime_dir()
            try:
                credential_file = work / "s3-credentials.json"
                credential_store.decrypt_credential("msp-backup-s3", S3_CREDENTIALS_CRED, credential_file)
                existing = json.loads(credential_file.read_text())
            except (OSError, ValueError, BackupError) as exc:
                fail(f"Could not read existing S3 credentials: {exc}")
            finally:
                credential_store.remove_runtime(work)
        while True:
            access_key_id = input("S3 access key ID (blank keeps the current value): ").strip()
            secret_key = getpass.getpass("S3 secret access key (blank keeps the current value): ")
            session_token = getpass.getpass("S3 session token (blank keeps current/none; '-' removes it): ")
            credentials = {
                "access_key_id": access_key_id or existing.get("access_key_id", ""),
                "secret_access_key": secret_key or existing.get("secret_access_key", ""),
                "session_token": "" if session_token == "-" else session_token or existing.get("session_token", ""),
            }
            if credentials["access_key_id"] and credentials["secret_access_key"]:
                break
            ui.status("S3 access key ID and secret access key are required. Enter them again.", "error")
        encrypt_credential("msp-backup-s3", json.dumps(credentials).encode(), S3_CREDENTIALS_CRED)
        return {"repository": repository, "backend": "s3", "region": region, "bucket": bucket,
                "s3_endpoint": endpoint, "prefix": prefix}, None

    def get_repository_password(self):
        existing_repository = CONFIG_FILE.is_file() and RESTIC_PASSWORD_CRED.is_file()
        if existing_repository:
            mode = ui.select("RESTIC REPOSITORY PASSWORD", [
                ("1", "Keep the saved password", "recommended for existing repositories"),
                ("2", "Enter the current repository password"),
                ("0", "Cancel"),
            ], "Changing the password can make existing snapshots inaccessible.")
            if mode in (None, "0"):
                fail("Password setup cancelled.")
            if mode == "1":
                work = credential_store.runtime_dir()
                try:
                    password_file = work / "restic-password"
                    credential_store.decrypt_credential("msp-backup-restic", RESTIC_PASSWORD_CRED, password_file)
                    return password_file.read_text().rstrip("\n")
                finally:
                    credential_store.remove_runtime(work)
            return self.ask_until_valid(
                "Current repository password (at least 12 characters)",
                lambda value: value if len(value) >= 12 else self.raise_value("Use at least 12 characters."),
                secret=True,
            )
        while True:
            mode = ui.select("RESTIC REPOSITORY PASSWORD", [
                ("1", "Generate a secure password", "recommended"),
                ("2", "Enter a password manually"),
                ("0", "Cancel"),
            ], "Use ↑/↓ or left/right, then Enter.")
            if mode in (None, "0"):
                fail("Password setup cancelled.")
            if mode == "1":
                password = secrets.token_urlsafe(32)
                ui.header("GENERATED RESTIC PASSWORD", "Save this password in Bitwarden before continuing")
                print(ui.paint(password, "yellow", "bold"))
                choice = ui.select("NEXT STEP", [
                    ("1", "I saved it — continue"),
                    ("2", "Generate another password"),
                    ("3", "Enter a password manually"),
                    ("0", "Cancel setup"),
                ], "The generated password will not be shown again.")
                if choice in (None, "0"):
                    fail("Password setup cancelled.")
                if choice == "2":
                    continue
                if choice == "3":
                    mode = "2"
                else:
                    return password
            if mode == "2":
                password = self.ask_until_valid(
                    "Repository password (at least 12 characters)",
                    lambda value: value if len(value) >= 12 else self.raise_value("Use at least 12 characters."),
                    secret=True,
                )
                self.ask_until_valid(
                    "Confirm password",
                    lambda value: value if value == password else self.raise_value("Passwords do not match."),
                    secret=True,
                )
                return password

    @staticmethod
    def raise_value(message):
        raise ValueError(message)

    def configure_repository(self):
        config = read_json(CONFIG_FILE, {})
        if not config or not RESTIC_PASSWORD_CRED.is_file() or not RESTIC_BIN.is_file():
            ui.status("Initial setup is not complete. Opening the full setup wizard.", "warning")
            ensure_restic()
            return self.setup_backup()
        settings, _ = self.collect_repository_settings()
        config.update(settings)
        if config.get("backend") != "s3":
            config.pop("region", None)
            config.pop("bucket", None)
        save_json(CONFIG_FILE, config)
        if config.get("backend") != "s3":
            credential_store.remove_old_secret(S3_CREDENTIALS_CRED)
        if restic("snapshots", check=False, quiet=True):
            if yes_no("The repository is not initialized. Initialize it now?"):
                restic("init")
            else:
                fail("The repository is not initialized.")
        info("Repository settings saved and verified.")

    def setup_backup(self):
        ui.header("MSP BACKUP · INITIAL SETUP", "Configure repository, encryption, paths, and retention")
        ETC.mkdir(mode=0o700, parents=True, exist_ok=True)
        repository_settings, local_repo = self.collect_repository_settings()
        password = self.get_repository_password()
        encrypt_credential("msp-backup-restic", password.encode(), RESTIC_PASSWORD_CRED)
        paths = []
        ui.section("Backup paths")
        print("Enter absolute paths one at a time. Submit an empty line when done.")
        while not paths:
            while True:
                value = input("> ").strip()
                if not value:
                    break
                path = Path(value)
                if not path.is_absolute() or not path.exists():
                    ui.status(f"Path must exist and be absolute: {value}. Enter another path.", "error")
                    continue
                paths.append(str(path))
            if not paths:
                ui.status("At least one backup path is required. Enter a path to continue.", "error")
        PATHS_FILE.write_text("\n".join(dict.fromkeys(paths)) + "\n")
        os.chmod(PATHS_FILE, 0o600)
        configure_db = yes_no("Configure MySQL/MariaDB, PostgreSQL, or SQLite backups?")
        if configure_db:
            configure_database()
            paths = PATHS_FILE.read_text().splitlines() if PATHS_FILE.exists() else paths
        elif read_json(DB_CONFIG_FILE):
            if str(DB_DUMP_DIR) not in paths:
                paths.append(str(DB_DUMP_DIR))
        if local_repo:
            for source in paths:
                source_path = Path(os.path.realpath(source))
                if source_path == Path("/") or local_repo == source_path or local_repo.is_relative_to(source_path) or source_path.is_relative_to(local_repo):
                    fail(f"Backup path {source_path} overlaps the repository {local_repo}.")
        keep = {
            "daily": input_positive_int("Daily snapshots to keep", 7),
            "weekly": input_positive_int("Weekly snapshots to keep", 4),
            "monthly": input_positive_int("Monthly snapshots to keep", 12),
        }
        keep.update(repository_settings)
        save_json(CONFIG_FILE, keep)
        if keep.get("backend") != "s3":
            credential_store.remove_old_secret(S3_CREDENTIALS_CRED)
        PATHS_FILE.write_text("\n".join(dict.fromkeys(paths)) + "\n")
        os.chmod(PATHS_FILE, 0o600)
        if restic("snapshots", check=False, quiet=True):
            if yes_no("The repository is not initialized. Initialize it?"):
                restic("init")
            else:
                fail("The repository is not initialized.")
        write_units(keep)
        if not SMTP_CONFIG_FILE.exists():
            save_json(SMTP_CONFIG_FILE, {"enabled": False})
        info("Setup complete; the daily timer is enabled for 02:30.")

    def run_backup(self):
        config = load_config()
        if not PATHS_FILE.exists():
            fail("The backup path list was not found.")
        restic("backup", "--tag", BACKUP_TAG, "--files-from", str(PATHS_FILE))

    def prune_backups(self):
        config = load_config()
        restic("forget", "--tag", BACKUP_TAG, "--keep-daily", str(config["daily"]), "--keep-weekly", str(config["weekly"]), "--keep-monthly", str(config["monthly"]), "--prune")

    def load_snapshots(self):
        try:
            output = restic("snapshots", "--json", quiet=True, capture_output=True)
            snapshots = json.loads(output or "[]")
        except (BackupError, json.JSONDecodeError) as exc:
            fail(f"Could not read the snapshot list: {exc}")
        if snapshots is None:
            return []
        if not isinstance(snapshots, list):
            fail("Restic returned an invalid snapshot list.")
        snapshots = [snapshot for snapshot in snapshots if isinstance(snapshot, dict) and snapshot.get("id")]

        def snapshot_time(snapshot):
            try:
                value = snapshot.get("time", "").replace("Z", "+00:00")
                return datetime.fromisoformat(value).timestamp()
            except (TypeError, ValueError, OverflowError):
                return 0

        return sorted(snapshots, key=snapshot_time, reverse=True)

    @staticmethod
    def snapshot_label(snapshot):
        timestamp = str(snapshot.get("time") or "unknown time").replace("T", " ")[:19]
        identifier = snapshot.get("short_id") or str(snapshot.get("id", "unknown"))[:8]
        hostname = snapshot.get("hostname") or "unknown host"
        return f"{timestamp}  ·  {hostname}  ·  {identifier}"

    def select_snapshot(self):
        snapshots = self.load_snapshots()
        if not snapshots:
            ui.status("No snapshots are available in this repository.", "warning")
            return None
        options = []
        for index, snapshot in enumerate(snapshots, start=1):
            paths = ", ".join(str(path) for path in snapshot.get("paths", []))
            if len(paths) > 88:
                paths = paths[:85] + "…"
            options.append((str(index), self.snapshot_label(snapshot), paths))
        options.append(("0", "Back"))
        choice = ui.select("RESTORE · SELECT SNAPSHOT", options, "Newest snapshots are listed first. Use arrows and Enter.")
        if choice in (None, "0"):
            return None
        try:
            return snapshots[int(choice) - 1]
        except (ValueError, IndexError):
            fail("Invalid snapshot selection.")

    def list_snapshots(self):
        while True:
            snapshot = self.select_snapshot()
            if snapshot is None:
                return
            if not self.snapshot_restore_actions(snapshot):
                return

    def snapshot_restore_actions(self, snapshot):
        snapshot_id = snapshot.get("id")
        if not snapshot_id:
            fail("The selected snapshot has no ID.")
        paths = ", ".join(snapshot.get("paths", []))
        if len(paths) > 120:
            paths = paths[:117] + "…"
        while True:
            choice = ui.select("SNAPSHOT ACTIONS", [
                ("1", "Restore the entire snapshot"),
                ("2", "Choose individual files and folders"),
                ("3", "Choose another snapshot"),
                ("0", "Back to the main menu"),
            ], f"{self.snapshot_label(snapshot)}  ·  {paths}")
            if choice in (None, "0"):
                return False
            if choice == "3":
                return True
            if choice == "1":
                if self.restore_snapshot_to(snapshot_id):
                    ui.pause("Restore finished; press any key to return to snapshots")
                    return True
            elif choice == "2":
                if self.restore_selected_items(snapshot_id):
                    ui.pause("Restore finished; press any key to return to snapshots")
                    return True

    def restore_target(self):
        while True:
            value = input("Absolute destination directory (0 to cancel): ").strip()
            if value == "0":
                return None
            try:
                return self.validate_restore_target(value)
            except ValueError as exc:
                ui.status(f"{exc} Please try again.", "error")

    @staticmethod
    def validate_restore_target(value):
        target = Path(value.strip())
        if not target.is_absolute() or target == Path("/"):
            raise ValueError("Enter an absolute destination other than /.")
        return target

    def restore_snapshot_to(self, snapshot_id, include_paths=None):
        target = self.restore_target()
        if target is None:
            return False
        if include_paths:
            summary = f"{len(include_paths)} selected file/folder path(s)"
        else:
            summary = "the entire snapshot"
        ui.section("Restore plan")
        print(f"Snapshot: {snapshot_id}")
        print(f"Scope:    {summary}")
        print(f"Target:   {target}")
        if not yes_no("Restored files may overwrite files in the target. Continue?", default=False):
            return False
        target.mkdir(parents=True, exist_ok=True)
        args = ["restore", snapshot_id, "--target", str(target)]
        if include_paths:
            patterns = [self.restic_include_pattern(path) for path in include_paths]
            estimated_size = sum(len(pattern.encode("utf-8")) + 32 for pattern in patterns)
            if estimated_size > 48000:
                if any("\n" in path or "\r" in path or path != path.strip() for path in include_paths):
                    fail("Too many selected paths include line breaks or edge spaces. Reduce the selection and retry.")
                work = credential_store.runtime_dir()
                try:
                    include_file = work / "restore-includes.txt"
                    file_patterns = [self.restic_include_pattern(path, include_file=True) for path in include_paths]
                    include_file.write_text("\n".join(file_patterns) + "\n", encoding="utf-8")
                    os.chmod(include_file, 0o600)
                    args.extend(["--include-file", str(include_file)])
                    restic(*args)
                finally:
                    credential_store.remove_runtime(work)
                return True
            for pattern in patterns:
                args.extend(["--include", pattern])
        restic(*args)
        return True

    @staticmethod
    def restic_include_pattern(path, *, include_file=False):
        # Escape glob syntax so the exact path selected by the user is restored.
        pattern = path.replace("\\", "\\\\").replace("[", "\\[").replace("*", "\\*").replace("?", "\\?")
        if pattern.startswith("!"):
            pattern = "\\" + pattern
        if include_file:
            pattern = pattern.replace("$", "$$")
            if pattern.startswith("#"):
                pattern = "\\" + pattern
        return pattern

    @staticmethod
    def format_size(size):
        try:
            size = int(size)
        except (TypeError, ValueError):
            return ""
        units = ("B", "KiB", "MiB", "GiB", "TiB")
        value = float(size)
        for unit in units:
            if value < 1024 or unit == units[-1]:
                return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
            value /= 1024

    def load_snapshot_nodes(self, snapshot_id):
        output = restic("ls", "--json", snapshot_id, quiet=True, capture_output=True)
        nodes = []
        for line in output.splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("struct_type", record.get("message_type")) != "node":
                continue
            path = record.get("path") or record.get("name")
            if not isinstance(path, str) or not path:
                continue
            if not path.startswith("/"):
                path = "/" + path
            path = posixpath.normpath(path)
            if path == "/":
                continue
            record["path"] = path
            nodes.append(record)
        nodes.sort(key=lambda node: (node["path"].casefold(), node.get("type", "")))
        if not nodes:
            ui.status("This snapshot contains no browsable file entries.", "warning")
        return nodes

    def restore_selected_items(self, snapshot_id):
        nodes = self.load_snapshot_nodes(snapshot_id)
        if not nodes:
            return False
        selected = set()
        while True:
            options = []
            for index, node in enumerate(nodes, start=1):
                path = node["path"]
                kind = "DIR" if node.get("type") == "dir" else str(node.get("type", "FILE")).upper()
                mark = "✓" if path in selected else " "
                size = self.format_size(node.get("size"))
                detail = f"{kind} · {size}" if size else kind
                display_path = path.replace("\n", "\\n").replace("\r", "\\r")
                options.append((str(index), f"[{mark}] {display_path}", detail))
            options.extend([
                ("d", f"Restore selected items ({len(selected)} selected)"),
                ("c", "Clear selection"),
                ("0", "Cancel and return"),
            ])
            choice = ui.select("SELECT RESTORE ITEMS", options, "Enter toggles a file/folder. A selected folder restores its contents.")
            if choice in (None, "0"):
                return False
            if choice == "c":
                selected.clear()
                continue
            if choice == "d":
                if not selected:
                    ui.status("Select at least one file or folder first.", "warning")
                    continue
                paths = self.collapse_selected_paths(selected)
                return self.restore_snapshot_to(snapshot_id, paths)
            if choice.isdecimal():
                index = int(choice) - 1
                if 0 <= index < len(nodes):
                    path = nodes[index]["path"]
                    if path in selected:
                        selected.remove(path)
                    else:
                        selected.add(path)

    @staticmethod
    def collapse_selected_paths(paths):
        result = []
        for path in sorted(paths, key=lambda value: (value.count("/"), value)):
            if not any(parent == "/" or path.startswith(parent.rstrip("/") + "/") for parent in result):
                result.append(path)
        return result

    def mount_worker(self, target):
        target_path = Path(target)
        if not target_path.is_absolute() or target_path == Path("/"):
            fail("Invalid mount directory.")
        restic("mount", str(target_path))

    def mount_snapshots(self):
        if not Path("/dev/fuse").exists():
            if shutil.which("modprobe"):
                subprocess.run(["modprobe", "fuse"], check=False)
            if not Path("/dev/fuse").exists():
                fail("/dev/fuse was not found; enable FUSE on the server.")
        target = Path(input("Mount directory (absolute path): ").strip())
        if not target.is_absolute() or target == Path("/"):
            fail("Enter a dedicated absolute directory.")
        target.mkdir(parents=True, exist_ok=True)
        if subprocess.run(["mountpoint", "-q", str(target)]).returncode == 0:
            fail("The directory is already mounted.")
        log_path = ETC / "mount.log"
        with log_path.open("a") as log:
            process = subprocess.Popen([str(INSTALL_PATH), "--mount-worker", str(target)], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        (ETC / "mount.pid").write_text(str(process.pid))
        (ETC / "mount.path").write_text(str(target))
        os.chmod(ETC / "mount.pid", 0o600)
        os.chmod(ETC / "mount.path", 0o600)
        time.sleep(2)
        if subprocess.run(["mountpoint", "-q", str(target)]).returncode:
            fail(f"Could not mount snapshots. Log: {log_path}")
        info(f"Snapshots mounted at {target}; use snapshots/ and latest/.")

    def unmount_snapshots(self):
        path_file = ETC / "mount.path"
        if not path_file.exists():
            fail("No active mount point is recorded.")
        target = path_file.read_text().strip()
        if subprocess.run(["mountpoint", "-q", target]).returncode:
            fail(f"Directory is not mounted: {target}")
        command = shutil.which("fusermount3") or shutil.which("fusermount") or shutil.which("umount")
        args = [command, "-u", target] if Path(command).name.startswith("fuser") else [command, target]
        run_stream(args)
        for name in ("mount.path", "mount.pid"):
            (ETC / name).unlink(missing_ok=True)

    def show_status(self):
        if RESTIC_BIN.exists():
            run_stream([str(RESTIC_BIN), "version"], label="Restic version")
        subprocess.run(["systemctl", "list-timers", "--all", "msp-backup.timer", "msp-backup-update.timer"], check=False)
        config = read_json(CONFIG_FILE, {})
        db = read_json(DB_CONFIG_FILE, {})
        smtp = read_json(SMTP_CONFIG_FILE, {})
        print(f"Repository: {config.get('repository', 'not configured')}")
        if config.get("backend") == "s3":
            print(f"Storage: S3 bucket {config.get('bucket', '')} (region {config.get('region', 'default')})")
        print(f"Database: {db.get('engine', 'not configured')}")
        print(f"SMTP notifications: {'enabled' if smtp.get('enabled') else 'disabled'}")
        print(f"Log: {LOG_FILE}")

    def configure_menu(self):
        choice = ui.select("SETTINGS", [
            ("1", "Log rotation"),
            ("2", "SMTP notifications"),
            ("3", "Update URL"),
            ("4", "Repository / S3 settings"),
            ("0", "Back"),
        ], "Use ↑/↓ or left/right, then Enter.")
        if choice in (None, "0"):
            return
        if choice == "1":
            configure_log_rotation()
        elif choice == "2":
            configure_notifications()
        elif choice == "3":
            configure_update_url()
        elif choice == "4":
            self.configure_repository()

    def snapshot_tools_menu(self):
        actions = {
            "1": ("Check repository", lambda: restic("check")),
            "2": ("Mount snapshots", mount_snapshots),
            "3": ("Unmount snapshots", unmount_snapshots),
            "4": ("Apply retention and prune", prune_backups),
        }
        self.run_submenu("SNAPSHOT TOOLS", actions)

    def schedule_menu(self):
        actions = {
            "1": ("Enable the daily backup timer", lambda: write_units(read_json(CONFIG_FILE, {}))),
            "2": ("Configure weekly MSP Backup auto-updates", install_script_update_timer),
        }
        self.run_submenu("SCHEDULES", actions)

    def run_submenu(self, title, actions):
        options = [(key, label) for key, (label, _) in actions.items()]
        options.append(("0", "Back to main menu"))
        while True:
            choice = ui.select(title, options, "Use ↑/↓ or left/right, then Enter.")
            if choice in (None, "0"):
                return
            selected = actions.get(choice)
            if selected is None:
                continue
            label, action = selected
            ui.header(label.upper(), "Action in progress")
            action()
            ui.status(f"{label} completed.", "success")
            ui.pause("Review the output; press any key to return")

    def menu(self):
        actions = {
            "1": ("Install/configure backups", lambda: (ensure_restic(), setup_backup())),
            "2": ("Run backup now", lambda: subprocess.run(["systemctl", "start", "msp-backup.service"], check=True)),
            "3": ("Browse snapshots and restore", self.list_snapshots),
            "4": ("Snapshot tools", self.snapshot_tools_menu),
            "5": ("Configure database backups", configure_database),
            "6": ("Schedules", self.schedule_menu),
            "7": ("Settings", configure_menu),
            "8": ("Update Restic", update_restic),
            "9": ("Check for MSP Backup updates", lambda: check_script_update(install=False)),
            "10": ("Status", show_status),
        }
        menu_options = [(key, label) for key, (label, _) in actions.items()]
        menu_options.append(("0", "Exit"))
        while True:
            choice = ui.select("MSP BACKUP", menu_options, f"Backup management  ·  Version {VERSION}  ·  Navigate with arrow keys")
            if choice == "0":
                ui.status("Exiting MSP Backup.", "success")
                return
            if choice is None:
                continue
            selected = actions.get(choice)
            if selected is None:
                continue
            label, action = selected
            ui.header(label.upper(), "Action in progress")
            while True:
                try:
                    action()
                except (BackupError, subprocess.CalledProcessError, OSError, ValueError) as exc:
                    if logger.handlers:
                        logger.error("Action '%s' failed: %s", label, exc)
                    retry = ui.select("ACTION FAILED", [
                        ("r", "Retry this action"),
                        ("0", "Return to main menu"),
                    ], str(exc))
                    if retry == "r":
                        ui.header(label.upper(), "Retrying action")
                        continue
                else:
                    ui.status(f"{label} completed.", "success")
                    ui.pause("Review the output; press any key to return to the menu")
                break

backup_workflows = BackupWorkflowManager()
migrate_legacy_secrets = backup_workflows.migrate_legacy_secrets
import_legacy_config = backup_workflows.import_legacy_config
write_units = backup_workflows.write_units
setup_backup = backup_workflows.setup_backup
run_backup = backup_workflows.run_backup
prune_backups = backup_workflows.prune_backups
mount_worker = backup_workflows.mount_worker
mount_snapshots = backup_workflows.mount_snapshots
unmount_snapshots = backup_workflows.unmount_snapshots
show_status = backup_workflows.show_status
configure_menu = backup_workflows.configure_menu
menu = backup_workflows.menu


class BackupApplication:
    """Own process startup and dispatch CLI/systemd actions to backup services."""

    def __init__(self, argv=None):
        self.argv = list(sys.argv[1:] if argv is None else argv)
        self.system = system_manager
        self.credentials = credential_store
        self.config = config_store
        self.restic = restic_manager
        self.restic_installer = restic_installer
        self.database = database_backup_manager
        self.notifications = notification_manager
        self.updates = update_manager
        self.workflows = backup_workflows

    def install_self(self):
        current = Path(sys.argv[0]).resolve()
        launcher = PACKAGE_DIR.parent / "msp-backup.py"
        if current.name == "__main__.py" and launcher.is_file():
            current = launcher
        if current == INSTALL_PATH:
            return False
        INSTALL_PATH.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        shutil.copy2(current, INSTALL_PATH)
        package_temp = INSTALL_PATH.parent / "msp_backup.new"
        if package_temp.exists():
            shutil.rmtree(package_temp)
        shutil.copytree(PACKAGE_DIR, package_temp, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        package_target = INSTALL_PATH.parent / "msp_backup"
        package_previous = INSTALL_PATH.parent / "msp_backup.previous"
        if package_previous.exists():
            shutil.rmtree(package_previous)
        if package_target.exists():
            os.replace(package_target, package_previous)
        try:
            os.replace(package_temp, package_target)
        except OSError:
            if package_previous.exists():
                os.replace(package_previous, package_target)
            raise
        if package_previous.exists():
            shutil.rmtree(package_previous)
        os.chown(APP_DIR, 0, 0)
        os.chmod(APP_DIR, 0o755)
        os.chown(INSTALL_PATH, 0, 0)
        os.chmod(INSTALL_PATH, 0o755)
        if LEGACY_RESTIC_BIN.is_file() and not RESTIC_INSTALL_BIN.exists():
            shutil.move(str(LEGACY_RESTIC_BIN), str(RESTIC_INSTALL_BIN))
            os.chown(RESTIC_INSTALL_BIN, 0, 0)
            os.chmod(RESTIC_INSTALL_BIN, 0o755)
        os.execv(str(INSTALL_PATH), [str(INSTALL_PATH), *self.argv])
        return True

    def dispatch_system_action(self):
        if not self.argv:
            return False
        command = self.argv[0]
        actions = {
            "--db-dump": self.database.database_dump,
            "--db-clean": self.database.cleanup_database_dump,
            "--run-backup": self.workflows.run_backup,
            "--prune": self.workflows.prune_backups,
            "--notify-success": lambda: self.notifications.notify_event("success"),
            "--notify-failure": lambda: self.notifications.notify_event("failure"),
            "--auto-update-script": lambda: self.updates.check_script_update(install=True, quiet=True),
        }
        if command == "--mount-worker" and len(self.argv) > 1:
            self.workflows.mount_worker(self.argv[1])
            return True
        action = actions.get(command)
        if action is None:
            return False
        action()
        return True

    def run(self):
        self.system.require_root()
        self.system.detect_family()
        self.install_self()
        self.system.configure_logging()
        if not self.argv:
            self.system.install_dependencies()
        credential_backend = self.system.ensure_credential_support()
        logger.info("Selected credential encryption backend: %s", credential_backend)
        imported = self.workflows.import_legacy_config()
        self.workflows.migrate_legacy_secrets()
        if imported and RESTIC_BIN.exists() and RESTIC_PASSWORD_CRED.exists() and CONFIG_FILE.exists() and PATHS_FILE.exists():
            self.workflows.write_units(self.config.read_json(CONFIG_FILE, {}))
        if imported and self.config.read_json(UPDATE_CONFIG_FILE, {}).get("manifest_url") and (SYSTEMD_DIR / "msp-backup-update.timer").exists():
            self.updates.install_script_update_timer()
        if self.dispatch_system_action():
            return 0
        if self.argv:
            self.system.install_dependencies()
        if not LOGROTATE.exists():
            self.system.write_logrotate()
        if CONFIG_FILE.exists():
            update = self.config.read_json(UPDATE_CONFIG_FILE, {})
            if update.get("manifest_url"):
                self.updates.check_script_update(install=False)
        self.workflows.menu()
        return 0


def main():
    return BackupApplication().run()

