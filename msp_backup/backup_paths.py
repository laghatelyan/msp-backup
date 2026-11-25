"""Backup source path editing and validation."""

import os
from pathlib import Path

from .common import CONFIG_FILE, DB_CONFIG_FILE, DB_DUMP_DIR, LEGACY_DB_DUMP_DIR, PATHS_FILE, fail, info, ui
from .configuration import read_json


class BackupPathManager:
    """Manage configured file and directory sources for Restic jobs."""

    def __init__(self, path_file=PATHS_FILE, db_config_file=DB_CONFIG_FILE,
                 managed_db_path=DB_DUMP_DIR, config_file=CONFIG_FILE, user_interface=ui):
        self.path_file = Path(path_file)
        self.db_config_file = Path(db_config_file)
        self.managed_db_path = str(managed_db_path)
        self.config_file = Path(config_file)
        self.ui = user_interface

    def edit_backup_paths(self):
        if not self.path_file.is_file():
            fail("Backup paths are not configured yet. Run the initial setup first.")
        paths = list(dict.fromkeys(line.strip() for line in self.path_file.read_text().splitlines() if line.strip()))
        database_configured = bool(read_json(self.db_config_file, {}))
        managed_db_path = str(self.managed_db_path)
        if database_configured:
            old_managed_paths = {str(LEGACY_DB_DUMP_DIR), "/var/lib/msp-backup/db-dumps"}
            paths = list(dict.fromkeys(managed_db_path if path in old_managed_paths else path for path in paths))
        if database_configured and managed_db_path not in paths:
            paths.append(managed_db_path)
        while True:
            options = []
            for index, value in enumerate(paths):
                path = Path(value)
                kind = "created automatically" if database_configured and value == managed_db_path and not path.exists() else "directory" if path.is_dir() else "file" if path.is_file() else "missing"
                managed = " · managed by database backups" if database_configured and value == managed_db_path else ""
                options.append((f"p{index}", value, f"{kind}{managed}"))
            options.extend([
                ("a", "Add a file or directory"),
                ("s", "Save changes"),
                ("0", "Cancel without saving"),
            ])
            choice = self.ui.select("BACKUP PATHS", options,
                               "Select a path to replace or remove it. Database dump path is managed automatically.")
            if choice in (None, "0"):
                return
            if choice == "a":
                value = self.prompt_backup_path()
                if value and value not in paths:
                    paths.append(value)
                elif value:
                    self.ui.status("That path is already in the backup list.", "warning")
                continue
            if choice == "s":
                if not paths:
                    self.ui.status("At least one backup path is required.", "error")
                    continue
                try:
                    self.validate_backup_paths(paths, managed_db_path if database_configured else None)
                except ValueError as exc:
                    self.ui.status(f"{exc} Remove or replace the invalid entry before saving.", "error")
                    continue
                temporary = self.path_file.with_suffix(".txt.tmp")
                temporary.write_text("\n".join(paths) + "\n", encoding="utf-8")
                os.chown(temporary, 0, 0)
                os.chmod(temporary, 0o600)
                os.replace(temporary, self.path_file)
                os.chown(self.path_file, 0, 0)
                info(f"Backup paths saved ({len(paths)} entries).")
                return
            if choice.startswith("p"):
                index = int(choice[1:])
                if 0 <= index < len(paths):
                    self.edit_backup_path(paths, index, managed_db_path if database_configured else None)

    def prompt_backup_path(self):
        while True:
            value = input("Absolute path to an existing file or directory (0 to cancel): ").strip()
            if value == "0":
                return None
            path = Path(value)
            if "\n" not in value and "\r" not in value and path.is_absolute() and path.exists():
                return str(path.resolve())
            self.ui.status("Enter an existing absolute file or directory path.", "error")

    def validate_backup_paths(self, paths, managed_db_path=None):
        for value in paths:
            path = Path(value)
            if not path.is_absolute() or (value != managed_db_path and not path.exists()):
                raise ValueError(f"Backup path does not exist or is not absolute: {value}")
        config = read_json(self.config_file, {})
        repository = config.get("repository", "")
        if repository.startswith("/") and config.get("backend") != "s3":
            local_repo = Path(os.path.realpath(repository))
            for value in paths:
                source = Path(os.path.realpath(value))
                if source == Path("/") or source == local_repo or source.is_relative_to(local_repo) or local_repo.is_relative_to(source):
                    raise ValueError(f"Backup path {source} overlaps the local repository {local_repo}.")

    def edit_backup_path(self, paths, index, managed_db_path=None):
        value = paths[index]
        if value == managed_db_path:
            self.ui.status("The database dump path is managed automatically while database backups are configured.", "warning")
            return
        choice = self.ui.select("BACKUP PATH", [
            ("1", "Replace this path"),
            ("2", "Remove this path"),
            ("0", "Back"),
        ], value)
        if choice == "1":
            replacement = self.prompt_backup_path()
            if replacement:
                if replacement in paths and replacement != value:
                    self.ui.status("That path is already in the backup list.", "warning")
                else:
                    paths[index] = replacement
        elif choice == "2":
            if len(paths) == 1:
                self.ui.status("At least one backup path must remain.", "warning")
                return
            paths.pop(index)
