# RC-02: Stop committing the database file

**Status:** Approved
**Backlog item:** RC-02 in `docs/BACKLOG.md`

## Summary

Stop tracking `src/raincheck.db` in git and ignore SQLite database files from now on.
Add a `--seed` flag to `src/db.py` so the database can be rebuilt with one command.
Update `README.md` so it describes the project as it exists today.
Define a tested procedure that keeps the user's real database safe when the untracking commit reaches the main checkout.

## Motivation

`src/raincheck.db` is tracked in git (`git ls-files` lists it), and the GitHub repository `nicholassukhdeo1/RainCheck` is **public**.
Every commit that changes the database adds another binary copy to history and publishes whatever was scraped.
Three versions of the file are already on `origin/main` history: blobs `f351c4f` (at the old path `raincheck.db`), `3023c6f`, and `d632e3c` (the current one, last committed 2026-09-06 in `b3bb151`).
They contain one brand row, the two watchlist items with prices and keywords, and at most two public Yahoo listings.
None of them contains secrets or personal data (checked by opening each blob with `sqlite3`).

The backlog asks the README to explain how to rebuild the database with `python3 db.py` "and the seed step".
Today there is no seed step that a user can run.
`seed_from_catalog()` (`src/db.py:73`) is not called by any command; the `__main__` block (`src/db.py:158-161`) only runs `init_db()`.
RC-01 recorded this as a known issue owned by "RC-02 or RC-14".

`README.md` is stale.
It lists `ebay_scrape.py`, which no longer exists (`README.md:29`), says seen listings are stored "in a per-item JSON file" (`README.md:35`), installs dependencies by hand instead of from `requirements.txt` (`README.md:41`), and never mentions the database, `db.py`, `ebay_pricing.py`, or the order the scripts must run in.
RC-01 recorded this as a known issue owned by RC-02.

### The data-loss trap

Untracking a file is safe in the checkout where `git rm --cached` runs, but not in any other checkout.
This was reproduced in throwaway repositories with git 2.54.0:

1. **Pull deletes the file.**
   When a checkout whose `HEAD` still tracks `src/raincheck.db` (unmodified) pulls a commit that untracks it, git deletes `src/raincheck.db` from disk.
   In RainCheck's workflow the implementer commits in a separate worktree, the user merges on GitHub, and the user's main checkout at `/Users/nicholassukhdeo/Desktop/RainCheck-main` then pulls `main`.
   A naive `git pull` deletes the user's real database.
2. **Pull aborts if the file is modified.**
   If the live database differs from the committed copy, the pull aborts with "Your local changes ... would be overwritten".
   The tempting fixes (`git stash`, `git checkout -- src/raincheck.db`, `git restore`) either hide the data or throw it away.
3. **Checking out an old commit overwrites the ignored file.**
   After untracking, `src/raincheck.db` is an ignored file.
   Git treats ignored files as expendable: checking out any commit or branch that still tracks the file silently replaces the live database with the stale committed copy, without an error.
   Switching back then deletes it.
   This applies to every commit from before RC-02 merges.
   Today that means the local `main` and `rc-02-untrack-db` branches (both until Phase C pulls the merge), plus any old commit checked out by hash (for example with `git checkout <sha>` or `git bisect`).
   PR #4 (RC-07) is already merged (`main` is `b781ee5`), and the RC-07 worktree and the other older branches have already been deleted locally and on `origin`.
4. **Moving the file aside first is safe.**
   If `src/raincheck.db` is moved out of the working tree before the pull, the pull fast-forwards cleanly in both the modified and unmodified cases, and moving it back leaves `git status` clean with the file ignored.

Today the live `src/raincheck.db` is byte-identical to the committed blob `d632e3c`, so nothing unique would be lost yet.
That stops being true the first time the user runs a scraper, which is why the procedure below is mandatory and not best-effort.

## Scope

**In scope:**

- Remove `src/raincheck.db` from the git index with `git rm --cached`, keeping the file on disk.
- Add ignore rules for SQLite database files and their side files to `.gitignore`.
- Add a `main(argv=None)` function to `src/db.py` with a `--seed` flag, so `python3 src/db.py --seed` creates the schema and seeds from `catalog.json`.
- Tests for the `--seed` entry point and for the repository's ignore rules and tracked files.
- An accuracy rewrite of the stale parts of `README.md` (listed under Design), plus a new "Database" section.
- The data-safety procedure for the post-merge pull, with each step assigned to a person or agent, and a rehearsal of it by the verifier on a throwaway clone.

**Out of scope:**

