"""Run MSP Backup with ``python -m msp_backup``."""

import sys

from .application import main
from .common import BackupError, logger


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BackupError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        if logger.handlers:
            logger.error("%s", exc)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nOperation cancelled.", file=sys.stderr)
        sys.exit(130)
