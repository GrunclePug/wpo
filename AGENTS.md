# WPO contributor instructions

Read `SKILL.md` and the backend-specific reference before changing orchestration.
Personal agent rules add to this repository contract.

## Layout

- `wpo/`: CLI, scheduler, verification, review, routing, backend, and dashboard server.
- `wpo/t3.py`: durable native-coordinator bridge; `references/t3-bridge.md` is its operator contract.
- `dashboard/`: local dashboard assets; native task identities are additive.
- `models/`, `templates/`, `references/`: bundled runtime resources and skill guidance.
- `tests/`: unit/native/configuration regressions and scripted scratch integration tests.
- `tools/check_install.py`: installed-wheel acceptance outside the source checkout.

## Contracts

Preserve existing WP/config/ledger/event/log formats and held/paused state.
T3 owns native worktrees; never create a child's checkout in the shell or remove
T3-managed directories. New reviews are fresh delegated tasks. Honor completion
notifications and returned `next`; never poll a running native task.

Keep machine paths, provider/model aliases, deployment details, and quota policy
in configuration. Keep secrets and account state outside the repository. No live
project, provider session, model call, or lane may start merely to run tests.

Both source and installed-wheel execution must find their runtime resources.
Changes to resource layout or entrypoints require the installed-artifact check.
Retain standalone execution only when explicitly selected; no silent backend fallback.

## Checks

Run focused tests for the changed behavior. Before committing completed feature
work, arrange independent review and run the full scratch suite:

```sh
uv run python -I -B -m unittest discover -s tests
git diff --check
```

For packaging or bundled-asset changes, also run `uv build` and the installed
wheel check documented in the README. The full suite must use fixtures and
temporary repositories; do not point it at personal projects or provider state.

Follow existing code style. Repository-wide Ruff includes preexisting lint debt;
check newly added code and report unrelated debt separately. Use small logical
commits. Public releases must exclude private historical Git objects as well as
private current-tree files.
