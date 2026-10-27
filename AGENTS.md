# RainCheck: instructions for Codex and other coding agents

This file is the handoff for coding agents that do not read `CLAUDE.md` (for example OpenAI Codex).
`CLAUDE.md` holds the same project rules plus Claude-specific workflow details; read it too.
When the two disagree, this file and `CLAUDE.md` should be fixed to match, not silently ignored.

## What RainCheck is

RainCheck scrapes Yahoo Auctions Japan for new listings of tracked Rick Owens items and emails the best deals.
Each listing is compared against a market price computed from active eBay listings (eBay Browse API).
It is Python 3 with a SQLite database.

## Layout

- `src/schema.sql` defines the tables (schema version 1, stored in `PRAGMA user_version`).
- `src/db.py` holds every database function, the versioned migrations (`MIGRATIONS`), catalog validation, and seeding.
- `src/catalog.json` is seed input only: edit it, then run `python3 src/db.py --seed`.
  The jobs read item configuration from the database, never from `catalog.json`.
- `src/ebay_pricing.py` computes each item's market price from eBay and stores it.
- `src/yahoo_scrape.py` parses Yahoo's embedded `__NEXT_DATA__` JSON, finds new deals, and sends one digest per run.
- `src/digest.py` builds the digest email (pure: no database, network, clock, or print).
- `src/matching.py` holds the one shared keyword matcher used by both jobs.
- `src/sendemail.py` sends email over SMTP, or prints it when `RAINCHECK_DRY_RUN` is set.
- `scripts/capture_fixtures.py` records live Yahoo and eBay fixtures; only a human runs it (it needs real credentials).
- `docs/BACKLOG.md` is the prioritized feature list; `docs/specs/` holds one approved spec per feature.
- `.codex/agents/` defines the Codex subagents `planner`, `test_writer`, `implementer`, `reviewer`, and `verifier`.
  They are ported from `.claude/agents/` (the Claude Code versions); keep their role instructions in sync when either changes.
  Model names and effort settings are provider-specific.

## Setup and commands

- `python3 -m venv .venv && .venv/bin/python -m pip install -r requirements-dev.txt`
- Run the tests: `.venv/bin/python -m pytest` from the repository root.
  The suite must pass fully, with no warnings: `pyproject.toml` turns `ResourceWarning` and unraisable exceptions into errors, so a leaked database connection fails the suite.
- Create or rebuild a database: `python3 src/db.py --seed`.
- Safe manual runs: set `RAINCHECK_DRY_RUN=1` (prints the email instead of sending) and `RAINCHECK_DB_PATH=<temp file>` (never the real database).

## Rules that are never broken

- Never read, print, or commit `.env` or any secret.
- Never commit any `*.db` file; `src/raincheck.db` is untracked and holds the user's real data.
- Never read, write, migrate, or delete the user's real `src/raincheck.db`; use a throwaway copy via `RAINCHECK_DB_PATH`.
  A schema change ships with a migration in `MIGRATIONS` and a written, backup-first upgrade procedure in its spec.
- Tests never touch the network, never send email, and never use the real database (the fixtures in `tests/conftest.py` enforce this).
- Parameterized SQL only; single-value tuples need a trailing comma; every `UPDATE` and `DELETE` has a `WHERE`.
- Every network call has a timeout.
- Every database function closes its connection on every path, including errors.
- HTML built from scraped data is escaped with `html.escape(..., quote=True)`, and only `http`/`https` URLs become links or images.
- Never use the em dash character; use a plain dash.
- In Markdown, put each full sentence on its own line.
- Commit messages never include a Co-Authored-By trailer or any AI attribution.
- Never manually edit auto-generated files.

## How to work on a feature

The user is learning and prioritizes quality, simplicity, robustness, and long-term maintainability over speed.
Explain what each step does and why, suggest better alternatives, and flag tradeoffs.

The main session is the orchestrator: it runs each stage by asking the matching subagent by name (for example "Have planner write the spec for RC-04"), checks the result itself, and only advances when a stage actually passes.
Codex does not enforce the subagents' read-only rules or isolate them in separate worktrees, so the orchestrator checks `git status` after each stage and keeps the implementer on the feature branch.

Codex subagent defaults for the Plus plan are:

| Agent | Model | Effort |
| --- | --- | --- |
| `planner` | `gpt-6-sol` | `medium` |
| `test_writer` | `gpt-6-luna` | `high` |
| `implementer` | `gpt-6-luna` | `high` |
| `reviewer` | `gpt-6-sol` | `high` |
| `verifier` | `gpt-6-luna` | `medium` |

For complex implementation such as RC-03 or RC-11, explicitly invoke `implementer` with `gpt-6-sol` and `high` effort.
The implementer agent file leaves its model settings unset so that this explicit choice overrides the project defaults in `.codex/config.toml`.

Every backlog item goes through these stages in order:

1. **Plan** (`planner`): write `docs/specs/<ID>-<slug>.md` from `docs/specs/TEMPLATE.md`, with testable acceptance criteria and an explicit out-of-scope list.
   Read every file the feature touches first; confirm external API behavior from official docs.
   Put decisions that belong to the user under "Open questions" with a recommendation.
2. **Approve:** the user reads and approves the spec; record their decisions in a "Resolved" note.
   Nothing is built from an unapproved spec.
3. **Test** (`test_writer`): write failing tests from the spec (not from an implementation), and confirm each fails for the right reason.
4. **Implement** (`implementer`): make the tests pass on a feature branch without editing the tests.
5. **Review** (`reviewer`): review the full diff against the spec with fresh context.
   Fix findings, including small pre-existing issues you notice (lint, flaky tests, leaks); record spec changes as dated "Amendments".
6. **Verify** (`verifier`): run the real entry points against a throwaway database with dry run on.
   For anything visual (the digest email), render it at phone (375px) and desktop widths and fix layout flaws.
7. **Merge:** push the branch and open a pull request; the user reviews and merges it.

After a merge that changes the database schema or untracks files, the user's real database needs a careful, backup-first step on their machine; follow the procedure in that feature's spec.

## Where the project stands (2026-10-27)

- Merged: RC-01 (test foundation), RC-02 (database untracked), RC-07 (Yahoo `__NEXT_DATA__` parsing), RC-03a (schema v1, migration, validated seeding), RC-03b (jobs read the database), RC-15 (one digest email per run), RC-04 (record matched item for emailed listings).
- The user's real database is already migrated to schema version 1 and seeded.
  Backups from each upgrade are in `~/RainCheck-backups`.
- Suggested next items: RC-10 (reliability and logging), RC-16 (auction-aware deals), RC-17 (word-aware matching).
  Read each item's "Depends on" and "Touches" lines in `docs/BACKLOG.md` first; items touching `src/schema.sql`, `src/db.py`, or `src/catalog.json` must not run in parallel.
- The stored eBay price for Ramones (420.0) is stale; running `python3 src/ebay_pricing.py` with the user's credentials refreshes it.
- AWS work (RC-11, RC-12) must use the project's single region, `us-east-2`.
