"""Protected JSON configuration storage."""

import json
import os

from .common import ETC, CONFIG_FILE, RESTIC_BIN, RESTIC_PASSWORD_CRED, fail


class ConfigStore:
    """Manage config operations."""

    def save_json(self, path, data):
        ETC.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(ETC, 0o700)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chown(path, 0, 0)

    def read_json(self, path, default=None):
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            fail(f"Could not read {path}: {exc}")

    def load_config(self):
        config = self.read_json(CONFIG_FILE)
        if not config or not config.get("repository"):
            fail("Configure the repository first (menu item 1).")
        if not RESTIC_PASSWORD_CRED.is_file() or not RESTIC_BIN.is_file():
            fail("The encrypted password or local Restic binary was not found.")
        return config


config_store = ConfigStore()
save_json = config_store.save_json
read_json = config_store.read_json
load_config = config_store.load_config