- Rewriting git history to remove the old database blobs (see Open question 1; if wanted, it is a separate user-run step).
- Changing seeding semantics: seeding stays `INSERT OR IGNORE`, so catalog edits to existing items still do not reach the database (RC-03).
- Changing the default database location `src/raincheck.db` (see Open question 4).
- Replacing `src/setup.py` or adding item-management commands (RC-14).
- Any schema change.
- Pre-commit hooks, CI, or GitHub push protection.
- README marketing prose, screenshots, badges, architecture diagrams, AWS deployment docs, and any description of features that do not exist yet.
- The "Configuration" and "Running tests" README sections written by RC-01, except where the rewritten "Setup" section needs to link to them.
- `CLAUDE.md`, `docs/BACKLOG.md`, and the agent definitions in `.claude/agents/`.

## Design

### Files touched

| File | Change |
| --- | --- |
| `src/raincheck.db` | Removed from the index with `git rm --cached src/raincheck.db`. The file stays on disk in whichever checkout runs the command. |
| `.gitignore` | Add a commented "Local data" block with `*.db`, `*.db-journal`, `*.db-wal`, `*.db-shm`. |
| `src/db.py` | Replace the `__main__` block with a `main(argv=None)` function that parses `--seed`, and a two-line `__main__` block that calls it. No other function changes. |
| `README.md` | Accuracy rewrite of "What it does", "Tech", "Project structure", "Setup", and "Usage"; new "Database" section. |
| `tests/test_db_cli.py` | New: tests for `db.main`. |
| `tests/test_repo_hygiene.py` | New: tests that no database file is tracked and that the ignore rules match the right paths. |

`src/schema.sql` and `src/catalog.json` are read, not modified.

#### Ignore pattern

The pattern is `*.db` plus the three SQLite side-file suffixes, with no leading slash, so it matches at any depth.

```gitignore
# Local data: the SQLite database is created on each machine and is never committed.
# SQLite can also create -journal, -wal, and -shm side files next to a database.
*.db
*.db-journal
*.db-wal
*.db-shm
```

Why this and not something narrower or broader:

- `src/raincheck.db` alone would miss copies that `RAINCHECK_DB_PATH` points at inside the repository (for example `raincheck-test.db` in the root, which `.env.example` suggests as a path shape), and would miss the side files.
- SQLite in the default rollback mode creates `raincheck.db-journal` for the duration of each write, and leaves it behind if a process is killed mid-write.
  `-wal` and `-shm` appear only in WAL mode, which RainCheck does not use today but might later (for example on Lambda in RC-11).
  If one of these were committed alongside a database, the two could disagree and corrupt the copy someone checks out.
- `*.db*` would also match unrelated files such as `*.dbml`, so the four explicit lines are used instead.
- `*.sqlite` and `*.sqlite3` are not added, because nothing in RainCheck uses those extensions; adding them later is a one-line change.
- Test databases live in pytest's `tmp_path`, outside the repository, so they are unaffected either way.
- The cost: a future test fixture that is a real `.db` file (for example an old-schema database for a migration test) would be ignored.
  The project convention is to build test databases from `src/schema.sql` in `tmp_path`, so that is the intended approach, and `git add -f` remains available as a deliberate escape hatch.

### Schema changes and migration

None.
No table, column, or index changes.

What does need a migration is the **file itself** in every checkout that already has it tracked.
The procedure below is how the user's existing `src/raincheck.db` survives RC-02 with every byte intact.

#### Who does what

| Phase | When | Who | What |
| --- | --- | --- | --- |
| A | From now until the PR is merged | Orchestrator, implementer, reviewer | Keep the untracking commit out of the main checkout. |
| B | After review, before merge | Verifier | Rehearse the procedure on throwaway clones and prove it preserves data. |
| C | Right after the user merges the PR on GitHub | Orchestrator (main session), at the user's request, showing each result | Back up, move the database aside, pull, move it back, verify. |
| D | After Phase C | User, or the orchestrator with the user's go-ahead | Delete the RC-02 implementer's branch and worktree and the merged remote branch; never check out a pre-RC-02 commit in the main checkout. |

Subagents (implementer, reviewer, verifier) never read, move, or write the real `src/raincheck.db`.
The user chose to have the orchestrator, the main session, run Phase C on their behalf, after the user has merged the PR, showing the output of every step; the user can also run the same commands themselves.
The orchestrator may run the read-only checks in step C9 to double-check the result.

#### Phase A: keep the untracking commit out of the main checkout

1. The implementer makes the untracking commit only inside its own worktree, with `git rm --cached src/raincheck.db`.
2. In the main checkout (`/Users/nicholassukhdeo/Desktop/RainCheck-main`), nobody runs `checkout`, `switch`, `merge`, `pull`, `rebase`, `cherry-pick`, or `reset` onto any commit that untracks the file, until Phase C.
3. The orchestrator pushes the implementer's branch from the implementer's worktree (`git -C <worktree> push -u origin <branch>`) and opens the PR from that branch.
4. The reviewer reads the diff with `git diff main...<branch>` and runs `pytest` inside the implementer's worktree, not in the main checkout.
5. The verifier works only in throwaway clones (Phase B).

