"""Privileged OS integration and command execution."""

import logging
import os
from pathlib import Path
import re
import shutil
import subprocess

from . import common
from .common import (APP_SYSTEMD_DIR, LOGROTATE, LOGROTATE_CONFIG, LOG_DIR, LOG_FILE,
                     BackupError, fail, logger, ui, yes_no)


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
        if self.systemd_creds_supported():
            common.CREDENTIAL_BACKEND = "systemd"
            return common.CREDENTIAL_BACKEND
        try:
            from cryptography.fernet import Fernet  # noqa: F401
        except ImportError:
            common.CREDENTIAL_BACKEND = ""
            fail("Secret encryption needs systemd-creds (systemd 250+) or the Python package 'cryptography'.")
        common.CREDENTIAL_BACKEND = "python"
        return common.CREDENTIAL_BACKEND

    def detect_family(self):
        common.FAMILY = ""
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
            common.FAMILY = "debian"
        elif any(name in tokens for name in ("rhel", "fedora", "centos", "rocky", "almalinux", "ol")):
            common.FAMILY = "rhel"
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
        operation = label or Path(args[0]).name
        logger.info("Starting %s", operation)
        ui.status(f"Running {operation}…", "running")
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        assert process.stdout
        for line in process.stdout:
            print(line, end="", flush=True)
            logger.info(line.rstrip())
        code = process.wait()
        if code:
            ui.status(f"{operation} failed (exit code {code}).", "error")
        else:
            ui.status(f"{operation} finished.", "success")
        if code and check:
            raise BackupError(f"Command exited with code {code}: {operation}")
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
        if not yes_no("Install the missing system/Python dependencies now?", default=False):
            fail("Dependencies were not installed.")
        if common.FAMILY == "debian":
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
            if code and common.FAMILY == "rhel" and "fuse3" in command:
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
