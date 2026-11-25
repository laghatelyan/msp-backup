"""Snapshot browsing and restore workflows."""

from pathlib import Path
from datetime import datetime
import json
import os
import posixpath
import shutil
import tempfile

from .common import BackupError, fail, ui, yes_no
from .credentials import credential_store
from .restic import restic


class SnapshotWorkflow:
    """Snapshot listing, selective restore, and restore verification."""

    def __init__(self, restic_runner=restic, user_interface=ui, credentials=credential_store, confirm=yes_no):
        self.restic = restic_runner
        self.ui = user_interface
        self.credentials = credentials
        self.confirm = confirm

    def load_snapshots(self):
        try:
            output = self.restic("snapshots", "--json", quiet=True, capture_output=True)
            snapshots = json.loads(output or "[]")
        except (BackupError, json.JSONDecodeError) as exc:
            fail(f"Could not read the snapshot list: {exc}")
        if snapshots is None:
            return []
        if not isinstance(snapshots, list):
            fail("Restic returned an invalid snapshot list.")
        snapshots = [snapshot for snapshot in snapshots if isinstance(snapshot, dict) and snapshot.get("id")]

        def snapshot_time(snapshot):
            try:
                value = snapshot.get("time", "").replace("Z", "+00:00")
                return datetime.fromisoformat(value).timestamp()
            except (TypeError, ValueError, OverflowError):
                return 0

        return sorted(snapshots, key=snapshot_time, reverse=True)

    @staticmethod
    def snapshot_label(snapshot):
        timestamp = str(snapshot.get("time") or "unknown time").replace("T", " ")[:19]
        identifier = snapshot.get("short_id") or str(snapshot.get("id", "unknown"))[:8]
        hostname = snapshot.get("hostname") or "unknown host"
        return f"{timestamp}  ·  {hostname}  ·  {identifier}"

    def select_snapshot(self):
        snapshots = self.load_snapshots()
        if not snapshots:
            self.ui.status("No snapshots are available in this repository.", "warning")
            return None
        options = []
        for index, snapshot in enumerate(snapshots, start=1):
            paths = ", ".join(str(path) for path in snapshot.get("paths", []))
            if len(paths) > 88:
                paths = paths[:85] + "…"
            options.append((str(index), self.snapshot_label(snapshot), paths))
        options.append(("0", "Back"))
        choice = self.ui.select("RESTORE · SELECT SNAPSHOT", options, "Newest snapshots are listed first. Use arrows and Enter.")
        if choice in (None, "0"):
            return None
        try:
            return snapshots[int(choice) - 1]
        except (ValueError, IndexError):
            fail("Invalid snapshot selection.")

    def list_snapshots(self):
        while True:
            snapshot = self.select_snapshot()
            if snapshot is None:
                return
            if not self.snapshot_restore_actions(snapshot):
                return

    def snapshot_restore_actions(self, snapshot):
        snapshot_id = snapshot.get("id")
        if not snapshot_id:
            fail("The selected snapshot has no ID.")
        paths = ", ".join(snapshot.get("paths", []))
        if len(paths) > 120:
            paths = paths[:117] + "…"
        while True:
            choice = self.ui.select("SNAPSHOT ACTIONS", [
                ("1", "Restore the entire snapshot"),
                ("2", "Choose individual files and folders"),
                ("3", "Choose another snapshot"),
                ("0", "Back to the main menu"),
            ], f"{self.snapshot_label(snapshot)}  ·  {paths}")
            if choice in (None, "0"):
                return False
            if choice == "3":
                return True
            if choice == "1":
                if self.restore_snapshot_to(snapshot_id):
                    self.ui.pause("Restore finished; press any key to return to snapshots")
                    return True
            elif choice == "2":
                if self.restore_selected_items(snapshot_id):
                    self.ui.pause("Restore finished; press any key to return to snapshots")
                    return True

    def restore_target(self):
        while True:
            value = input("Absolute destination directory (0 to cancel): ").strip()
            if value == "0":
                return None
            try:
                return self.validate_restore_target(value)
            except ValueError as exc:
                self.ui.status(f"{exc} Please try again.", "error")

    @staticmethod
    def validate_restore_target(value):
        target = Path(value.strip())
        if not target.is_absolute() or target == Path("/"):
            raise ValueError("Enter an absolute destination other than /.")
        return target

    def restore_snapshot_to(self, snapshot_id, include_paths=None):
        target = self.restore_target()
        if target is None:
            return False
        if include_paths:
            summary = f"{len(include_paths)} selected file/folder path(s)"
        else:
            summary = "the entire snapshot"
        self.ui.section("Restore plan")
        print(f"Snapshot: {snapshot_id}")
        print(f"Scope:    {summary}")
        print(f"Target:   {target}")
        if not self.confirm("Restored files may overwrite files in the target. Continue?", default=False):
            return False
        target.mkdir(parents=True, exist_ok=True)
        args = ["restore", snapshot_id, "--target", str(target)]
        if include_paths:
            patterns = [self.restic_include_pattern(path) for path in include_paths]
            estimated_size = sum(len(pattern.encode("utf-8")) + 32 for pattern in patterns)
            if estimated_size > 48000:
                if any("\n" in path or "\r" in path or path != path.strip() for path in include_paths):
                    fail("Too many selected paths include line breaks or edge spaces. Reduce the selection and retry.")
                work = self.credentials.runtime_dir()
                try:
                    include_file = work / "restore-includes.txt"
                    file_patterns = [self.restic_include_pattern(path, include_file=True) for path in include_paths]
                    include_file.write_text("\n".join(file_patterns) + "\n", encoding="utf-8")
                    os.chmod(include_file, 0o600)
                    args.extend(["--include-file", str(include_file)])
                    self.restic(*args)
                finally:
                    self.credentials.remove_runtime(work)
                return True
            for pattern in patterns:
                args.extend(["--include", pattern])
        self.restic(*args)
        return True

    @staticmethod
    def restic_include_pattern(path, *, include_file=False):
        # Escape glob syntax so the exact path selected by the user is restored.
        pattern = path.replace("\\", "\\\\").replace("[", "\\[").replace("*", "\\*").replace("?", "\\?")
        if pattern.startswith("!"):
            pattern = "\\" + pattern
        if include_file:
            pattern = pattern.replace("$", "$$")
            if pattern.startswith("#"):
                pattern = "\\" + pattern
        return pattern

    @staticmethod
    def format_size(size):
        try:
            size = int(size)
        except (TypeError, ValueError):
            return ""
        units = ("B", "KiB", "MiB", "GiB", "TiB")
        value = float(size)
        for unit in units:
            if value < 1024 or unit == units[-1]:
                return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
            value /= 1024

    def load_snapshot_nodes(self, snapshot_id):
        output = self.restic("ls", "--json", snapshot_id, quiet=True, capture_output=True)
        nodes = []
        for line in output.splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("struct_type", record.get("message_type")) != "node":
                continue
            path = record.get("path") or record.get("name")
            if not isinstance(path, str) or not path:
                continue
            if not path.startswith("/"):
                path = "/" + path
            path = posixpath.normpath(path)
            if path == "/":
                continue
            record["path"] = path
            nodes.append(record)
        nodes.sort(key=lambda node: (node["path"].casefold(), node.get("type", "")))
        if not nodes:
            self.ui.status("This snapshot contains no browsable file entries.", "warning")
        return nodes

    def test_restore_file(self):
        snapshot = self.select_snapshot()
        if snapshot is None:
            return
        nodes = self.load_snapshot_nodes(snapshot["id"])
        files = [node for node in nodes if node.get("type") == "file"]
        if not files:
            self.ui.status("The selected snapshot contains no regular files to test.", "warning")
            return
        options = []
        for index, node in enumerate(files, start=1):
            path = node["path"].replace("\n", "\\n").replace("\r", "\\r")
            size = self.format_size(node.get("size")) or "size unknown"
            options.append((str(index), path, size))
        options.append(("0", "Cancel"))
        choice = self.ui.select("TEST RESTORE · SELECT FILE", options,
                           f"{self.snapshot_label(snapshot)} · restores one file, then removes the temporary copy")
        if choice in (None, "0"):
            return
        try:
            node = files[int(choice) - 1]
            expected_size = int(node["size"])
        except (ValueError, IndexError, KeyError, TypeError):
            fail("The selected file has no valid size metadata; choose another file.")
        required = expected_size + max(1024 * 1024, min(16 * 1024 * 1024, expected_size // 10))
        while True:
            base_text = input("Existing directory for temporary restore test (0 to cancel): ").strip()
            if base_text == "0":
                return
            base = Path(base_text)
            if not base.is_absolute() or not base.is_dir():
                self.ui.status("Enter an existing absolute directory; no directory will be created for you.", "error")
                continue
            try:
                free = shutil.disk_usage(base).free
            except OSError as exc:
                self.ui.status(f"Could not check free space: {exc}", "error")
                continue
            if free < required:
                self.ui.status(f"Not enough free space: {self.format_size(free)} available; about {self.format_size(required)} recommended. Choose another directory or cancel.", "warning")
                continue
            if not os.access(base, os.W_OK | os.X_OK):
                self.ui.status("The selected directory is not writable by root.", "error")
                continue
            break
        self.ui.section("Restore test plan")
        print(f"Snapshot: {self.snapshot_label(snapshot)}")
        print(f"File:     {node['path']}")
        print(f"Size:     {self.format_size(expected_size)}")
        print(f"Free:     {self.format_size(free)}")
        print("The temporary copy is removed automatically after the check.")
        if not self.confirm("Run this restore test?", default=False):
            return
        temporary = Path(tempfile.mkdtemp(prefix="msp-backup-restore-test-", dir=base))
        try:
            os.chmod(temporary, 0o700)
            self.restic("restore", snapshot["id"], "--target", str(temporary),
                   "--include", self.restic_include_pattern(node["path"]))
            restored = temporary / node["path"].lstrip("/")
            if not restored.is_file():
                fail("Restic completed, but the selected file was not found in the temporary restore.")
            actual_size = restored.stat().st_size
            if actual_size != expected_size:
                fail(f"Restored file size differs from snapshot metadata ({actual_size} vs {expected_size} bytes).")
            self.ui.status(f"Restore test passed: {node['path']} ({self.format_size(actual_size)}).", "success")
        finally:
            try:
                shutil.rmtree(temporary)
            except OSError as exc:
                self.ui.status(f"Could not remove the temporary restore directory {temporary}: {exc}", "warning")

    def restore_selected_items(self, snapshot_id):
        nodes = self.load_snapshot_nodes(snapshot_id)
        if not nodes:
            return False
        selected = set()
        while True:
            options = []
            for index, node in enumerate(nodes, start=1):
                path = node["path"]
                kind = "DIR" if node.get("type") == "dir" else str(node.get("type", "FILE")).upper()
                mark = "✓" if path in selected else " "
                size = self.format_size(node.get("size"))
                detail = f"{kind} · {size}" if size else kind
                display_path = path.replace("\n", "\\n").replace("\r", "\\r")
                options.append((str(index), f"[{mark}] {display_path}", detail))
            options.extend([
                ("d", f"Restore selected items ({len(selected)} selected)"),
                ("c", "Clear selection"),
                ("0", "Cancel and return"),
            ])
            choice = self.ui.select("SELECT RESTORE ITEMS", options, "Enter toggles a file/folder. A selected folder restores its contents.")
            if choice in (None, "0"):
                return False
            if choice == "c":
                selected.clear()
                continue
            if choice == "d":
                if not selected:
                    self.ui.status("Select at least one file or folder first.", "warning")
                    continue
                paths = self.collapse_selected_paths(selected)
                return self.restore_snapshot_to(snapshot_id, paths)
            if choice.isdecimal():
                index = int(choice) - 1
                if 0 <= index < len(nodes):
                    path = nodes[index]["path"]
                    if path in selected:
                        selected.remove(path)
                    else:
                        selected.add(path)

    @staticmethod
    def collapse_selected_paths(paths):
        result = []
        for path in sorted(paths, key=lambda value: (value.count("/"), value)):
            if not any(parent == "/" or path.startswith(parent.rstrip("/") + "/") for parent in result):
                result.append(path)
        return result
