---
name: wp-orchestration
description: Coordinate coding agents using work packages, lanes, verification, review, and the WPO dashboard. Use for WPO, work packages, lanes, or an orchestration setup. The default T3 backend uses native delegated tasks through a durable coordinator bridge; standalone CLI execution remains explicit.
---

# Work-package orchestration

Break a requested multi-agent effort into work packages (WPs) with a goal, owned paths,
dependencies, acceptance criteria, and verification commands. Reuse the project's existing WPs
and conventions. Ordinary coding tasks need no orchestration machinery.

Choose the execution environment before starting work:

- **T3 Code:** read [references/t3.md](references/t3.md). T3 owns child execution and its worktrees.
  The WPO CLI retains scheduling and artifacts; a live T3 coordinator executes native task requests.
  Backend control wakes require [references/t3-bridge.md](references/t3-bridge.md).
- **Standalone CLI:** read [references/standalone.md](references/standalone.md) when the user
  requests the existing WPO supervisors, lanes, or subprocess harnesses. Those processes are
  separate from T3 threads. Select `execution_mode: standalone` explicitly. Preserve existing holds
  and pauses unless the user asks to resume.
- **Configuring or sharing the skill:** read [references/configuration.md](references/configuration.md).
  Machine paths, custom skills, model preferences, quota policy, and integrations belong in
  untracked machine configuration. Project rules belong in `.orchestration/config.json`.

Keep each WP's ownership and dependency boundary explicit. A suitable WP records:

- `id`, title, status, dependencies, and owned paths;
- the requested behavior and measurable acceptance criteria;
- scoped verification commands and required context;
- progress, review findings, and a handoff with evidence and remaining work.

[templates/WP-TEMPLATE.md](templates/WP-TEMPLATE.md) defines the WP contract for both backends.
The existing router selects a `builder` or `reviewer` tier; the T3 adapter resolves its model and
effort against recorded live native capabilities. Unsupported targets are skipped with their
reason. Refresh the capability record after provider/model changes; never silently substitute
an unavailable explicit model.

The [standalone reference](REFERENCE.md) documents the existing CLI mechanics. Read its relevant
section only when inspecting or maintaining that engine. [SPEC.md](SPEC.md) is historical design
context, not the current configuration contract.
