# Changelog

## 1.14.3

- Accept the default HestiaCP `root:root` mode `0660` for read-only MySQL credential import; continue rejecting world-writable config files.

## 1.14.2

- Kept panel credential import errors visible so the operator can read the security-check failure before choosing another database source.

## 1.14.1

- Fixed HestiaCP/VestaCP credential import error handling and added owner/mode details when the panel config fails security checks.
- Kept database setup in its retryable source-selection flow when panel credentials cannot be imported.

## 1.14.0

- Added GitHub Actions release builds for standalone Linux x86_64 and aarch64 binaries.
- Built against a pinned relocatable CPython distribution compatible with the PyInstaller shared-library requirement.
- Replaced Python package ZIP self-updates with SHA-256 verified, atomic executable updates from a release manifest.
- Added first-run installation support for the standalone binary.

## 1.13.0

- Added safe discovery of HestiaCP and VestaCP MySQL credentials from their local panel configuration.
- Split terminal interaction, database setup, backup path management, FUSE mounts, credentials, system operations, and config storage into dedicated modules.
- Fixed arrow-key input parsing so held/repeated navigation events are processed one at a time.

## 1.12.0

- Extracted snapshot browsing, selective restore, and restore verification into a dedicated workflow component.
- Made manual backups run database dumps, retention, cleanup, and notifications as one complete operation.
- Added cleanup of staged database dumps to the systemd failure handler.
- Consolidated successful scheduled backup finalization into one ordered action.

## 1.11.0

- Added interactive backup path management with add, replace, remove, validation, and save/cancel.
- Kept the database dump path managed while database backups are configured.

## 1.10.0

- Replaced remaining categorical prompts with arrow-key selections and retryable validation.
- Added automatic discovery and separate backup files for every MySQL/MariaDB and PostgreSQL database.
- Added separate MySQL account/grant and PostgreSQL global-role exports.
- Added SQLite file or directory discovery with one online backup per database file.
- Added `/run` capacity checks before staging database dumps and cleanup of partial dumps.

## 1.9.0

- Added bounded repository data verification (1 MiB–1 GiB) without restoring files locally.
- Added a one-file restore drill with free-space preflight and automatic temporary-file cleanup.
- Added protected last-run reports and richer SMTP success/failure summaries.

## 1.8.0

- Removed duplicate snapshot restore and repository settings entries from the main menu.
- Grouped repository checks, snapshot mount/unmount, and retention under Snapshot tools.
- Grouped daily backup and weekly application update schedules under Schedules.
- Simplified the main menu to ten actions and updated the update URL hint.

## 1.7.0

- Added an interactive snapshot browser with newest snapshots first.
- Added restore of a whole snapshot or selected files and folders from the chosen snapshot.
- Added a persistent restore plan and confirmation before writing restored files.
- Added paging for long terminal menus and a clear-selection action in the item picker.

## 1.6.0

- Added arrow-key navigation for interactive terminal menus and confirmation dialogs.
- Kept command output, including snapshot lists, visible until a key is pressed.
- Added numbered-menu fallback for non-interactive terminals.

## 1.5.1

- Set a private Restic cache directory under `/opt/msp-backup/` for systemd runs without `HOME`.

## 1.5.0

- Made repository/S3 configuration accessible from item 16 on both first run and reconfiguration.
- Added secure automatic Restic repository password generation and prompt-level validation retries.
- Added an ANSI terminal interface with versioned menus and clearer operation states.

## 1.4.0

- Added guided Amazon S3 and S3-compatible repository setup.
- Encrypted S3 access keys and session tokens; provided a menu action to change repository settings later.
- Passed S3 credentials to Restic only from a protected temporary runtime directory.

## 1.3.0

- Relocated the application, Restic binary, configuration, credentials, and logs under `/opt/msp-backup/`.
- Kept systemd and logrotate integration files under `/opt` and linked them into their system search paths.
- Added migration of existing configuration, credentials, logs, Restic binary, and timers.

## 1.2.0

- Added a Python Fernet credential backend for systems without systemd-creds 250+.
- Kept systemd-creds as the preferred backend when available and made cryptography an optional dependency.

## 1.1.5

- Added clear menu/action/result separators.
- Kept the menu active and reported recoverable action errors without exiting.

## 1.1.4

- Kept the interactive menu open after each action; `0` exits the menu.
- Displayed the backup and update timers in one status query.

## 1.1.3

- Prevented startup errors from being printed multiple times before logging is configured.

## 1.1.2

- Initialize and inject the Restic manager in the application composition root.

## 1.1.1

- Finalized package installation and update handling for the modular release.
- Added the project architecture overview to the Russian operator guide.

## 1.1.0

- Refactored the utility into the `msp_backup` Python package with service managers for system operations, configuration, credentials, Restic, databases, notifications, and updates.
- Kept `msp-backup.py` as a small launcher and added `python -m msp_backup` support.
- Changed script self-updates to verify and install a complete package ZIP.
- Added project metadata and Git version tagging.
