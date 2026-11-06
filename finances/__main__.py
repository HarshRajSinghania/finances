"""``python -m finances`` — the same front door as the ``finances`` command."""
import sys

from finances.cli import main

sys.exit(main())