Committing the spec and tests on `rc-02-untrack-db` in the main checkout is safe, because those commits do not change whether the database is tracked.

#### Phase B: verifier rehearsal on throwaway clones

The verifier creates a temporary directory outside the repository and clones the local repository into it (`git clone /Users/nicholassukhdeo/Desktop/RainCheck-main <tmp>/<name>`).
This copies every local branch as `origin/<branch>`, including the implementer's branch, without touching the real checkout.
The cloned `src/raincheck.db` comes from git, so it is the committed copy, never the user's live data.
Each clone has no `.env` (it is ignored), so no secrets are loaded.
The verifier unsets `RAINCHECK_DB_PATH` in its shell for these steps, so every command uses the clone's own `src/raincheck.db`.

- **B1, negative control (naive pull):** in clone 1, check out `main` (which still tracks the file), then run `git merge --no-edit origin/<implementer-branch>`.
  Expected: `src/raincheck.db` no longer exists.
  This proves the clone reproduces the trap, so B2 means something.
- **B2, the Phase C procedure:** in clone 2, check out `main`, then simulate unique live data with `sqlite3 src/raincheck.db "INSERT INTO brands (name, yahoo_brand_id) VALUES ('SENTINEL', 'SENTINEL-1')"`.
  Record `shasum -a 256 src/raincheck.db`.
  Run Phase C steps C2 through C9 exactly as written, with `~/RainCheck-backups` replaced by a directory inside `<tmp>`, and with `git merge --no-edit origin/<implementer-branch>` in place of `git pull --ff-only origin main` in C6.
  Expected: the checks in C9 all pass, the hash matches the recorded one, and `SELECT name FROM brands WHERE name = 'SENTINEL'` returns one row.
- **B3, old-branch overwrite (documents the trap in the README):** in clone 2 after B2, run `git switch --detach origin/main` (a commit that still tracks the file), then `sqlite3 src/raincheck.db "SELECT COUNT(*) FROM brands WHERE name = 'SENTINEL'"`.
  Expected: `0`, because git overwrote the ignored file with the committed copy.
  This confirms the README warning in criterion 20 is accurate.
- **B4, fresh clone:** clone 3 checks out `origin/<implementer-branch>`.
  Expected: `src/raincheck.db` does not exist; `python3 src/db.py --seed` exits 0, prints `Database initialized at <clone>/src/raincheck.db.` and `Catalog seeded.`; the database then has 1 brand and 2 items; and `git status --porcelain` prints nothing.
- **B5, any working directory:** with `RAINCHECK_DB_PATH=<tmp>/cwd-test.db`, run `python3 <clone 3>/src/db.py --seed` from `<tmp>` (not the repository).
  Expected: exit 0, and `<tmp>/cwd-test.db` has 1 brand and 2 items.
- **B6:** `python3 -m pytest` passes in clone 3.

#### Phase C: the post-merge pull (run by the orchestrator at the user's request)

Run from a terminal, one step at a time, stopping at the first unexpected result.
Replace nothing; the paths are the real ones.

- **C0.** Make sure no RainCheck script is running: `pgrep -fl "yahoo_scrape|ebay_pricing|db.py"` prints nothing.
- **C1.** `cd /Users/nicholassukhdeo/Desktop/RainCheck-main`, then confirm no `.env` moves the live database: `grep -c '^RAINCHECK_DB_PATH' src/.env .env 2>/dev/null` prints only zero counts (it prints counts, never values).
  If any count is not zero, the live database is not `src/raincheck.db`; stop and rethink Phase C.
- **C2.** `git status --short` shows nothing, or only ` M src/raincheck.db`.
  Anything else must first be saved as a temporary work-in-progress commit, not a stash, because the git stash is shared with every worktree; never stash or restore `src/raincheck.db` itself.
- **C3.** `ls src/raincheck.db-*` finds no files.
  A leftover `src/raincheck.db-journal` means a write was interrupted; run `sqlite3 src/raincheck.db "PRAGMA integrity_check;"` once (opening the database rolls the journal back), then confirm the side file is gone before continuing.
