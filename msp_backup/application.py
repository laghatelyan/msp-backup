from pathlib import Path
import getpass
import os
import shutil
import subprocess
import sys
import time


from .common import BACKUP_TAG, CONFIG_FILE, DB_CONFIG_FILE, DB_DUMP_DIR, ETC, INSTALL_PATH, LEGACY_MYSQL_CRED, LEGACY_PGPASS_CRED, LOGROTATE, LOG_FILE, PACKAGE_DIR, PATHS_FILE, RESTIC_BIN, RESTIC_PASSWORD_CRED, SMTP_CONFIG_FILE, SMTP_PASSWORD_CRED, SYSTEMD_DIR, UPDATE_CONFIG_FILE, config_store, credential_store, encrypt_credential, fail, info, input_positive_int, load_config, read_json, remove_old_secret, run_stream, save_json, system_manager, yes_no
from .restic import ensure_restic, restic, restic_installer, update_restic
from .database import configure_database, database_backup_manager
from .notifications import configure_log_rotation, configure_notifications, notification_manager
from .updates import check_script_update, configure_update_url, install_script_update_timer, update_manager

class BackupWorkflowManager:
    """Own setup, backup, restore, mount, and interactive workflows."""

    def migrate_legacy_secrets(self):
        ETC.mkdir(mode=0o700, parents=True, exist_ok=True)
        old_roots = [ETC, Path("/etc/restic-wizard")]
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
                    subprocess.run(["systemd-creds", "encrypt", "--with-key=auto", f"--name={name}", str(source), str(target)], check=True)
                    os.chmod(target, 0o600)
                remove_old_secret(source)
        old_dump = Path("/var/lib/msp-backup/db-dumps")
        for filename in ("mysql-all.sql", "postgresql-all.sql", "sqlite-database.db", "mysql-all.sql.tmp", "postgresql-all.sql.tmp", "sqlite-database.db.tmp"):
            remove_old_secret(old_dump / filename)

    def import_legacy_config(self):
        legacy_roots = [ETC, Path("/etc/restic-wizard")]
        legacy = next((root for root in legacy_roots if (root / "config").is_file()), None)
        if CONFIG_FILE.exists() or legacy is None:
            return False
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
        (SYSTEMD_DIR / "msp-backup.service").write_text(service)
        (SYSTEMD_DIR / "msp-backup.timer").write_text(timer)
        (SYSTEMD_DIR / "msp-backup-notify-failure.service").write_text(failure)
        for path in (SYSTEMD_DIR / "msp-backup.service", SYSTEMD_DIR / "msp-backup.timer", SYSTEMD_DIR / "msp-backup-notify-failure.service"):
            os.chmod(path, 0o644)
        run_stream(["systemctl", "daemon-reload"])
        run_stream(["systemctl", "enable", "--now", "msp-backup.timer"])

    def setup_backup(self):
        ETC.mkdir(mode=0o700, parents=True, exist_ok=True)
        repository = input("Restic repository (local path or backend): ").strip()
        if not repository or "'" in repository or "\n" in repository:
            fail("Invalid repository address.")
        local_repo = None
        if repository.startswith("/"):
            local_repo = Path(os.path.realpath(repository))
            if local_repo == Path("/"):
                fail("The filesystem root / cannot be used as a repository.")
            local_repo.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chown(local_repo, 0, 0)
            os.chmod(local_repo, 0o700)
            repository = str(local_repo)
        password = getpass.getpass("Repository password (at least 12 characters): ")
        if len(password) < 12:
            fail("The password must be at least 12 characters long.")
        if password != getpass.getpass("Confirm password: "):
            fail("Passwords do not match.")
        encrypt_credential("msp-backup-restic", password.encode(), RESTIC_PASSWORD_CRED)
        paths = []
        print("Backup paths: enter one absolute path per line; an empty line finishes input.")
        while True:
            value = input("> ").strip()
            if not value:
                break
            path = Path(value)
            if not path.is_absolute() or not path.exists():
                fail(f"Path must exist and be absolute: {value}")
            paths.append(str(path))
        if not paths:
            fail("At least one path is required.")
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
            "repository": repository,
        }
        save_json(CONFIG_FILE, keep)
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

    def restore_snapshot(self):
        restic("snapshots")
        snapshot = input("Snapshot ID (or latest): ").strip()
        target = Path(input("Absolute destination directory: ").strip())
        if not snapshot or not target.is_absolute() or target == Path("/"):
            fail("Invalid snapshot or destination directory.")
        if not yes_no("Restore may overwrite existing files. Continue?"):
            return
        subpath = input("Path within the snapshot (press Enter for the entire snapshot): ").strip()
        target.mkdir(parents=True, exist_ok=True)
        args = ["restore", snapshot, "--target", str(target)]
        if subpath:
            args += ["--include", "/" + subpath.lstrip("/")]
        restic(*args)

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
        for timer in ("msp-backup.timer", "msp-backup-update.timer"):
            subprocess.run(["systemctl", "list-timers", "--all", timer], check=False)
        config = read_json(CONFIG_FILE, {})
        db = read_json(DB_CONFIG_FILE, {})
        smtp = read_json(SMTP_CONFIG_FILE, {})
        print(f"Repository: {config.get('repository', 'not configured')}")
        print(f"Database: {db.get('engine', 'not configured')}")
        print(f"SMTP notifications: {'enabled' if smtp.get('enabled') else 'disabled'}")
        print(f"Log: {LOG_FILE}")

    def configure_menu(self):
        print("1) Log rotation  2) SMTP  3) Update URL  0) Back")
        choice = input("Choice: ").strip()
        if choice == "1":
            configure_log_rotation()
        elif choice == "2":
            configure_notifications()
        elif choice == "3":
            configure_update_url()

    def menu(self):
        print("\nMSP Backup — Backup Management")
        print(" 1) Install/configure backups")
        print(" 2) Update Restic")
        print(" 3) Enable the daily backup timer")
        print(" 4) Status")
        print(" 5) Run backup now")
        print(" 6) List snapshots")
        print(" 7) Check repository")
        print(" 8) Restore snapshot")
        print(" 9) Mount snapshots")
        print("10) Unmount snapshots")
        print("11) Apply retention and prune")
        print("12) Configure database backups")
        print("13) Configure logs, SMTP, and updates")
        print("14) Check for MSP Backup updates")
        print("15) Configure weekly MSP Backup auto-updates")
        choice = input("Action (0 to exit): ").strip()
        actions = {
            "1": lambda: (ensure_restic(), setup_backup()),
            "2": update_restic,
            "3": lambda: write_units(read_json(CONFIG_FILE, {})),
            "4": show_status,
            "5": lambda: subprocess.run(["systemctl", "start", "msp-backup.service"], check=True),
            "6": lambda: restic("snapshots"),
            "7": lambda: restic("check"),
            "8": restore_snapshot,
            "9": mount_snapshots,
            "10": unmount_snapshots,
            "11": prune_backups,
            "12": configure_database,
            "13": configure_menu,
            "14": lambda: check_script_update(install=False),
            "15": install_script_update_timer,
        }
        if choice in actions:
            actions[choice]()

backup_workflows = BackupWorkflowManager()
migrate_legacy_secrets = backup_workflows.migrate_legacy_secrets
import_legacy_config = backup_workflows.import_legacy_config
write_units = backup_workflows.write_units
setup_backup = backup_workflows.setup_backup
run_backup = backup_workflows.run_backup
prune_backups = backup_workflows.prune_backups
restore_snapshot = backup_workflows.restore_snapshot
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
        if current == INSTALL_PATH:
            return False
        INSTALL_PATH.parent.mkdir(parents=True, exist_ok=True)
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
        os.chown(INSTALL_PATH, 0, 0)
        os.chmod(INSTALL_PATH, 0o755)
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
        self.system.ensure_credential_support()
        self.system.configure_logging()
        imported = self.workflows.import_legacy_config()
        self.workflows.migrate_legacy_secrets()
        if imported and RESTIC_BIN.exists() and RESTIC_PASSWORD_CRED.exists() and CONFIG_FILE.exists() and PATHS_FILE.exists():
            self.workflows.write_units(self.config.read_json(CONFIG_FILE, {}))
        if self.dispatch_system_action():
            return 0
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

