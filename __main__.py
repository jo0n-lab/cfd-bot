"""Allow `python -m cfd_bot` from the parent telegram directory."""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
os.chdir(PROJECT_ROOT)

from cfd_bot.cfd_bot.cli import main  # noqa: E402

raise SystemExit(main())
