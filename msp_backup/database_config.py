"""Interactive setup for database backup credentials."""

from pathlib import Path
import getpass
import os
import shutil

from .common import (DB_CONFIG_FILE, DB_DUMP_DIR, DB_PASSWORD_CRED, ETC,
                     LEGACY_MYSQL_CRED, LEGACY_PGPASS_CRED, PATHS_FILE,
                     fail, get_family, info, input_default, ui, yes_no)
from .configuration import save_json
from .credentials import encrypt_credential
from .panels import ControlPanelDetector
from .system import run_stream, secure_runfs


class DatabaseConfigurationWorkflow:
    """Configure a supported database source and encrypted service credentials."""

    def configure_database(self):
        ETC.mkdir(mode=0o700, parents=True, exist_ok=True)
        panels = ControlPanelDetector().discover()
        options = []
        panel_choices = {}
        for index, panel in enumerate(panels, start=1):
            key = f"p{index}"
            panel_choices[key] = panel
            options.append((key, f"Use {panel.name} MySQL credentials",
                            f"Reads {panel.mysql_config}; password is re-encrypted for MSP Backup"))
        options.extend([
            ("1", "MySQL / MariaDB — enter credentials", "Discover every database"),
            ("2", "PostgreSQL — enter credentials", "Discover every database"),
            ("3", "SQLite — choose a file or directory", "Discover SQLite files in a directory"),
            ("0", "Cancel"),
        ])
        while True:
            choice = ui.select("DATABASE BACKUP", options,
                               "Detected panel credentials can be imported without displaying the password.")
            if choice in (None, "0"):
                return False
            panel = panel_choices.get(choice)
            if panel is None:
                engine = {"1": "mysql", "2": "postgresql", "3": "sqlite"}.get(choice)
                if engine:
                    break
                ui.status("Choose one of the listed options.", "error")
                continue
            try:
                panel_credentials = panel.read_mysql_credentials()
                engine = "mysql"
                break
            except BackupError as exc:
                ui.status(f"{exc} Select another source or configure credentials manually.", "error")
        ui.status("The database account must be able to list and export all databases.", "warning")
        if engine == "mysql":
            ui.status("MySQL/MariaDB also needs permission to read account definitions and grants.", "warning")
        clients = {"mysql": (("mariadb-dump", "mysqldump"), ("mariadb", "mysql")),
                   "postgresql": (("pg_dump",), ("pg_dumpall",), ("psql",)),
                   "sqlite": ()}[engine]
        if any(not any(shutil.which(cmd) for cmd in group) for group in clients):
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
            if engine == "mysql" and panel is not None:
                data.update({"host": panel_credentials.host, "port": panel_credentials.port,
                             "user": panel_credentials.user, "credential_source": panel_credentials.panel})
                secret = panel_credentials.password
                info(f"Imported the {panel_credentials.panel} MySQL account; the password will be stored encrypted by MSP Backup.")
            else:
                default_port = "3306" if engine == "mysql" else "5432"
                default_host = "localhost" if engine == "mysql" else "127.0.0.1"
                while True:
                    data["host"] = input_default("Database host", default_host).strip()
                    if data["host"]:
                        break
                    ui.status("Database host cannot be empty.", "error")
                while True:
                    port = input_default("Database port", default_port)
                    if port.isdecimal() and 1 <= int(port) <= 65535:
                        break
                    ui.status("Enter a port from 1 to 65535.", "error")
                data["port"] = int(port)
                while True:
                    data["user"] = input("Database user: ").strip()
                    if data["user"]:
                        break
                    ui.status("Database user is required.", "error")
                secret = getpass.getpass("Database password: ")
                data.pop("credential_source", None)
            encrypt_credential("msp-backup-database", secret.encode(), DB_PASSWORD_CRED)
        else:
            while True:
                value = input("Absolute path to a SQLite file or containing directory (0 to cancel): ").strip()
                if value == "0":
                    return False
                path = Path(value)
                if path.is_absolute() and (path.is_file() or path.is_dir()):
                    break
                ui.status("Enter the absolute path to an existing SQLite file or directory.", "error")
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
        info(f"{engine} backups configured; all databases are discovered at backup time and stored separately.")
        return True
