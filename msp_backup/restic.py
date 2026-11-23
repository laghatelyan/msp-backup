from urllib.error import HTTPError, URLError
from pathlib import Path
from urllib.request import Request, urlopen
import bz2
import hashlib
import json
import os
import platform
import re
import subprocess
import sys

from .common import BackupError, RESTIC_API, RESTIC_BIN, RESTIC_PASSWORD_CRED, S3_CREDENTIALS_CRED, VERSION, decrypt_credential, fail, info, load_config, logger, remove_runtime, run_stream, runtime_dir

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
            if config["repository"].startswith("s3:") and config.get("backend") != "s3":
                fail("This S3 repository has no saved credentials. Configure repository / S3 credentials from the menu.")
            if config.get("backend") == "s3":
                if not S3_CREDENTIALS_CRED.is_file():
                    fail("S3 credentials are missing. Configure S3 storage from the menu.")
                s3_credentials = work / "s3-credentials.json"
                decrypt_credential("msp-backup-s3", S3_CREDENTIALS_CRED, s3_credentials)
                try:
                    credentials = json.loads(s3_credentials.read_text())
                    access_key_id = credentials["access_key_id"]
                    secret_access_key = credentials["secret_access_key"]
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    raise BackupError(f"The encrypted S3 credential is invalid: {exc}") from exc
                env["AWS_ACCESS_KEY_ID"] = access_key_id
                env["AWS_SECRET_ACCESS_KEY"] = secret_access_key
                if credentials.get("session_token"):
                    env["AWS_SESSION_TOKEN"] = credentials["session_token"]
                else:
                    env.pop("AWS_SESSION_TOKEN", None)
                if config.get("region"):
                    env["AWS_DEFAULT_REGION"] = config["region"]
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

class ResticInstaller:
    """Install, update, and inspect the Restic executable."""

    def download(self, url, timeout=60):
        request = Request(url, headers={"User-Agent": f"MSP-Backup/{VERSION}"})
        with urlopen(request, timeout=timeout) as response:
            return response.read()

    def restic_arch(self):
        arch = platform.machine().lower()
        return {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64", "i386": "386", "i686": "386", "armv7l": "arm"}.get(arch) or fail(f"Unsupported architecture: {arch}")

    def install_restic_latest(self):
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

    def ensure_restic(self):
        if RESTIC_BIN.exists():
            run_stream([str(RESTIC_BIN), "version"], label="Restic version")
        else:
            install_restic_latest()

    def update_restic(self):
        if not RESTIC_BIN.exists():
            install_restic_latest()
            return
        run_stream([str(RESTIC_BIN), "self-update", "--output", str(RESTIC_BIN.with_suffix(".new"))], label="Update Restic")
        replacement = RESTIC_BIN.with_suffix(".new")
        if replacement.exists():
            os.chmod(replacement, 0o755)
            os.replace(replacement, RESTIC_BIN)
            info("Restic updated.")

restic_installer = ResticInstaller()
download = restic_installer.download
restic_arch = restic_installer.restic_arch
install_restic_latest = restic_installer.install_restic_latest
ensure_restic = restic_installer.ensure_restic
update_restic = restic_installer.update_restic
