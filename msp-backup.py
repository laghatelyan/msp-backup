#!/usr/bin/env python3
"""MSP Backup: Restic, database dumps, logging, SMTP notifications and updates."""

VERSION = "1.0.0"

import bz2
import getpass
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import re
import shutil
import smtplib
import ssl
import subprocess
import sys
import tempfile
import time
from email.message import EmailMessage
from email.utils import getaddresses
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


INSTALL_PATH = Path("/usr/local/sbin/msp-backup")
ETC = Path("/etc/msp-backup")
LOG_DIR = Path("/var/log/msp-backup")
LOG_FILE = LOG_DIR / "msp-backup.log"
LOGROTATE = Path("/etc/logrotate.d/msp-backup")
DB_DUMP_DIR = Path("/run/msp-backup/db-dumps")
SYSTEMD_DIR = Path("/etc/systemd/system")
RESTIC_PASSWORD_CRED = ETC / "restic-password.cred"
DB_PASSWORD_CRED = ETC / "database-password.cred"
SMTP_PASSWORD_CRED = ETC / "smtp-password.cred"
LEGACY_MYSQL_CRED = ETC / "database-mysql-config.cred"
LEGACY_PGPASS_CRED = ETC / "database-pgpass.cred"
BACKUP_TAG = "msp-backup"

CONFIG_FILE = ETC / "config.json"
DB_CONFIG_FILE = ETC / "database.json"
SMTP_CONFIG_FILE = ETC / "notifications.json"
UPDATE_CONFIG_FILE = ETC / "update.json"
PATHS_FILE = ETC / "paths.txt"
SCRIPT_DIR = Path(__file__).resolve().parent
RESTIC_BIN = SCRIPT_DIR / "restic"

RESTIC_API = "https://api.github.com/repos/restic/restic/releases/latest"
logger = logging.getLogger("msp-backup")
FAMILY = ""


class BackupError(RuntimeError):
    pass


def info(message):
    print(f"\n==> {message}", flush=True)
    logger.info(message)


def fail(message):
    logger.error(message)
    raise BackupError(message)


def require_root():
    if os.geteuid() != 0:
        fail("Run this script with sudo or as root.")


def ensure_credential_support():
    if not shutil.which("systemd-creds") or not shutil.which("systemd"):
        fail("systemd-creds and systemd version 250 or later are required. Passwords will not be stored in plaintext.")
    result = subprocess.run(["systemd", "--version"], capture_output=True, text=True)
    match = re.search(r"systemd\s+(\d+)", result.stdout)
    if not match or int(match.group(1)) < 250:
        fail("systemd version 250 or later is required to encrypt secrets.")


def detect_family():
    global FAMILY
    data = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                data[key] = value.strip('"')
    except OSError as exc:
        fail(f"Could not detect the Linux distribution: {exc}")
    tokens = f"{data.get('ID', '')} {data.get('ID_LIKE', '')}".lower()
    if any(name in tokens for name in ("debian", "ubuntu")):
        FAMILY = "debian"
    elif any(name in tokens for name in ("rhel", "fedora", "centos", "rocky", "almalinux", "ol")):
        FAMILY = "rhel"
    else:
        fail(f"Unsupported distribution {data.get('PRETTY_NAME', 'unknown')}; Debian/Ubuntu or a RHEL-based system is required.")


def configure_logging():
    LOG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chown(LOG_DIR, 0, 0)
    os.chmod(LOG_DIR, 0o700)
    LOG_FILE.touch(exist_ok=True)
    os.chown(LOG_FILE, 0, 0)
    os.chmod(LOG_FILE, 0o600)
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    if not LOGROTATE.exists() and shutil.which("logrotate"):
        write_logrotate("daily", 14)


def write_logrotate(frequency="daily", count=14):
    LOGROTATE.parent.mkdir(parents=True, exist_ok=True)
    content = f"""{LOG_FILE} {{
    {frequency}
    rotate {count}
    missingok
    notifempty
    compress
    delaycompress
    copytruncate
    create 0600 root root
}}
"""
    LOGROTATE.write_text(content)
    os.chmod(LOGROTATE, 0o644)


