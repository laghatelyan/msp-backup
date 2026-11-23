"""MSP Backup: Restic, database dumps, logging, SMTP notifications and updates."""

import json
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from .version import VERSION



APP_DIR = Path("/opt/msp-backup")
INSTALL_PATH = APP_DIR / "msp-backup"
RESTIC_INSTALL_BIN = APP_DIR / "restic"
LEGACY_RESTIC_BIN = Path("/usr/local/sbin/restic")

ETC = APP_DIR / "data"
OLD_ETC = Path("/etc/msp-backup")

LOG_DIR = APP_DIR / "logs"

LOG_FILE = LOG_DIR / "msp-backup.log"

LOGROTATE = Path("/etc/logrotate.d/msp-backup")
APP_SYSTEMD_DIR = APP_DIR / "systemd"
LOGROTATE_CONFIG = APP_SYSTEMD_DIR / "msp-backup.logrotate"

DB_DUMP_DIR = Path("/run/msp-backup/db-dumps")

SYSTEMD_DIR = Path("/etc/systemd/system")

RESTIC_PASSWORD_CRED = ETC / "restic-password.cred"

DB_PASSWORD_CRED = ETC / "database-password.cred"

SMTP_PASSWORD_CRED = ETC / "smtp-password.cred"
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
    print(f"\n==> {message}", flush=True)
    logger.info(message)


def fail(message):
    raise BackupError(message)
































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

