"""wp-orchestration: run many coding agents in parallel on one git repository.

Work is cut into work packages (WP files); each is built in its own git worktree by a tier-routed
agent session, verified, reviewed and fast-forwarded into the main branch. The package holds the
machinery; a project holds only `.orchestration/config.json` and its WP files. See SKILL.md.
"""

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = PACKAGE_ROOT.parent
SKILL_ROOT = SOURCE_ROOT if (SOURCE_ROOT / "SKILL.md").is_file() else PACKAGE_ROOT / "resources"
ENTRY_POINT = SKILL_ROOT / "bin" / "wpo.py"

# The tooling runs from every worktree's working directory; bytecode caches would appear there.
sys.dont_write_bytecode = True