def run_stream(args, *, env=None, label=None, check=True):
    logger.info("Starting %s", label or Path(args[0]).name)
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    assert process.stdout
    for line in process.stdout:
        print(line, end="", flush=True)
        logger.info(line.rstrip())
    code = process.wait()
    if code and check:
        raise BackupError(f"Command exited with code {code}: {label or Path(args[0]).name}")
    return code


def install_dependencies():
    required = ["bzip2", "findmnt", "logrotate", "mountpoint"]
    missing = [name for name in required if not shutil.which(name)]
    if not shutil.which("fusermount3") and not shutil.which("fusermount"):
        missing.append("FUSE (fusermount)")
    if not missing:
        return
    print(f"Missing dependencies: {', '.join(missing)}")
    if input("Install system utilities and FUSE now? [y/N] ").strip().lower() != "y":
        fail("Dependencies were not installed.")
    if FAMILY == "debian":
        commands = [
            ["apt-get", "update"],
            ["apt-get", "install", "-y", "bzip2", "coreutils", "util-linux", "fuse3", "logrotate"],
        ]
    else:
        manager = "dnf" if shutil.which("dnf") else "yum"
        commands = [
            [manager, "install", "-y", "bzip2", "coreutils", "util-linux", "logrotate"],
            [manager, "install", "-y", "fuse3"],
        ]
    for command in commands:
        code = run_stream(command, check=False)
        if code and FAMILY == "rhel" and "fuse3" in command:
            manager = "dnf" if shutil.which("dnf") else "yum"
            run_stream([manager, "install", "-y", "fuse"], label="Install FUSE")
        elif code:
            fail(f"Could not install dependencies using {command[0]}.")
    if not LOGROTATE.exists():
        write_logrotate()
    if not Path("/dev/fuse").exists() and shutil.which("modprobe"):
        subprocess.run(["modprobe", "fuse"], check=False)


