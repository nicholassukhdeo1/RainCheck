# <ID>: <Feature title>

**Status:** Draft | Approved | Implemented
**Backlog item:** <ID> in `docs/BACKLOG.md`

## Summary

One or two sentences on what this feature does.

## Motivation

Why this matters, and what is broken or missing today.
Reference the current code with `file:line` where relevant.

## Scope

**In scope:**

- ...

**Out of scope:**

- ...

## Design

### Files touched

| File | Change |
| --- | --- |
| `src/...` | ... |

### Schema changes and migration

Describe any change to `src/schema.sql`.
Describe exactly how an existing `raincheck.db` is migrated without losing data.
Write "None" if there is no schema change.

### Interfaces

New or changed function signatures, return values, and error behavior.

## Acceptance criteria

Each criterion must be concrete enough that a test can check it.

1. ...
2. ...

## Edge cases

- Empty results:
- `None` or missing values:
- Network failure or bad HTTP status:
- Malformed input:
- Duplicates:

## Test plan

Which fixtures are needed (saved HTML, recorded API JSON, seed data), and what each test covers.

## Shared-file conflicts

Does this touch `src/schema.sql`, `src/db.py`, or `src/catalog.json`?
If yes, it must not run in parallel with other features that touch the same files.

## Open questions

Decisions that belong to the user, each with a recommendation and its tradeoff.

## Rollback

How to undo this change if it causes problems.
