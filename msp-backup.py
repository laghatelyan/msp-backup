#!/usr/bin/env python3
"""MSP Backup command-line launcher."""

import sys
from msp_backup.application import main
from msp_backup.common import BackupError, logger

if __name__ == "__main__":
    try:
        sys.exit(main())
    except BackupError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        logger.error("%s", exc)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nOperation cancelled.", file=sys.stderr)
        sys.exit(130)
