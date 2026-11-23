from urllib.error import HTTPError, URLError
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile

from .restic import download
from pathlib import Path
from .common import APP_SYSTEMD_DIR, BackupError, INSTALL_PATH, PACKAGE_DIR, UPDATE_CONFIG_FILE, VERSION, fail, info, input_default, link_systemd_unit, logger, read_json, run_stream, save_json, yes_no

class UpdateManager:
    """Own updates configuration and operational workflows."""

    def configure_update_url(self):
        current = read_json(UPDATE_CONFIG_FILE, {})
        print("The manifest must be JSON with version, url, and sha256 fields.")
        url = input_default("URL manifest", current.get("manifest_url", ""))
        if not url.startswith("https://"):
            fail("Updates require an HTTPS URL.")
        save_json(UPDATE_CONFIG_FILE, {"manifest_url": url})
        info("Update check URL saved.")

    def parse_version(self, value):
        match = re.fullmatch(r"\s*v?(\d+(?:\.\d+){1,3})(?:[-+].*)?\s*", value)
        if not match:
            raise BackupError(f"Invalid version: {value}")
        parts = [int(part) for part in match.group(1).split(".")]
        return tuple((parts + [0, 0, 0, 0])[:4])

    def check_script_update(self, install=False, quiet=False):
        config = read_json(UPDATE_CONFIG_FILE, {})
        manifest_url = config.get("manifest_url")
        if not manifest_url:
            if not quiet:
                print("Update URL is not configured (menu item 15).")
            return False
        try:
            manifest = json.loads(download(manifest_url).decode("utf-8"))
            latest = manifest["version"]
            package_url = manifest["url"]
            expected = manifest["sha256"].lower()
            if not package_url.startswith("https://") or not re.fullmatch(r"[0-9a-f]{64}", expected):
                fail("The manifest contains an insecure package URL or invalid SHA-256.")
            if parse_version(latest) <= parse_version(VERSION):
                if not quiet:
                    info(f"MSP Backup is up to date: {VERSION}.")
                return False
            print(f"MSP Backup {latest} is available (installed: {VERSION}).")
            if not install:
                if not yes_no("Download, verify the SHA-256, and install the update?"):
                    return False
            archive = download(package_url)
            if hashlib.sha256(archive).hexdigest() != expected:
                fail("The package SHA-256 does not match the manifest.")
            if len(archive) > 20 * 1024 * 1024:
                fail("The update package exceeds the 20 MiB size limit.")
            parent = PACKAGE_DIR.parent
            stage_root = Path(tempfile.mkdtemp(prefix="msp-backup-update.", dir=parent))
            staged_package = stage_root / "msp_backup"
            staged_package.mkdir(mode=0o755)
            total_size = 0
            try:
                with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                    entries = bundle.infolist()
                    if len(entries) > 32:
                        fail("The update package contains too many entries.")
                    written = set()
                    for entry in entries:
                        member = Path(entry.filename)
                        if member.is_dir():
                            continue
                        if len(member.parts) != 2 or member.parts[0] != "msp_backup" or member.suffix != ".py":
                            fail(f"Unexpected path in update package: {entry.filename}")
                        if member.name in written:
                            fail(f"Duplicate module in update package: {member.name}")
                        written.add(member.name)
                        if entry.file_size > 2 * 1024 * 1024:
                            fail("An update module exceeds the 2 MiB size limit.")
                        total_size += entry.file_size
                        if total_size > 16 * 1024 * 1024:
                            fail("The uncompressed update package exceeds the 16 MiB size limit.")
                        unix_mode = entry.external_attr >> 16
                        if unix_mode and (unix_mode & 0o170000) == 0o120000:
                            fail("Symbolic links are not allowed in update packages.")
                        destination = staged_package / member.name
                        destination.write_bytes(bundle.read(entry))
                version_file = staged_package / "version.py"
                if not version_file.is_file():
                    fail("The update package does not contain version.py.")
                text = version_file.read_text(encoding="utf-8")
                match = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', text, re.M)
                if not match or match.group(1) != latest:
                    fail("The package version does not match the manifest.")
                for module in staged_package.glob("*.py"):
                    os.chmod(module, 0o644)
                    os.chown(module, 0, 0)
                previous = PACKAGE_DIR.with_name("msp_backup.previous")
                if previous.exists():
                    shutil.rmtree(previous)
                os.replace(PACKAGE_DIR, previous)
                try:
                    os.replace(staged_package, PACKAGE_DIR)
                except OSError:
                    os.replace(previous, PACKAGE_DIR)
                    raise
                shutil.rmtree(previous)
                info(f"MSP Backup was updated to {latest}; the new version will run on the next start.")
            finally:
                shutil.rmtree(stage_root, ignore_errors=True)
            return True
        except (URLError, HTTPError, OSError, ValueError, KeyError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
            logger.exception("Update check failed")
            print(f"Could not check for or install the update: {exc}", file=sys.stderr)
            return False

    def install_script_update_timer(self):
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