def secure_runfs():
    try:
        fs = subprocess.check_output(["findmnt", "-n", "-o", "FSTYPE", "--target", "/run"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        fs = ""
    if fs not in ("tmpfs", "ramfs"):
        fail("/run is not tmpfs/ramfs; refusing to place decrypted secrets on disk.")


def runtime_dir():
    secure_runfs()
    return Path(tempfile.mkdtemp(prefix="msp-backup.", dir="/run"))


def remove_runtime(path):
    if path and str(path).startswith("/run/msp-backup."):
        shutil.rmtree(path, ignore_errors=True)


def encrypt_credential(name, plaintext, destination):
    ETC.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(ETC, 0o700)
    result = subprocess.run(
        ["systemd-creds", "encrypt", "--with-key=auto", f"--name={name}", "-", str(destination)],
        input=plaintext,
    )
    if result.returncode:
        fail(f"Could not encrypt secret {name}.")
    os.chown(destination, 0, 0)
    os.chmod(destination, 0o600)


def decrypt_credential(name, source, destination):
    result = subprocess.run(["systemd-creds", "decrypt", f"--name={name}", str(source), str(destination)])
    if result.returncode:
        raise BackupError(f"Could not decrypt credential {name}.")
    os.chmod(destination, 0o600)


def save_json(path, data):
    ETC.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(ETC, 0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chown(path, 0, 0)


def read_json(path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"Could not read {path}: {exc}")


def load_config():
    config = read_json(CONFIG_FILE)
    if not config or not config.get("repository"):
        fail("Configure the repository first (menu item 1).")
    if not RESTIC_PASSWORD_CRED.is_file() or not RESTIC_BIN.is_file():
        fail("The encrypted password or local Restic binary was not found.")
    return config


class ResticManager:
    """Run Restic with repository credentials kept in memory-backed storage."""

    def __init__(self, binary=RESTIC_BIN, password_credential=RESTIC_PASSWORD_CRED):
        self.binary = Path(binary)
        self.password_credential = Path(password_credential)

    def run(self, *args, check=True, quiet=False):
        config = load_config()
        work = runtime_dir()
        try:
            password = work / "restic-password"
            decrypt_credential("msp-backup-restic", self.password_credential, password)
            env = os.environ.copy()
            env["RESTIC_REPOSITORY"] = config["repository"]
            env["RESTIC_PASSWORD_FILE"] = str(password)
            command = [str(self.binary), *args]
            if quiet:
                result = subprocess.run(command, env=env, capture_output=True, text=True)
                if result.returncode and not check:
                    return result.returncode
                if result.stdout:
                    print(result.stdout, end="")
                    logger.info(result.stdout.rstrip())
                if result.stderr:
                    print(result.stderr, end="", file=sys.stderr)
                    logger.warning(result.stderr.rstrip())
                if check and result.returncode:
                    raise BackupError(f"Restic exited with code {result.returncode}.")
                return result.returncode
            return run_stream(command, env=env, check=check, label=f"restic {args[0]}")
        finally:
            remove_runtime(work)


restic_manager = ResticManager()


def restic(*args, check=True, quiet=False):
    return restic_manager.run(*args, check=check, quiet=quiet)


def download(url, timeout=60):
    request = Request(url, headers={"User-Agent": f"MSP-Backup/{VERSION}"})
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def restic_arch():
    arch = platform.machine().lower()
    return {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64", "i386": "386", "i686": "386", "armv7l": "arm"}.get(arch) or fail(f"Unsupported architecture: {arch}")


def install_restic_latest():
    try:
        release = json.loads(download(RESTIC_API))
        tag = release["tag_name"].removeprefix("v")
        if not re.fullmatch(r"\d+\.\d+\.\d+", tag):
            fail("Invalid tag for the latest Restic release.")
        asset = f"restic_{tag}_linux_{restic_arch()}.bz2"
        base = f"https://github.com/restic/restic/releases/download/v{tag}"
        sums = download(f"{base}/SHA256SUMS").decode()
        expected = None
        for line in sums.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[-1].lstrip("*") == asset:
                expected = parts[0]
                break
        if not expected:
            fail("The binary checksum was not found in SHA256SUMS.")
        compressed = download(f"{base}/{asset}")
        if hashlib.sha256(compressed).hexdigest() != expected:
            fail("Restic checksum verification failed.")
        binary = bz2.decompress(compressed)
        temporary = RESTIC_BIN.with_suffix(".new")
        temporary.write_bytes(binary)
        os.chown(temporary, 0, 0)
        os.chmod(temporary, 0o755)
        os.replace(temporary, RESTIC_BIN)
        os.chown(RESTIC_BIN, 0, 0)
        info(f"Restic {tag} installed beside the script: {RESTIC_BIN}")
        run_stream([str(RESTIC_BIN), "version"], label="Restic version")
    except (URLError, HTTPError, OSError, KeyError, ValueError, IndexError) as exc:
        fail(f"Could not download Restic: {exc}")


def ensure_restic():
    if RESTIC_BIN.exists():
        run_stream([str(RESTIC_BIN), "version"], label="Restic version")
    else:
        install_restic_latest()


def input_default(prompt, default=""):
    value = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return value or default


def input_positive_int(prompt, default, maximum=999):
    value = input_default(prompt, str(default))
    if not value.isdecimal() or not 1 <= int(value) <= maximum:
        fail(f"Value must be between 1 and {maximum}.")
    return int(value)


def yes_no(prompt, default=False):
    suffix = "[Y/n]" if default else "[y/N]"
    value = input(f"{prompt} {suffix} ").strip().lower()
    if not value:
        return default
    return value in ("y", "yes")


def atomic_secret_file(path, content):
    path.write_bytes(content)
    os.chmod(path, 0o600)


def remove_old_secret(path):
    if not path.is_file():
        return
    shred = shutil.which("shred")
    if shred:
        subprocess.run([shred, "-u", str(path)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def migrate_legacy_secrets():
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


def import_legacy_config():
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


def configure_database():
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
        package = {"mysql": "default-mysql-client" if FAMILY == "debian" else "mariadb", "postgresql": "postgresql-client" if FAMILY == "debian" else "postgresql", "sqlite": "sqlite3"}[engine]
        if not yes_no(f"Package {package} is required. Install it?"):
            fail("The database client utility is not installed.")
        command = ["apt-get", "update"] if FAMILY == "debian" else None
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


def escape_mysql(value):
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "")


def escape_pgpass(value):
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("\n", "")


def database_dump():
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


def cleanup_database_dump():
    for filename in ("mysql-all.sql", "postgresql-all.sql", "sqlite-database.db", "mysql-all.sql.tmp", "postgresql-all.sql.tmp", "sqlite-database.db.tmp"):
        (DB_DUMP_DIR / filename).unlink(missing_ok=True)


def write_units(config):
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


def setup_backup():
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


def run_backup():
    config = load_config()
    if not PATHS_FILE.exists():
        fail("The backup path list was not found.")
    restic("backup", "--tag", BACKUP_TAG, "--files-from", str(PATHS_FILE))


def prune_backups():
    config = load_config()
    restic("forget", "--tag", BACKUP_TAG, "--keep-daily", str(config["daily"]), "--keep-weekly", str(config["weekly"]), "--keep-monthly", str(config["monthly"]), "--prune")


def restore_snapshot():
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


def mount_worker(target):
    target_path = Path(target)
    if not target_path.is_absolute() or target_path == Path("/"):
        fail("Invalid mount directory.")
    restic("mount", str(target_path))


def mount_snapshots():
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


def unmount_snapshots():
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


def show_status():
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


def configure_log_rotation():
    frequency = input_default("Rotation frequency [daily/weekly/monthly]", "daily")
    if frequency not in ("daily", "weekly", "monthly"):
        fail("Choose daily, weekly, or monthly.")
    count_text = input_default("Number of archives to keep (1–365)", "14")
    if not count_text.isdecimal() or not 1 <= int(count_text) <= 365:
        fail("The number of archives must be between 1 and 365.")
    write_logrotate(frequency, int(count_text))
    info(f"Log rotation: {frequency}, archives: {count_text}.")


def configure_notifications():
    ETC.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not yes_no("Enable SMTP notifications?", default=True):
        save_json(SMTP_CONFIG_FILE, {"enabled": False})
        SMTP_PASSWORD_CRED.unlink(missing_ok=True)
        info("Notifications are disabled.")
        return
    host = input("SMTP server: ").strip()
    if not host or any(ch.isspace() for ch in host) or "=" in host:
        fail("Invalid SMTP host.")
    tls_choice = input_default("TLS: 1=STARTTLS/587, 2=SSL-TLS/465", "1")
    if tls_choice not in ("1", "2"):
        fail("Choose STARTTLS or SSL/TLS.")
    tls = "starttls" if tls_choice == "1" else "ssl"
    port = input_default("SMTP port", "587" if tls == "starttls" else "465")
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        fail("Invalid SMTP port.")
    auth = yes_no("Use SMTP authentication?", default=True)
    username = ""
    if auth:
        username = input("SMTP username: ").strip()
        secret = getpass.getpass("SMTP password: ")
        if not username or not secret:
            fail("SMTP username and password are required.")
        encrypt_credential("msp-backup-smtp", secret.encode(), SMTP_PASSWORD_CRED)
    else:
        SMTP_PASSWORD_CRED.unlink(missing_ok=True)
    sender = input("Sender email address: ").strip()
    recipients = input("Recipient email addresses, comma-separated: ").strip()
    if "@" not in sender or any(ch.isspace() for ch in sender) or "@" not in recipients or any(ch.isspace() for ch in recipients):
        fail("Enter valid email addresses.")
    mode_choice = input_default("1=Always, 2=Only on failures", "2")
    if mode_choice not in ("1", "2"):
        fail("Choose 1 or 2.")
    data = {"enabled": True, "host": host, "port": int(port), "tls": tls, "auth": auth,
            "user": username, "from": sender, "to": recipients, "mode": "always" if mode_choice == "1" else "failures"}
    save_json(SMTP_CONFIG_FILE, data)
    if yes_no("Send a test email now?", default=True):
        notify_event("test")


def notify_event(event):
    config = read_json(SMTP_CONFIG_FILE, {})
    if not config.get("enabled"):
        return
    if event == "success" and config.get("mode") != "always":
        return
    host_name = subprocess.run(["hostname", "-f"], capture_output=True, text=True).stdout.strip() or platform.node()
    if event == "failure":
        journal = subprocess.run(["journalctl", "-u", "msp-backup.service", "-n", "40", "--no-pager"], capture_output=True, text=True).stdout
        body = f"The backup failed.\nServer: {host_name}\n\n{journal}"
        subject = f"[MSP Backup] Failure on {host_name}"
    elif event == "test":
        subject = "[MSP Backup] SMTP test"
        body = f"This is an MSP Backup test email.\nServer: {host_name}"
    else:
        subject = "[MSP Backup] Backup completed"
        body = f"The backup completed successfully.\nServer: {host_name}\nVersion: {VERSION}"
    work = runtime_dir()
    try:
        if config.get("auth"):
            password_path = work / "smtp-password"
            decrypt_credential("msp-backup-smtp", SMTP_PASSWORD_CRED, password_path)
            password = password_path.read_text().rstrip("\n")
        else:
            password = ""
        message = EmailMessage()
        message["From"] = config["from"]
        recipients = [address for _, address in getaddresses([config["to"]]) if address]
        if not recipients:
            raise BackupError("No SMTP recipients are configured.")
        message["To"] = ", ".join(recipients)
        message["Subject"] = subject
        message.set_content(body)
        context = ssl.create_default_context()
        if config["tls"] == "ssl":
            server = smtplib.SMTP_SSL(config["host"], config["port"], timeout=30, context=context)
        else:
            server = smtplib.SMTP(config["host"], config["port"], timeout=30)
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
        try:
            if config.get("auth"):
                server.login(config["user"], password)
            server.send_message(message, to_addrs=recipients)
        finally:
            try:
                server.quit()
            except Exception:
                pass
        info(f"SMTP notification sent ({event}).")
    except Exception as exc:
        logger.exception("SMTP notification failed")
        print(f"Could not send SMTP notification: {exc}", file=sys.stderr)
    finally:
        remove_runtime(work)


def configure_update_url():
    current = read_json(UPDATE_CONFIG_FILE, {})
    print("The manifest must be JSON with version, url, and sha256 fields.")
    url = input_default("URL manifest", current.get("manifest_url", ""))
    if not url.startswith("https://"):
        fail("Updates require an HTTPS URL.")
    save_json(UPDATE_CONFIG_FILE, {"manifest_url": url})
    info("Update check URL saved.")


def parse_version(value):
    match = re.fullmatch(r"\s*v?(\d+(?:\.\d+){1,3})(?:[-+].*)?\s*", value)
    if not match:
        raise BackupError(f"Invalid version: {value}")
    parts = [int(part) for part in match.group(1).split(".")]
    return tuple((parts + [0, 0, 0, 0])[:4])


def check_script_update(install=False, quiet=False):
    config = read_json(UPDATE_CONFIG_FILE, {})
    manifest_url = config.get("manifest_url")
    if not manifest_url:
        if not quiet:
            print("Update URL is not configured (menu item 15).")
        return False
    try:
        manifest = json.loads(download(manifest_url).decode("utf-8"))
        latest = manifest["version"]
        script_url = manifest["url"]
        expected = manifest["sha256"].lower()
        if not script_url.startswith("https://") or not re.fullmatch(r"[0-9a-f]{64}", expected):
            fail("The manifest contains an insecure URL or invalid SHA-256.")
        if parse_version(latest) <= parse_version(VERSION):
            if not quiet:
                info(f"MSP Backup is up to date: {VERSION}.")
            return False
        print(f"MSP Backup {latest} is available (installed: {VERSION}).")
        if not install:
            if not yes_no("Download, verify the SHA-256, and install the update?"):
                return False
        script = download(script_url)
        if hashlib.sha256(script).hexdigest() != expected:
            fail("The update SHA-256 does not match the manifest.")
        text = script.decode("utf-8")
        match = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', text, re.M)
        if not match or match.group(1) != latest:
            fail("The downloaded script version does not match the manifest.")
        temp = INSTALL_PATH.with_suffix(".new")
        temp.write_bytes(script)
        os.chown(temp, 0, 0)
        os.chmod(temp, 0o755)
        os.replace(temp, INSTALL_PATH)
        info(f"MSP Backup was updated to {latest}; the new version will run on the next start.")
        return True
    except (URLError, HTTPError, OSError, ValueError, KeyError, UnicodeDecodeError) as exc:
        logger.exception("Update check failed")
        print(f"Could not check for or install the update: {exc}", file=sys.stderr)
        return False


def install_script_update_timer():
    if not read_json(UPDATE_CONFIG_FILE, {}).get("manifest_url"):
        configure_update_url()
    service = f"""[Unit]
Description=Check for MSP Backup updates

[Service]
Type=oneshot
ExecStart={INSTALL_PATH} --auto-update-script
"""
    timer = """[Unit]
Description=Check MSP Backup updates weekly

[Timer]
OnCalendar=Sun *-*-* 04:00:00
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
"""
    (SYSTEMD_DIR / "msp-backup-update.service").write_text(service)
    (SYSTEMD_DIR / "msp-backup-update.timer").write_text(timer)
    os.chmod(SYSTEMD_DIR / "msp-backup-update.service", 0o644)
    os.chmod(SYSTEMD_DIR / "msp-backup-update.timer", 0o644)
    run_stream(["systemctl", "daemon-reload"])
    run_stream(["systemctl", "enable", "--now", "msp-backup-update.timer"])


def configure_menu():
    print("1) Log rotation  2) SMTP  3) Update URL  0) Back")
    choice = input("Choice: ").strip()
    if choice == "1":
        configure_log_rotation()
    elif choice == "2":
        configure_notifications()
    elif choice == "3":
        configure_update_url()


def menu():
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


def update_restic():
    if not RESTIC_BIN.exists():
        install_restic_latest()
        return
    run_stream([str(RESTIC_BIN), "self-update", "--output", str(RESTIC_BIN.with_suffix(".new"))], label="Update Restic")
    replacement = RESTIC_BIN.with_suffix(".new")
    if replacement.exists():
        os.chmod(replacement, 0o755)
        os.replace(replacement, RESTIC_BIN)
        info("Restic updated.")


class BackupApplication:
    """Own process startup and dispatch CLI/systemd actions to backup services."""

    def __init__(self, argv=None):
        self.argv = list(sys.argv[1:] if argv is None else argv)

    def install_self(self):
        current = Path(__file__).resolve()
        if current == INSTALL_PATH:
            return False
        INSTALL_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(current, INSTALL_PATH)
        os.chown(INSTALL_PATH, 0, 0)
        os.chmod(INSTALL_PATH, 0o755)
        os.execv(str(INSTALL_PATH), [str(INSTALL_PATH), *self.argv])
        return True

    def dispatch_system_action(self):
        if not self.argv:
            return False
        command = self.argv[0]
        actions = {
            "--db-dump": lambda: database_dump(),
            "--db-clean": lambda: cleanup_database_dump(),
            "--run-backup": lambda: run_backup(),
            "--prune": lambda: prune_backups(),
            "--notify-success": lambda: notify_event("success"),
            "--notify-failure": lambda: notify_event("failure"),
            "--auto-update-script": lambda: check_script_update(install=True, quiet=True),
        }
        if command == "--mount-worker" and len(self.argv) > 1:
            mount_worker(self.argv[1])
            return True
        action = actions.get(command)
        if action is None:
            return False
        action()
        return True

    def run(self):
        require_root()
        detect_family()
        self.install_self()
        ensure_credential_support()
        configure_logging()
        imported = import_legacy_config()
        migrate_legacy_secrets()
        if imported and RESTIC_BIN.exists() and RESTIC_PASSWORD_CRED.exists() and CONFIG_FILE.exists() and PATHS_FILE.exists():
            write_units(read_json(CONFIG_FILE, {}))
        if self.dispatch_system_action():
            return 0
        install_dependencies()
        if not LOGROTATE.exists():
            write_logrotate()
        if CONFIG_FILE.exists():
            update = read_json(UPDATE_CONFIG_FILE, {})
            if update.get("manifest_url"):
                check_script_update(install=False)
        menu()
        return 0


def main():
    return BackupApplication().run()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BackupError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        logger.error("%s", exc)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nOperation cancelled.", file=sys.stderr)
        sys.exit(130)
