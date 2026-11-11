---
id: WP-XXXX
title: Short imperative title
milestone: M?
lane: name             # optional: the lane supervisor that runs it
status: todo
depends_on: []
builder: code          # an intelligence tier (SKILL.md, Model routing), tier+taste, or pin:<route>
reviewer: code-hard    # a tier, or none
owns: []               # the only paths this WP may change, besides its own file and the shared files
delegate: code-simple  # optional: the tier of the builder's workers, or none
mode: single           # optional: staged, for small-context executors
skills: []             # optional: skills its code-writing sessions load besides the project's
---

## Goal
One or two sentences: what exists when this WP is done, and why it matters.

## Context
Links to the spec sections, research and earlier WPs this builds on.

## Deliverables
- Files, modules, APIs or documents this WP produces.

## Steps
- [ ] Step 1
- [ ] Step 2

## Acceptance criteria
1. Each criterion is checkable by a command or an observation.

## Verify
The WP's own scoped checks; the project's `verify_round` checks run after them, so do not repeat them. The whole test suite runs only on the merge into main.
```
one command per line; every one must exit 0
```

## Out of scope
- What this WP deliberately does not do.

## Progress log

## Review

## Try it
<!-- The builder writes this before review: the exact commands to run from the main checkout once
this is merged, and what to look for (a window, a file, an output). For a WP with nothing to try by
hand, say so and why. -->


## Handoff
