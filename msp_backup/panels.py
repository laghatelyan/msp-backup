"""Read known local control-panel database credentials without executing config files."""

from dataclasses import dataclass, field
from pathlib import Path
import shlex
import stat

from .common import BackupError


@dataclass(frozen=True)
class PanelDatabaseCredentials:
    panel: str
    host: str
    port: int
    user: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class ControlPanelInstallation:
    name: str
    root: Path

    @property
    def mysql_config(self):
        return self.root / "conf" / "mysql.conf"

    def read_mysql_credentials(self):
        path = self.mysql_config
        if path.is_symlink() or not path.is_file():
            raise BackupError(f"{self.name} MySQL configuration is not a regular local file: {path}")
        try:
            metadata = path.stat()
            mode = stat.S_IMODE(metadata.st_mode)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or mode & 0o022:
                raise BackupError(
                    f"{self.name} MySQL configuration must be a root-owned regular file "
                    f"with no group/world write permission (owner UID {metadata.st_uid}, "
                    f"mode {mode:04o}): {path}"
                )
            values = parse_shell_assignments(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise BackupError(f"Could not read {self.name} MySQL configuration: {exc}") from exc

        required = ("HOST", "USER", "PASSWORD")
        missing = [key for key in required if not values.get(key)]
        if missing:
            raise BackupError(f"{self.name} MySQL configuration is missing: {', '.join(missing)}")
        try:
            port = int(values.get("PORT") or "3306")
        except ValueError as exc:
            raise BackupError(f"{self.name} MySQL port is invalid.") from exc
        if not 1 <= port <= 65535:
            raise BackupError(f"{self.name} MySQL port is outside the valid range.")
        return PanelDatabaseCredentials(
            panel=self.name,
            host=values["HOST"],
            port=port,
            user=values["USER"],
            password=values["PASSWORD"],
        )


class ControlPanelDetector:
    """Detect supported panel layouts using their documented installation paths."""

    INSTALLATIONS = (
        ("HestiaCP", Path("/usr/local/hestia")),
        ("VestaCP", Path("/usr/local/vesta")),
    )

    def discover(self):
        found = []
        for name, root in self.INSTALLATIONS:
            if root.is_dir() and not root.is_symlink():
                panel = ControlPanelInstallation(name=name, root=root)
                if panel.mysql_config.is_file() and not panel.mysql_config.is_symlink():
                    found.append(panel)
        return found


def parse_shell_assignments(text):
    """Parse KEY=value config tokens safely; never source/execute the shell file."""
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    values = {}
    for token in lexer:
        key, separator, value = token.partition("=")
        if separator and key and key.replace("_", "").isalnum():
            values[key] = value
    return values