- **C4.** Back up and check the backup:

  ```bash
  mkdir -p ~/RainCheck-backups
  cp -p src/raincheck.db ~/RainCheck-backups/raincheck-pre-rc02.db
  shasum -a 256 src/raincheck.db ~/RainCheck-backups/raincheck-pre-rc02.db
  sqlite3 ~/RainCheck-backups/raincheck-pre-rc02.db "PRAGMA integrity_check;"
  sqlite3 src/raincheck.db "SELECT COUNT(*) FROM listings;"
  ```

  The two hashes match, the integrity check prints `ok`, and the listing count is recorded and shown to the user for C9.
  `~/RainCheck-backups` is outside the repository, so no git command can touch it.
- **C5.** Move the live database out of the working tree without overwriting anything: `mv -n src/raincheck.db ~/RainCheck-backups/raincheck-live.db`, then `ls src/raincheck.db ~/RainCheck-backups/raincheck-live.db` must show only the second path.
  If the move did not happen (for example because `raincheck-live.db` already existed from an earlier attempt), stop.
  `git status --short` now shows ` D src/raincheck.db`; that is expected and harmless.
- **C6.** Update `main`: `git switch main && git pull --ff-only origin main`.
  If either command fails, do not improvise: run `mv -n ~/RainCheck-backups/raincheck-live.db src/raincheck.db` to put everything back as it was, then ask for help.
- **C7.** Confirm the untracking arrived: `git ls-files src/raincheck.db` prints nothing.
  If it still prints the path, the merge did not include RC-02; move the file back (C8) and stop.
- **C8.** Move the database back without overwriting anything: `mv -n ~/RainCheck-backups/raincheck-live.db src/raincheck.db`, then `ls` confirms `src/raincheck.db` exists and `raincheck-live.db` is gone.
- **C9.** Verify:

  ```bash
  shasum -a 256 src/raincheck.db ~/RainCheck-backups/raincheck-pre-rc02.db   # hashes match
  git status --short                                                         # no line mentions raincheck.db
  git check-ignore -v src/raincheck.db                                       # .gitignore:<line>:*.db  src/raincheck.db
  sqlite3 src/raincheck.db "PRAGMA integrity_check; SELECT COUNT(*) FROM listings;"
  .venv/bin/python src/db.py                                                 # Database initialized at .../src/raincheck.db.
  ```

  The listing count matches the count recorded in C4.
  The project's `.venv` interpreter is used so `python-dotenv` is available.
  `db.py` without `--seed` only runs `CREATE TABLE IF NOT EXISTS`, so it changes no data.
- **C10.** Keep `~/RainCheck-backups/raincheck-pre-rc02.db` at least until the next real scrape run has succeeded.

**Extra safety net:** as long as the live database has not changed since its last commit (`git hash-object src/raincheck.db` prints `d632e3cdadb74b6cd385ad3d4cbf5c6fd0c85194`), it can also be recovered from history with `git cat-file blob d632e3cdadb74b6cd385ad3d4cbf5c6fd0c85194 > src/raincheck.db`.
This disappears if history is rewritten (Open question 1), so Phase C must be complete before any history rewrite.

#### Phase D: remove the RC-02 branches and stay off pre-RC-02 commits

Any commit from before RC-02 still tracks the file, so checking it out in the main checkout overwrites the live database (trap 3).
The older branches and the RC-07 worktree are already gone, so only RC-02's own branches remain to clean up.
After Phase C:

- **D1.** Remove the RC-02 implementer's worktree with `git worktree remove <worktree path>`.
  That also deletes the worktree's ignored copy of the database, which is fine because it is the committed blob `d632e3c`, not live data.
- **D2.** Delete the local branches `rc-02-untrack-db` and the implementer's RC-02 branch.
  Use `git branch -d`; if git refuses because the PR was squash-merged, confirm on GitHub that the PR is merged, then use `git branch -D`.
- **D3.** Delete the merged RC-02 remote branch on GitHub, so `origin` again has only `main`.
- **D4.** From then on, never check out a pre-RC-02 commit in the main checkout (`git checkout <old sha>`, `git bisect` across the RC-02 merge, or a branch made from an old commit) without first backing up the database as in C4.
  The README warning (criterion 20) records this for the long term.

### Interfaces

```python
def main(argv=None) -> None
```

- Lives in `src/db.py`, at the end of the file, after every other function.
- Parses `argv` with `argparse` (`argv=None` means `sys.argv[1:]`, the standard `argparse` behavior).
- The only option is `--seed` (a `store_true` flag), with help text that says it seeds brands, items, and keywords from `catalog.json` after creating the tables.
  The parser description says the command creates the database tables if they do not exist.
- Argument parsing happens first.
  An unknown argument makes `argparse` print usage to stderr and raise `SystemExit(2)` before any database file is opened or created.
  `--help` prints usage and raises `SystemExit(0)`, also without touching the database.
