"""Entry point: ``python -m maintdoc``."""

import sys

from maintdoc.cli import main

if __name__ == "__main__":  # required for multiprocessing spawn on Windows
    sys.exit(main())
