"""Release checks and atomic self-updates for the installed executable."""

from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import hashlib
import json
import os
import platform
import re
import shutil
import struct
import sys
import tempfile

from pathlib import Path

from .common import APP_SYSTEMD_DIR, BackupError, INSTALL_PATH, UPDATE_CONFIG_FILE, VERSION, fail, info, input_default, link_systemd_unit, logger, yes_no
from .configuration import read_json, save_json
from .system import run_stream

MAX_MANIFEST_SIZE = 1024 * 1024
MAX_BINARY_SIZE = 128 * 1024 * 1024
ASSET_ARCHES = {"x86_64": ("linux-x86_64", 62), "amd64": ("linux-x86_64", 62),
                "aarch64": ("linux-aarch64", 183), "arm64": ("linux-aarch64", 183)}


class UpdateManager:
    """Own update configuration and operational workflows."""

    def configure_update_url(self):
        current = read_json(UPDATE_CONFIG_FILE, {})
        print("Configure the HTTPS URL of the release manifest.json.")
        url = input_default("Release manifest URL", current.get("manifest_url", ""))
        if not url.startswith("https://"):
            fail("Updates require an HTTPS URL.")
        save_json(UPDATE_CONFIG_FILE, {"manifest_url": url})
        info("Update URL saved.")

    def parse_version(self, value):
        match = re.fullmatch(r"\s*v?(\d+(?:\.\d+){1,3})(?:[-+].*)?\s*", value)
        if not match:
            raise BackupError(f"Invalid version: {value}")
        parts = [int(part) for part in match.group(1).split(".")]
        return tuple((parts + [0, 0, 0, 0])[:4])

    @staticmethod
    def _download(url, limit):
        if not isinstance(url, str) or not url.startswith("https://"):
            raise BackupError("Release manifest and binary URLs must use HTTPS.")
        request = Request(url, headers={"User-Agent": f"MSP-Backup/{VERSION}"})
        with urlopen(request, timeout=60) as response:
            if not response.geturl().startswith("https://"):
                raise BackupError("The download redirected away from HTTPS.")
            length = response.headers.get("Content-Length")
            if length and int(length) > limit:
                raise BackupError("The download exceeds its size limit.")
            data = response.read(limit + 1)
        if len(data) > limit:
            raise BackupError("The download exceeds its size limit.")
        return data

    @staticmethod
    def _platform_asset():
        arch = ASSET_ARCHES.get(platform.machine().lower())
        if not arch:
            raise BackupError(f"Unsupported Linux architecture: {platform.machine()}")
        return arch

    @staticmethod
    def _validate_elf(data, expected_machine):
        if len(data) < 20 or data[:4] != b"\x7fELF" or data[4] != 2 or data[5] not in (1, 2):
            raise BackupError("The downloaded asset is not a 64-bit Linux executable.")
        byte_order = "<" if data[5] == 1 else ">"
        machine = struct.unpack_from(byte_order + "H", data, 18)[0]
        if machine != expected_machine:
            raise BackupError("The binary architecture does not match this server.")

    def check_script_update(self, install=False, quiet=False):
        config = read_json(UPDATE_CONFIG_FILE, {})
        manifest_url = config.get("manifest_url")
        if not manifest_url:
            if not quiet:
                print("Release manifest URL is not configured (Settings → Update URL).")
            return False
        try:
            manifest = json.loads(self._download(manifest_url, MAX_MANIFEST_SIZE).decode("utf-8"))
            latest = manifest["version"]
            assets = manifest["assets"]
            if not isinstance(assets, dict):
                raise BackupError("The release manifest has no valid assets map.")
            asset_name, expected_machine = self._platform_asset()
            asset = assets.get(asset_name)
            if not isinstance(asset, dict):
                raise BackupError(f"This release has no binary for {asset_name}.")
            binary_url = asset["url"]
            expected_hash = asset["sha256"].lower()
            if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
                raise BackupError("The release manifest contains an invalid SHA-256.")
            if self.parse_version(latest) <= self.parse_version(VERSION):
                if not quiet:
                    info(f"MSP Backup is up to date: {VERSION}.")
                return False
            print(f"MSP Backup {latest} is available (installed: {VERSION}).")
            if not install and not yes_no("Download, verify, and install this release?"):
                return False
            binary = self._download(binary_url, MAX_BINARY_SIZE)
            if hashlib.sha256(binary).hexdigest() != expected_hash:
                raise BackupError("The binary SHA-256 does not match the release manifest.")
            self._validate_elf(binary, expected_machine)
            self._install_binary(binary)
            info(f"MSP Backup was updated to {latest}; the new version will run on the next start.")
            return True
        except (URLError, HTTPError, OSError, ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
            logger.exception("Update check failed")
            print(f"Could not check for or install the update: {exc}", file=sys.stderr)
            return False
        except BackupError as exc:
            logger.exception("Update check failed")
            print(f"Could not check for or install the update: {exc}", file=sys.stderr)
            return False

    @staticmethod
    def _install_binary(binary):
        INSTALL_PATH.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        if INSTALL_PATH.is_symlink():
            raise BackupError(f"Refusing to replace a symlink at {INSTALL_PATH}.")
        fd, staged_name = tempfile.mkstemp(prefix=".msp-backup-update-", dir=INSTALL_PATH.parent)
        staged = Path(staged_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(binary)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(staged, 0o755)
            os.chown(staged, 0, 0)
            backup = INSTALL_PATH.with_name("msp-backup.previous")
            if INSTALL_PATH.exists():
                if not INSTALL_PATH.is_file():
                    raise BackupError(f"Installed application is not a regular file: {INSTALL_PATH}.")
                backup_tmp = backup.with_name(".msp-backup-previous.tmp")
                shutil.copy2(INSTALL_PATH, backup_tmp)
                os.chown(backup_tmp, 0, 0)
                os.chmod(backup_tmp, 0o755)
                os.replace(backup_tmp, backup)
            os.replace(staged, INSTALL_PATH)
            os.chown(INSTALL_PATH, 0, 0)
            os.chmod(INSTALL_PATH, 0o755)
        finally:
            staged.unlink(missing_ok=True)

    def install_script_update_timer(self):
        if not read_json(UPDATE_CONFIG_FILE, {}).get("manifest_url"):
            self.configure_update_url()
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
        APP_SYSTEMD_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
        for name, content in (("msp-backup-update.service", service), ("msp-backup-update.timer", timer)):
            path = APP_SYSTEMD_DIR / name
            path.write_text(content)
            os.chown(path, 0, 0)
            os.chmod(path, 0o644)
            link_systemd_unit(path)
        run_stream(["systemctl", "daemon-reload"])
        run_stream(["systemctl", "enable", "--now", "msp-backup-update.timer"])


update_manager = UpdateManager()
configure_update_url = update_manager.configure_update_url
parse_version = update_manager.parse_version
check_script_update = update_manager.check_script_update
install_script_update_timer = update_manager.install_script_update_timer