- After parsing, it calls `load_dotenv()` (moved here from the `__main__` block, so `RAINCHECK_DB_PATH` in `.env` is still honored), then `init_db()`.
- With `--seed`, it then calls `seed_from_catalog()`.
  `init_db()` always runs first, so `--seed` works on a database file that does not exist yet.
- Errors from `seed_from_catalog()` are not caught: a missing catalog raises `FileNotFoundError`, malformed JSON raises `json.JSONDecodeError`, and a missing key raises `KeyError`, each with a traceback and a non-zero exit code when run from the command line.
  The tables created by `init_db()` remain; that is harmless because `init_db()` is idempotent.
- Output is exactly what the existing functions print: `Database initialized at <path>.`, then `Catalog seeded.` when seeding.
- The `__main__` block becomes:

  ```python
  if __name__ == "__main__":
      main()
  ```

Command-line behavior that follows from this:

| Command | Effect |
| --- | --- |
| `python3 src/db.py` | Same as today: create tables if missing. |
| `python3 src/db.py --seed` | Create tables if missing, then seed from `src/catalog.json`. Safe to re-run. |
| `python3 src/db.py --help` | Print usage. |

All three work from any working directory, because RC-01 made the database and catalog paths relative to `src/db.py`.

### README changes

The README is rewritten for accuracy, keeping its personal voice and the "Configuration" and "Running tests" sections from RC-01 as they are.

- **What it does:** replace "Includes an experimental eBay scraper as a second data source" with a bullet saying each item's market price is the median price of matching active eBay listings, fetched through the eBay Browse API.
  Other bullets stay unless they are false.
- **Tech:** add SQLite and the eBay Browse API.
- **Project structure:** list what actually exists: `src/db.py`, `src/schema.sql`, `src/yahoo_scrape.py`, `src/ebay_pricing.py`, `src/sendemail.py`, `src/setup.py`, `src/catalog.json`, `src/raincheck.db` (marked as local and not committed), `tests/`, `scripts/capture_fixtures.py`, and `docs/`.
  Remove `ebay_scrape.py` and the "per-item JSON file" sentence; say instead that listings already seen are stored in the SQLite database.
- **Setup:** create and activate a virtual environment, `pip install -r requirements.txt`, copy `.env.example` to `.env` and fill in the Gmail and eBay values (pointing to "Configuration" for the optional variables), then `python3 src/db.py --seed`.
  State where `.env` may live: `load_dotenv()` searches upward from the `src/` folder, so `src/.env` and a `.env` in the repository root both work.
- **Usage:** run `python3 src/ebay_pricing.py` first (it updates market prices; it needs the eBay credentials), then `python3 src/yahoo_scrape.py`.
  Both can be run from any directory.
  `src/setup.py` is the exception: it must be run from inside `src/` (`cd src && python3 setup.py`) because it reads and writes `catalog.json` relative to the current directory, and after adding items with it you run `python3 src/db.py --seed` so the new items reach the database.
- **Database (new section):**
  - Where it lives: `src/raincheck.db` by default, or the path in `RAINCHECK_DB_PATH`.
  - It is local to each machine and is not committed; `.gitignore` excludes `*.db` and the SQLite side files.
  - Create or rebuild it with `python3 src/db.py --seed`.
  - What a rebuild from scratch loses: the record of listings already seen (so current deals are emailed again on the next run) and eBay-derived market prices (items fall back to `market_price` from `catalog.json` until `ebay_pricing.py` runs again).
  - Seeding only adds brands, items, and keywords that are missing; edits to an existing item in `catalog.json` do not reach the database yet (planned in RC-03).
  - How to back up safely: `sqlite3 src/raincheck.db ".backup '<destination>'"`, which makes a consistent copy even if another process is writing, stored outside the repository.
  - Warning: checking out a commit from before this change (before the RC-02 merge) replaces the local database with the old committed copy without asking, so back it up first.

## Acceptance criteria

Criteria 1-3 and 5-14 are checked by pytest.
Criteria 4 and 15-21 are checked by the reviewer.
Criteria 22-27 are checked by the verifier in Phase B.
Criterion 28 is checked by the user after merge.

**Repository hygiene**

1. `git ls-files` run at the repository root lists no path ending in `.db`, `.db-journal`, `.db-wal`, or `.db-shm`.
2. `git check-ignore --no-index -q <path>` exits 0 (ignored) for each of `src/raincheck.db`, `src/raincheck.db-journal`, `src/raincheck.db-wal`, `src/raincheck.db-shm`, `raincheck-test.db`, and `data/nested/backup.db`.
3. `git check-ignore --no-index -q <path>` exits 1 (not ignored) for each of `src/schema.sql`, `src/catalog.json`, `src/db.py`, `tests/fixtures/catalog.json`, and `requirements.txt`.
4. The branch's diff against `main` shows `src/raincheck.db` as deleted and contains no other change to any `.db` file, and `.gitignore` has the four patterns from the Design section under a comment explaining them.

