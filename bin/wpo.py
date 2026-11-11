"""Entry point of the `wpo` command: `python -I -B bin/wpo.py <command> ...` (see SKILL.md)."""

import sys
from pathlib import Path

# -I leaves the script's directory off sys.path; the package sits one level up.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wpo.cli import entrypoint  # noqa: E402

if __name__ == "__main__":
    sys.exit(entrypoint())
