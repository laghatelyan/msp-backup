# Changelog

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