**`db.main`**

5. With `RAINCHECK_DB_PATH` pointing at a file that does not exist, `db.main([])` creates that file with the five tables (`brands`, `items`, `item_keywords`, `listings`, `listing_matches`), every table has zero rows, and stdout contains `Database initialized at <that path>.`
6. With `RAINCHECK_DB_PATH` pointing at a file that does not exist and the test catalog in place, `db.main(["--seed"])` creates the file and produces exactly the rows from RC-01 criterion 23: one brand `('Rick Owens', '103538')`, items `rick owens ramones` (800.0, `rick+owens+ramones`) and `pod shorts` (300.0, `pod+shorts`), keywords `ramones` and `ramon` for the first item and `pod short` for the second.
7. In criterion 6, stdout contains `Database initialized at <path>.` followed later by `Catalog seeded.`
8. Running `db.main(["--seed"])` twice leaves the row counts of `brands`, `items`, and `item_keywords` the same as after one run.
9. Running `db.main(["--seed"])` on a seeded database that also has a row in `listings` leaves that listing row unchanged.
10. `db.main(["--bogus"])` raises `SystemExit` with code 2, and the file at `RAINCHECK_DB_PATH` does not exist afterwards.
11. `db.main(["--help"])` raises `SystemExit` with code 0, stdout contains `--seed`, and the file at `RAINCHECK_DB_PATH` does not exist afterwards.
12. With `db.CATALOG_PATH` pointing at a file that does not exist, `db.main(["--seed"])` raises `FileNotFoundError`.
13. `db.main` calls `db.load_dotenv` before opening the database: when `db.load_dotenv` is replaced by a function that sets `RAINCHECK_DB_PATH` to `<tmp_path>/from_dotenv.db`, then after `db.main([])` that file exists and has the five tables.
14. The full test suite passes with `python3 -m pytest`, and no test reads or writes `src/raincheck.db`.

**Code and README (reviewer)**

15. `src/db.py` ends with `main(argv=None)` and an `if __name__ == "__main__":` block whose only statement is `main()`; no other function in `src/db.py` changed.
16. `README.md` contains no mention of `ebay_scrape.py`, an "experimental eBay scraper", or a "per-item JSON file".
17. The README "Project structure" lists every file named in the Design section, and marks `src/raincheck.db` as local and not committed.
18. The README "Setup" section includes `pip install -r requirements.txt`, copying `.env.example` to `.env` (with both `src/.env` and the repository root named as valid locations), and `python3 src/db.py --seed`.
19. The README "Usage" section runs `python3 src/ebay_pricing.py` before `python3 src/yahoo_scrape.py`, and says `setup.py` must be run from inside `src/` and followed by `python3 src/db.py --seed`.
20. The README has a "Database" section covering every bullet listed under "Database (new section)" in the Design section, including the warning about checking out commits from before RC-02.
21. The README "Configuration" and "Running tests" sections are unchanged in substance.

**End to end (verifier, Phase B)**

22. B1: after a naive merge in a throwaway clone, `src/raincheck.db` is gone (the trap is reproduced).
23. B2: after the Phase C procedure in a throwaway clone, the database's SHA-256 matches the value recorded before the procedure, the `SENTINEL` brand row is present, `PRAGMA integrity_check` prints `ok`, `git ls-files src/raincheck.db` prints nothing, `git status --porcelain` does not mention it, and `git check-ignore -v src/raincheck.db` names the `*.db` rule.
24. B3: after `git switch --detach origin/main` in that clone, the `SENTINEL` row is gone, confirming the README warning.
25. B4: a fresh clone of the implementer's branch has no `src/raincheck.db`; `python3 src/db.py --seed` exits 0 with both expected lines of output, the database then has 1 brand and 2 items, and `git status --porcelain` prints nothing.
26. B5: `python3 <clone>/src/db.py --seed` run from a directory outside the repository, with `RAINCHECK_DB_PATH` set, seeds that path and exits 0.
27. B6: `python3 -m pytest` passes in the fresh clone.

**After merge (orchestrator, Phase C)**

28. After steps C0-C9, the SHA-256 of `src/raincheck.db` in `/Users/nicholassukhdeo/Desktop/RainCheck-main` equals that of `~/RainCheck-backups/raincheck-pre-rc02.db`, `git ls-files src/raincheck.db` prints nothing, and `git check-ignore -v src/raincheck.db` names the `*.db` rule.

## Edge cases

