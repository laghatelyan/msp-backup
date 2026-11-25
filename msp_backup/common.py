"""Shared constants, errors, and small cross-component helpers."""

import logging
from pathlib import Path
import sys

from .version import VERSION

APP_DIR = Path("/opt/msp-backup")
INSTALL_PATH = APP_DIR / "msp-backup"
RESTIC_INSTALL_BIN = APP_DIR / "restic"
LEGACY_RESTIC_BIN = Path("/usr/local/sbin/restic")

ETC = APP_DIR / "data"
OLD_ETC = Path("/etc/msp-backup")

LOG_DIR = APP_DIR / "logs"
RESTIC_CACHE_DIR = APP_DIR / "cache"

LOG_FILE = LOG_DIR / "msp-backup.log"

LOGROTATE = Path("/etc/logrotate.d/msp-backup")
APP_SYSTEMD_DIR = APP_DIR / "systemd"
LOGROTATE_CONFIG = APP_SYSTEMD_DIR / "msp-backup.logrotate"

DB_DUMP_DIR = APP_DIR / "tmp" / "db-dumps"
LEGACY_DB_DUMP_DIR = Path("/run/msp-backup/db-dumps")

SYSTEMD_DIR = Path("/etc/systemd/system")

RESTIC_PASSWORD_CRED = ETC / "restic-password.cred"
S3_CREDENTIALS_CRED = ETC / "s3-credentials.cred"

DB_PASSWORD_CRED = ETC / "database-password.cred"

SMTP_PASSWORD_CRED = ETC / "smtp-password.cred"
RUN_REPORT_FILE = ETC / "last-run.json"
PYTHON_CREDENTIAL_KEY = ETC / "credential.key"

LEGACY_MYSQL_CRED = ETC / "database-mysql-config.cred"

LEGACY_PGPASS_CRED = ETC / "database-pgpass.cred"

BACKUP_TAG = "msp-backup"

CONFIG_FILE = ETC / "config.json"

DB_CONFIG_FILE = ETC / "database.json"

SMTP_CONFIG_FILE = ETC / "notifications.json"

UPDATE_CONFIG_FILE = ETC / "update.json"

PATHS_FILE = ETC / "paths.txt"

ENTRY_PATH = Path(sys.argv[0]).resolve()
SCRIPT_DIR = ENTRY_PATH.parent.parent if ENTRY_PATH.name == "__main__.py" and ENTRY_PATH.parent.name == "msp_backup" else ENTRY_PATH.parent
PACKAGE_DIR = Path(__file__).resolve().parent

RESTIC_BIN = SCRIPT_DIR / "restic"

RESTIC_API = "https://api.github.com/repos/restic/restic/releases/latest"

logger = logging.getLogger("msp-backup")

FAMILY = ""
CREDENTIAL_BACKEND = ""

class BackupError(RuntimeError):
    pass

from .terminal import TerminalUI

ui = TerminalUI()

def get_family():
    return FAMILY

def link_systemd_unit(source):
    source = Path(source)
    SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)
    destination = SYSTEMD_DIR / source.name
    if destination.is_symlink() and destination.resolve() == source.resolve():
        return
    if destination.is_symlink():
        destination.unlink()
    elif destination.exists():
        try:
            previous = destination.read_text(errors="replace")
        except OSError as exc:
            fail(f"Could not inspect existing systemd unit {destination}: {exc}")
        if "MSP Backup" not in previous:
            fail(f"Refusing to replace unrelated systemd unit: {destination}")
        destination.unlink()
    destination.symlink_to(source.resolve())

def info(message):
    ui.status(message, "success")
    logger.info(message)

def fail(message):
    raise BackupError(message)

def input_default(prompt, default=""):
    value = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return value or default

def input_positive_int(prompt, default, maximum=999):
    while True:
        value = input_default(prompt, str(default))
        if value.isdecimal() and 1 <= int(value) <= maximum:
            return int(value)
        ui.status(f"Value must be between 1 and {maximum}. Try again.", "error")

def yes_no(prompt, default=False):
    if not ui.interactive:
        suffix = "[Y/n]" if default else "[y/N]"
        while True:
            value = input(f"{prompt} {suffix} ").strip().lower()
            if not value:
                return default
            if value in ("y", "yes"):
                return True
            if value in ("n", "no"):
                return False
            ui.status("Enter y or n.", "error")
    initial = 0 if default else 1
    choice = ui.select(prompt, [("y", "Yes"), ("n", "No")], "Use ↑/↓ or left/right, then Enter.", initial=initial)
    return default if choice is None else choice == "y"

