# Changelog

## 1.1.0

- Refactored the utility into the `msp_backup` Python package with service managers for system operations, configuration, credentials, Restic, databases, notifications, and updates.
- Kept `msp-backup.py` as a small launcher and added `python -m msp_backup` support.
- Changed script self-updates to verify and install a complete package ZIP.
- Added project metadata and Git version tagging.