- **Empty results:** seeding an empty catalog (`[]`) creates the tables and inserts nothing; this is existing `seed_from_catalog` behavior and is not retested here.
- **`None` or missing values:** `db.main(None)` reads `sys.argv[1:]`, so `python3 src/db.py` behaves exactly as before; an unset or empty `RAINCHECK_DB_PATH` falls back to `src/raincheck.db` (RC-01 behavior).
  A catalog entry missing a key raises `KeyError` from `seed_from_catalog` unchanged; validating the catalog belongs to RC-03.
- **Network failure or bad HTTP status:** not applicable; nothing in RC-02 uses the network.
  `git` subprocess calls in tests are local only.
- **Malformed input:** unknown command-line arguments exit with code 2 before any file is created (criterion 10); a missing catalog raises `FileNotFoundError` (criterion 12); malformed catalog JSON raises `json.JSONDecodeError` from `load_catalog`, unchanged.
- **Duplicates:** re-running `--seed` inserts nothing new (criterion 8) and never touches `listings` (criterion 9).
- **Live database modified since its last commit:** a plain pull aborts; Phase C handles both cases by moving the file aside (B2 uses a modified database on purpose).
- **Leftover journal file:** step C3 makes sure no `-journal` side file is separated from its database during the move.
- **A database at a custom `RAINCHECK_DB_PATH` inside the repository:** ignored by `*.db`, so it is never committed by accident.
- **No git or no `.git` (for example a source archive):** the hygiene tests skip with a clear reason instead of failing.
- **Pre-RC-02 commits checked out later:** overwrite the live database (trap 3); handled by Phase D (D4) and the README warning.
- **A branch created before RC-02 merges, merged afterwards:** merges cleanly on GitHub as long as it does not touch `src/raincheck.db`, because a merge where one side deletes the database and the other leaves it unchanged keeps it deleted.
  No such branch exists today.

## Test plan

No new fixtures are needed.
The tests reuse the RC-01 fixtures `temp_db`, `test_catalog` (`tests/fixtures/catalog.json`), and `db_conn`, plus pytest's `capsys`, `monkeypatch`, and `tmp_path`.

| Test file | Covers | Notes |
| --- | --- | --- |
| `tests/test_db_cli.py` | Criteria 5-13 | Calls `db.main([...])` in-process, so the conftest no-op `load_dotenv` and temporary `RAINCHECK_DB_PATH` apply. Criteria 5, 6, 10, 11, and 13 must not use `temp_db`, because they need the database file not to exist beforehand; they still call `assert_db_is_temp(tmp_path)` from `conftest.py` first. Criterion 13 monkeypatches `db.load_dotenv`. |
| `tests/test_repo_hygiene.py` | Criteria 1-3 | Runs `git` with `subprocess.run([...], cwd=<repo root>, capture_output=True, text=True, timeout=10)`, where the repo root is the parent of `tests/`. A module-level check skips the file with `pytest.skip` when `git` is not on `PATH` or `git rev-parse --is-inside-work-tree` fails. `--no-index` makes `check-ignore` test the patterns themselves, independent of whether a path is currently tracked; without it, a still-tracked file is never reported as ignored. |

Why in-process instead of `subprocess` for `db.main`: a real `python3 src/db.py` subprocess would run the real `load_dotenv()` and load `src/.env` secrets into the child process, and it bypasses the conftest safety nets.
The real command-line entry point is covered by the verifier (criteria 25 and 26) instead.

Before implementation, criteria 1, 2 (for `src/raincheck.db` the pattern is missing), and 5-13 (no `db.main`) fail for the right reason.
Criterion 3 already passes before implementation; it guards against an over-broad pattern being added.

The test-writer runs on `rc-02-untrack-db` in the main checkout, which is safe because it does not change what is tracked.

## Shared-file conflicts

Yes: this touches `src/db.py` (only the `__main__` block, replaced by `main()`).
It must not run in parallel with RC-03, RC-04, RC-05, RC-06, RC-13, or RC-14, which also edit `src/db.py`.
If the user chooses option B in Open question 2, RC-02 no longer touches `src/db.py` and only edits `.gitignore`, `README.md`, and tests.

It does not touch `src/schema.sql` or `src/catalog.json`.

Other overlaps:

- PR #4 (RC-07) is merged, and `rc-02-untrack-db` has already been fast-forwarded to `main` (`b781ee5`), so the implementer's worktree, created from `main`, can fast-forward onto `rc-02-untrack-db` once the spec and tests are committed there.
  If `main` moves again before the implementer starts, merge `main` into `rc-02-untrack-db` first; that is safe in the main checkout because `main` still tracks the database until RC-02 merges.
- RC-15 and RC-10 will touch `README.md` only if they document new behavior; neither is in flight.

## Open questions