class SystemManager:
    """Manage system operations."""

    def require_root(self):
        if os.geteuid() != 0:
            fail("Run this script with sudo or as root.")

    def systemd_creds_supported(self):
        if not shutil.which("systemd-creds") or not shutil.which("systemd"):
            return False
        result = subprocess.run(["systemd", "--version"], capture_output=True, text=True)
        match = re.search(r"systemd\s+(\d+)", result.stdout)
        return bool(match and int(match.group(1)) >= 250)

    def ensure_credential_support(self):
        global CREDENTIAL_BACKEND
        if self.systemd_creds_supported():
            CREDENTIAL_BACKEND = "systemd"
            return CREDENTIAL_BACKEND
        try:
            from cryptography.fernet import Fernet  # noqa: F401
        except ImportError:
            CREDENTIAL_BACKEND = ""
            fail("Secret encryption needs systemd-creds (systemd 250+) or the Python package 'cryptography'.")
        CREDENTIAL_BACKEND = "python"
        return CREDENTIAL_BACKEND

    def detect_family(self):
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

    def configure_logging(self):
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
        if (not LOGROTATE.is_symlink() or not LOGROTATE.exists()) and shutil.which("logrotate"):
            write_logrotate("daily", 14)

    def write_logrotate(self, frequency="daily", count=14):
        if LOGROTATE.is_file() and not LOGROTATE.is_symlink():
            try:
                legacy = LOGROTATE.read_text()
                old_frequency = re.search(r"^\s*(daily|weekly|monthly)\s*$", legacy, re.M)
                old_count = re.search(r"^\s*rotate\s+(\d+)\s*$", legacy, re.M)
                if frequency == "daily" and old_frequency:
                    frequency = old_frequency.group(1)
                if count == 14 and old_count:
                    count = min(365, max(1, int(old_count.group(1))))
            except OSError:
                pass
        APP_SYSTEMD_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
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
        LOGROTATE_CONFIG.write_text(content)
        os.chown(LOGROTATE_CONFIG, 0, 0)
        os.chmod(LOGROTATE_CONFIG, 0o644)
        if LOGROTATE.is_symlink():
            LOGROTATE.unlink()
        elif LOGROTATE.exists():
            LOGROTATE.unlink()
        LOGROTATE.parent.mkdir(parents=True, exist_ok=True)
        LOGROTATE.symlink_to(LOGROTATE_CONFIG)

    def run_stream(self, args, *, env=None, label=None, check=True):
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

    def install_dependencies(self):
        required = ["bzip2", "findmnt", "logrotate", "mountpoint"]
        missing = [name for name in required if not shutil.which(name)]
        if not shutil.which("fusermount3") and not shutil.which("fusermount"):
            missing.append("FUSE (fusermount)")
        needs_crypto = not self.systemd_creds_supported()
        if needs_crypto:
            try:
                from cryptography.fernet import Fernet  # noqa: F401
            except ImportError:
                missing.append("Python cryptography library")
        if not missing:
            return
        print(f"Missing dependencies: {', '.join(missing)}")
        if input("Install the missing system/Python dependencies now? [y/N] ").strip().lower() != "y":
            fail("Dependencies were not installed.")
        if FAMILY == "debian":
            packages = ["bzip2", "coreutils", "util-linux", "fuse3", "logrotate"]
            if needs_crypto:
                try:
                    from cryptography.fernet import Fernet  # noqa: F401
                except ImportError:
                    packages.append("python3-cryptography")
            commands = [
                ["apt-get", "update"],
                ["apt-get", "install", "-y", *packages],
            ]
        else:
            manager = "dnf" if shutil.which("dnf") else "yum"
            packages = ["bzip2", "coreutils", "util-linux", "logrotate"]
            if needs_crypto:
                try:
                    from cryptography.fernet import Fernet  # noqa: F401
                except ImportError:
                    packages.append("python3-cryptography")
            commands = [
                [manager, "install", "-y", *packages],
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

    def secure_runfs(self):
        try:
            fs = subprocess.check_output(["findmnt", "-n", "-o", "FSTYPE", "--target", "/run"], text=True).strip()
        except (OSError, subprocess.CalledProcessError):
            fs = ""
        if fs not in ("tmpfs", "ramfs"):
            fail("/run is not tmpfs/ramfs; refusing to place decrypted secrets on disk.")

system_manager = SystemManager()
require_root = system_manager.require_root
ensure_credential_support = system_manager.ensure_credential_support
detect_family = system_manager.detect_family
configure_logging = system_manager.configure_logging
write_logrotate = system_manager.write_logrotate
run_stream = system_manager.run_stream
install_dependencies = system_manager.install_dependencies
secure_runfs = system_manager.secure_runfs

class CredentialStore:
    """Manage credential operations."""

    def runtime_dir(self):
        secure_runfs()
        return Path(tempfile.mkdtemp(prefix="msp-backup.", dir="/run"))

    def remove_runtime(self, path):
        if path and str(path).startswith("/run/msp-backup."):
            shutil.rmtree(path, ignore_errors=True)

    def _fernet(self):
        try:
            from cryptography.fernet import Fernet
        except ImportError:
            fail("The Python cryptography package is required for file-based credential encryption.")
        ETC.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chown(ETC, 0, 0)
        os.chmod(ETC, 0o700)
        if not PYTHON_CREDENTIAL_KEY.exists():
            key = Fernet.generate_key()
            fd, temporary_name = tempfile.mkstemp(prefix="credential-key.", dir=ETC)
            temporary = Path(temporary_name)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as key_file:
                    key_file.write(key)
                    key_file.flush()
                    os.fsync(key_file.fileno())
                os.chown(temporary, 0, 0)
                try:
                    os.link(temporary, PYTHON_CREDENTIAL_KEY)
                except FileExistsError:
                    pass
            finally:
                temporary.unlink(missing_ok=True)
        try:
            metadata = PYTHON_CREDENTIAL_KEY.lstat()
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0
                    or metadata.st_mode & 0o077):
                fail("The credential key must be a root-owned regular file with mode 0600.")
            return Fernet(PYTHON_CREDENTIAL_KEY.read_bytes())
        except (OSError, ValueError) as exc:
            raise BackupError(f"Could not load the Python credential key: {exc}") from exc

    @staticmethod
    def _write_protected(destination, content):
        fd, temporary_name = tempfile.mkstemp(prefix=f"{destination.name}.", dir=destination.parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.chown(temporary, 0, 0)
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
            os.chown(destination, 0, 0)
            os.chmod(destination, 0o600)
        finally:
            temporary.unlink(missing_ok=True)

    def encrypt_credential(self, name, plaintext, destination):
        ETC.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(ETC, 0o700)
        if CREDENTIAL_BACKEND == "systemd":
            result = subprocess.run(
                ["systemd-creds", "encrypt", "--with-key=auto", f"--name={name}", "-", str(destination)],
                input=plaintext,
            )
            if result.returncode:
                fail(f"Could not encrypt secret {name}.")
            os.chown(destination, 0, 0)
            os.chmod(destination, 0o600)
            return
        if CREDENTIAL_BACKEND == "python":
            encrypted = b"MSP-BACKUP-FERNET-1\n" + self._fernet().encrypt(plaintext)
            self._write_protected(destination, encrypted)
            return
        fail("No credential encryption backend has been selected.")

    def decrypt_credential(self, name, source, destination):
        try:
            stored = source.read_bytes()
        except OSError as exc:
            raise BackupError(f"Could not read credential {name}: {exc}") from exc
        marker = b"MSP-BACKUP-FERNET-1\n"
        if stored.startswith(marker):
            try:
                plaintext = self._fernet().decrypt(stored[len(marker):])
            except Exception as exc:
                raise BackupError(f"Could not decrypt credential {name} with the local Python key.") from exc
            self._write_protected(destination, plaintext)
            return
        if not shutil.which("systemd-creds"):
            raise BackupError(f"Credential {name} uses systemd-creds encryption; restore it on its original host or systemd 250+.")
        result = subprocess.run(["systemd-creds", "decrypt", f"--name={name}", str(source), str(destination)])
        if result.returncode:
            raise BackupError(f"Could not decrypt credential {name}.")
        os.chmod(destination, 0o600)

    def atomic_secret_file(self, path, content):
        path.write_bytes(content)
        os.chmod(path, 0o600)

    def remove_old_secret(self, path):
        if not path.is_file():
            return
        shred = shutil.which("shred")
        if shred:
            subprocess.run([shred, "-u", str(path)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

credential_store = CredentialStore()
runtime_dir = credential_store.runtime_dir
remove_runtime = credential_store.remove_runtime
encrypt_credential = credential_store.encrypt_credential
decrypt_credential = credential_store.decrypt_credential
atomic_secret_file = credential_store.atomic_secret_file
remove_old_secret = credential_store.remove_old_secret

class ConfigStore:
    """Manage config operations."""

    def save_json(self, path, data):
        ETC.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(ETC, 0o700)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chown(path, 0, 0)

    def read_json(self, path, default=None):
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            fail(f"Could not read {path}: {exc}")

    def load_config(self):
        config = read_json(CONFIG_FILE)
        if not config or not config.get("repository"):
            fail("Configure the repository first (menu item 1).")
        if not RESTIC_PASSWORD_CRED.is_file() or not RESTIC_BIN.is_file():
            fail("The encrypted password or local Restic binary was not found.")
        return config

config_store = ConfigStore()
save_json = config_store.save_json
read_json = config_store.read_json
load_config = config_store.load_config

