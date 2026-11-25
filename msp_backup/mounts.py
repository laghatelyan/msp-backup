"""Mount and unmount Restic snapshots through FUSE."""

from pathlib import Path
import os
import shutil
import subprocess
import time

from .common import ETC, INSTALL_PATH, fail, info
from .system import run_stream
from .restic import restic


class SnapshotMountManager:
    """Own FUSE mount lifecycle and its protected runtime metadata."""

    def __init__(self, restic_runner=restic, install_path=INSTALL_PATH, config_dir=ETC):
        self.restic = restic_runner
        self.install_path = Path(install_path)
        self.config_dir = Path(config_dir)

    def mount_worker(self, target):
        target_path = Path(target)
        if not target_path.is_absolute() or target_path == Path("/"):
            fail("Invalid mount directory.")
        self.restic("mount", str(target_path))

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
        log_path = self.config_dir / "mount.log"
        with log_path.open("a") as log:
            process = subprocess.Popen([str(self.install_path), "--mount-worker", str(target)], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        (self.config_dir / "mount.pid").write_text(str(process.pid))
        (self.config_dir / "mount.path").write_text(str(target))
        os.chmod(self.config_dir / "mount.pid", 0o600)
        os.chmod(self.config_dir / "mount.path", 0o600)
        time.sleep(2)
        if subprocess.run(["mountpoint", "-q", str(target)]).returncode:
            fail(f"Could not mount snapshots. Log: {log_path}")
        info(f"Snapshots mounted at {target}; use snapshots/ and latest/.")

    def unmount_snapshots(self):
        path_file = self.config_dir / "mount.path"
        if not path_file.exists():
            fail("No active mount point is recorded.")
        target = path_file.read_text().strip()
        if subprocess.run(["mountpoint", "-q", target]).returncode:
            fail(f"Directory is not mounted: {target}")
        command = shutil.which("fusermount3") or shutil.which("fusermount") or shutil.which("umount")
        args = [command, "-u", target] if Path(command).name.startswith("fuser") else [command, target]
        run_stream(args)
        for name in ("mount.path", "mount.pid"):
            (self.config_dir / name).unlink(missing_ok=True)