**Resolved 2026-10-26:** the user chose the recommended option for questions 1 to 4: no history rewrite, a `--seed` flag on `db.py`, the README accuracy rewrite, and the live database stays at `src/raincheck.db`.
The user also chose to have the orchestrator run Phase C after merge.

1. **Rewrite git history to remove the old database blobs?**
   Recommendation: **no.**
   The three blobs on `origin/main` hold one brand, two watchlist items with prices and keywords, and at most two public Yahoo listings, with no secrets or personal data, and they total about 170 KB.
   Rewriting with `git filter-repo --sensitive-data-removal` changes every commit SHA, requires every open PR (including RC-02's own) to be merged or closed first, breaks every existing clone and agent worktree, and per GitHub's documentation still leaves the old commits reachable through cached views and pull request refs unless GitHub Support agrees to clean them up, which it reserves for cases where rotating credentials cannot mitigate the risk.
   It would also remove the history-based recovery path described in Phase C.
   Tradeoff: if you say no, those few rows stay public forever in history; if you say yes, you get a clean history at the cost of a disruptive one-time operation.
   If you want it anyway, it should be its own backlog item, done by you (not an agent) after Phase C and after every open PR is merged: run `git filter-repo --sensitive-data-removal --invert-paths --path src/raincheck.db --path raincheck.db` in a fresh mirror clone, force-push, contact GitHub Support with the affected PR count, and re-clone every checkout.
2. **How should the seed step be exposed?**
   Recommendation: **A, a `--seed` flag on `python3 src/db.py`** (what this spec describes).
   - A: one obvious command, testable, honors `RAINCHECK_DB_PATH` from `.env`, works from any directory, and RC-03 (upsert) and RC-14 (item commands) can build on it.
     Cost: RC-02 now touches the shared file `src/db.py`, so it must run alone.
   - B: document a one-liner such as `cd src && python3 -c "import db; db.init_db(); db.seed_from_catalog()"`.
     No code change, so RC-02 stays off shared files.
     But the one-liner never calls `load_dotenv()`, so a `RAINCHECK_DB_PATH` set in `.env` is silently ignored and the default database is seeded instead; it is also easy to mistype and cannot be tested.
     Choosing B drops criteria 5-13 and 15 and `tests/test_db_cli.py`.
   - C: defer seeding to RC-14 and document only `python3 src/db.py`.
     The README would then describe a rebuild that produces an empty, unusable database, which defeats the backlog's goal.
3. **Is the README rewrite scope right?**
   Recommendation: the accuracy rewrite described in Design (fix everything that is false or missing about how to install and run RainCheck today, add a Database section, leave voice and RC-01 sections alone).
   Alternative: a minimal change (only the Database section and deleting the two false statements).
   Tradeoff: the minimal version is a smaller diff but leaves Setup and Usage wrong, for example missing the eBay pricing step and the database creation step, so a new reader still could not run the project.
4. **Should the live database move out of the repository folder?**
   Recommendation: **not in RC-02**; keep the default `src/raincheck.db` and rely on Phase C, Phase D, and the README warning.
   Alternative: keep your live database somewhere like `~/RainCheck-data/raincheck.db` and set `RAINCHECK_DB_PATH` to it in `src/.env`; you can do that yourself at any time without code changes.
   Tradeoff: outside the repository, no git command can ever overwrite or delete it, which removes trap 3 for good.
   The cost is that every command depends on `.env` being present: without it, scripts quietly use an empty `src/raincheck.db` instead, and the verifier's habit of copying `src/raincheck.db` would need updating.
   RC-11 moves the production database to S3 anyway, so the local location matters less long term.

## Rollback

- **Before merge:** close the PR and delete the branch; nothing in the main checkout has changed.
- **After merge, before Phase C:** revert the merge commit on GitHub (`git revert -m 1 <merge-sha>` on a new branch, then a PR).
  The main checkout still tracks the file and has not pulled, so no data is at risk.
- **After Phase C:** reverting would track `src/raincheck.db` again.
  Because the reverted commit re-adds the old committed copy, pulling it in the main checkout would overwrite the live ignored file (trap 3 in reverse).
  So before pulling a revert, back up and move the database aside exactly as in C4-C5, pull, and then decide deliberately whether to move the live copy back over the tracked one.
  In practice, prefer a forward fix (for example adjusting `.gitignore`) over a revert.
- **Data recovery at any point:** `~/RainCheck-backups/raincheck-pre-rc02.db` from step C4, and, while the live database still matches it, the history blob `d632e3cdadb74b6cd385ad3d4cbf5c6fd0c85194`.
- **Code-only rollback of `--seed`:** restore the previous `__main__` block in `src/db.py`; nothing else depends on `main()`.
