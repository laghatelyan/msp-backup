"""Encrypted secret persistence and protected runtime files."""

import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile

from . import common
from .common import ETC, PYTHON_CREDENTIAL_KEY, BackupError, fail
from .system import secure_runfs


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
        if common.CREDENTIAL_BACKEND == "systemd":
            result = subprocess.run(
                ["systemd-creds", "encrypt", "--with-key=auto", f"--name={name}", "-", str(destination)],
                input=plaintext,
            )
            if result.returncode:
                fail(f"Could not encrypt secret {name}.")
            os.chown(destination, 0, 0)
            os.chmod(destination, 0o600)
            return
        if common.CREDENTIAL_BACKEND == "python":
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
