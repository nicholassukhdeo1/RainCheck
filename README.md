# RainCheck

A personal web-scraping project that hunts for underpriced fashion listings and emails me the best daily deals.

I built this to get hands-on with web scraping, HTTP requests, and automating a task I actually cared about - finding grail pieces (like Rick Owens Ramones) below their typical resale price.

## What it does

- Lets me build a watchlist of items to track, each with a market price and keywords
- Sets each item's market price from matching active eBay listings in the first 50 eBay Browse API search results
- Scrapes the Rick Owens brand page on Yahoo Auctions Japan (sorted newest-first)
- Filters listings by keyword, converts prices from JPY to USD, and keeps only those below market
- Ranks the remaining listings by discount and remembers which ones it has already seen
- Sends one digest email per run with the top 5 new deals per item (an HTML table plus a plain-text version), and no email when there are no new deals

## Tech

- Python
- SQLite (the watchlist, seen listings, and market prices)
- eBay Browse API (market prices)
- BeautifulSoup (HTML parsing)
- Requests (HTTP)
- smtplib (email delivery)

## Project structure

```
src/
├── db.py            # every database function; run it to create and seed the database
├── schema.sql       # the table definitions
├── yahoo_scrape.py  # scrape Yahoo Auctions Japan and email the best deals
├── ebay_pricing.py  # compute each item's market price from eBay and store it
├── matching.py      # the keyword matcher both jobs share (include and exclude keywords)
├── digest.py        # builds the digest email (subject, HTML, plain text) from the deals
├── sendemail.py     # sends the digest email (text + HTML)
├── setup.py         # manage tracked items in the SQLite database
├── catalog.json     # the watchlist of items being tracked
└── raincheck.db     # the SQLite database (local to each machine, not committed)
tests/               # offline test suite
scripts/
└── capture_fixtures.py  # saves real pages for the test fixtures
docs/                # backlog and feature specs
```

Each watchlist item is a dictionary (name, brand, size, search query, market price, keywords).
On each run the scraper pulls current listings, compares them against the item's market price, and stores the listings it has already seen in the SQLite database so repeat runs only surface new finds.

## Setup

1. Create and activate a virtual environment, then install dependencies:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. Copy `.env.example` to `.env` and fill in your Gmail and eBay values (this file is gitignored and must never be committed):
   ```
   SENDER_EMAIL=you@gmail.com
   SENDER_PASSWORD=your_gmail_app_password
   RECEIVER_EMAIL=you@gmail.com
   EBAY_CLIENT_ID=your_ebay_client_id
   EBAY_CLIENT_SECRET=your_ebay_client_secret
   ```
   `SENDER_PASSWORD` is a Gmail [app password](https://myaccount.google.com/apppasswords), not your normal account password.
   `load_dotenv()` searches upward from the script's own folder (`src/`), so the `.env` file can live in `src/` or in the repository root.
   (Under a debugger it searches from the current directory instead.)
   See "Configuration" below for the optional variables.
3. Create and seed the database:
   ```bash
   python3 src/db.py --seed
   ```

## Usage

1. Edit `src/catalog.json` to change what is tracked.
2. Run `python3 src/db.py --seed` to copy the catalog into the database.
3. Update the market prices (this needs the eBay credentials), then scrape and email the best deals:

```bash
python3 src/ebay_pricing.py   # update market prices from eBay
python3 src/yahoo_scrape.py   # scrape and email the best deals
```

Both can be run from any directory.

- Both jobs read the items only from the database, never from `catalog.json`, so a `catalog.json` edit takes effect only after you seed.
- Both jobs ignore listings whose title contains an exclude keyword.
- Matching is case-insensitive substring matching, so `low` also matches `yellow` (until RC-17 makes it word-aware).
- eBay is searched with the item's `search_query`, or with the item name when it has none.
- eBay searches the newest-first first 50 results and stores each distinct fixed-price listing's first observed USD ask for that item.
  Variations that share a legacy listing ID count once, and a later asking-price change never rewrites the first observation.
  The table keeps these observations permanently, including prices that are currently outliers.
  The current market price is the all-time arithmetic mean of prices retained after the median absolute deviation (MAD) filter: prices more than 3.5 scaled MADs from the pool median are excluded, and when MAD is zero only prices equal to the median remain.
  RainCheck updates that price once at least five retained distinct listings have accumulated; otherwise it preserves the existing market price.
  Each run logs new and duplicate counts plus cumulative candidate, outlier, and retained counts.
  The first-page limit means listings beyond the newest 50 can be missed when more than 50 relevant new listings arrive between runs.
  Listing history can be inspected with `SELECT ebay_listing_id, first_price_usd, first_seen_at FROM ebay_listing_prices WHERE item_id = ? ORDER BY first_seen_at, ebay_listing_id`.
- If a job finds an outdated or missing database, it prints one `Error:` line naming the command that fixes it and stops before any network request.
- HTTP 429 and 5xx responses are retried up to two times with short bounded waits; transport errors are not retried.
  Requests retain their explicit per-call timeouts, and server `Retry-After` delays above five seconds stop that request from retrying.
- Job status and failures are logged to stderr.
  A job exits with status 1 after a terminal failure, including an isolated item failure; successful no-work runs exit 0.
- eBay item failures leave that item's prior data intact and do not stop later item searches, except a final HTTP 429 stops further eBay searches.
- Yahoo sends at most one digest per run and records all displayed deals together only after a successful send.
  If SMTP accepts a message and the later database transaction fails, the deals can be emailed again on a later run.

From the repository root, use the item CLI to manage one tracked product at a time:

```bash
python3 src/setup.py add --name "Pod Shorts Custom" --keyword "pod shorts" --search-query "rick owens pod shorts" --manual-price 250
python3 src/setup.py list
python3 src/setup.py update 3 --manual-price 275 --exclude-keyword fake
python3 src/setup.py remove 3
python3 src/setup.py list --all
python3 src/setup.py add --name "Pod Shorts Custom" --keyword "pod shorts"
python3 src/setup.py update 2 --manual-price 250
python3 src/setup.py adopt 2
```

Use the IDs printed by `list` in place of `3` and `2`; `2` is the catalog's `pod shorts` item in this example.
`add` reuses an inactive item's ID when its brand and name match.
The CLI edits the manual fallback USD price for a tracked item; each Yahoo auction keeps its own yen price and the eBay estimate takes priority when available.
`adopt` requires an exact brand and name match in `src/catalog.json` and copies that entry's configuration to the item.
The commands work from any directory and honor `RAINCHECK_DB_PATH`.
They require an initialized, current-version database.

## Database

- The database lives at `src/raincheck.db` by default, or at the path in `RAINCHECK_DB_PATH`.
- It is local to each machine and is not committed.
  `.gitignore` excludes `*.db` and the SQLite side files (`-journal`, `-wal`, `-shm`).
- Create or rebuild it with `python3 src/db.py --seed`.
- Rebuilding from scratch loses two things.
  The record of listings already seen is gone, so current deals are emailed again on the next run.
  The eBay-derived market prices are gone too, so items fall back to `market_price` from `catalog.json` until `ebay_pricing.py` runs again.
- Seeding (`python3 src/db.py --seed`) syncs `catalog.json` into catalog-managed rows in the database.
  CLI-managed and unclaimed rows remain unchanged, including when the catalog has a matching name or omits them.
  A new catalog row is marked `catalog`; new CLI rows are marked `cli`.
  Existing rows migrated from an older database are `unclaimed` because their ownership cannot be inferred safely.
  Use `python3 src/setup.py adopt ID` to let later seeds update or deactivate an individual item.
  Seeding keeps listing matches and eBay price history.
- `market_price` in `catalog.json` is only a fallback.
  It is stored as `manual_price_usd`, and it never overwrites the eBay-derived `market_price_usd`.
  Deal math uses the eBay price when there is one and the catalog price otherwise.
- Seeding checks the whole catalog first and reports every problem per entry (for example `entry 2 ("pod shorts"): market_price must be a number greater than 0`).
  If anything is invalid, nothing is written.
- The database has a schema version.
  After you update RainCheck, back up the database and run `python3 src/db.py` to upgrade it in place without losing listings or eBay prices.
  The jobs refuse to run on an outdated database and tell you to do this, so nothing is changed behind your back.
- For the version 4 upgrade, stop scheduled jobs and make a timestamped SQLite `.backup` outside the repository before running `python3 src/db.py` without `--seed`.
  Check `PRAGMA integrity_check` on the backup first.
  After upgrading, confirm `PRAGMA user_version` is 4, `PRAGMA integrity_check` returns `ok`, `PRAGMA foreign_key_check` is empty, and row counts for `items`, `item_keywords`, `listings`, `listing_matches`, and `ebay_listing_prices` are unchanged.
  All preexisting items should have `config_source = 'unclaimed'`; inspect `python3 src/setup.py list --all` before adopting any row.
  Keep the backup until the CLI and jobs work normally.
- To back it up safely, run `sqlite3 src/raincheck.db ".backup '<destination>'"`.
  This makes a consistent copy even if another process is writing, so store it outside the repository.
- Warning: checking out a commit from before this change (before the RC-02 merge) replaces your local database with the old committed copy without asking.
  Back it up first.

## Configuration

Two optional environment variables change how RainCheck behaves.
Set them in your shell or in `.env`.

- `RAINCHECK_DB_PATH` - path to the SQLite database file.
  When unset or empty, RainCheck uses `src/raincheck.db`.
  A relative path is resolved against the directory you run the command from.
  The parent directory must already exist.
- `RAINCHECK_DRY_RUN` - when set to anything except `0`, `false`, `no`, `off`, or empty, the whole digest (subject, text part, and HTML part) is printed instead of sent.
  Unrecognized values (such as a typo like `ture`) also count as on, so a typo never sends real email.
  Dry run only affects email.
  Database writes still happen, so a dry run against your real database marks listings as seen and they will not be emailed later.
  Pair `RAINCHECK_DRY_RUN=1` with a throwaway `RAINCHECK_DB_PATH`.

eBay pricing also needs `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET` (see `.env.example`).

## Running tests

The test suite runs fully offline against a throwaway database.
From the repository root:

```bash
pip install -r requirements-dev.txt
python3 -m pytest
```

## AWS daily run (RC-11)

The SAM template is limited to `us-east-2` and rejects a deployment in another selected Region.
The schedule starts disabled and is set for 09:00 `Asia/Tokyo` each day.
No AWS deployment, production database upload, or schedule enablement is part of local development.
Review the proposed CloudFormation change set and the cost categories in [the RC-11 spec](docs/specs/RC-11-aws-daily-run.md) before an authorized deployment.

### Offline preflight

Run `python3 -m pytest` to check the handler and template without AWS, network, SMTP, or the production database.
Run `sam validate --template-file template.yaml --lint` when the SAM CLI and linter are available.
Run `sam build --use-container --template-file template.yaml` with the Python 3.12 Lambda build container before deployment.
The `build-RainCheckFunction` Makefile target copies an explicit source allowlist and installs the pinned production dependencies, including `boto3`, into the Lambda artifact.
For an offline source-only package check, run `make build-RainCheckFunction ARTIFACTS_DIR=/tmp/raincheck-package-check PIP=true` using a fresh output directory.
Check that the artifact contains only the nine runtime files from `src/`, with no `.env`, `.db`, test, or fixture file.

### Production preflight and initial upload

1. Confirm that the selected Region is `us-east-2`.
   Review the recorded AWS CloudShell gate for every active Yahoo brand: each brand needs HTTP 200, the expected `__NEXT_DATA__` listing path, and at least one valid listing record.
   The current gate passed only for Rick Owens brand ID `103538`; probe each future brand using the command in the RC-11 spec before including it in an AWS run.
2. Create the required standard-tier SSM `SecureString` parameters in `us-east-2` with the AWS-managed `aws/ssm` key.
   The default names are `/raincheck/ebay/client-id`, `/raincheck/ebay/client-secret`, `/raincheck/sender/email`, and `/raincheck/sender/password`.
   Set the optional receiver parameter name when mail should go to a different address.
   Check parameter names, types, and presence without printing or exporting their values.
   The template stores only parameter names in Lambda environment variables.
3. Stop local jobs and make a timestamped SQLite `.backup` of `src/raincheck.db` outside the repository, following the Database section above.
   If the database needs migration, follow its backup-first upgrade procedure before taking the final upload snapshot.
   On that snapshot, require `PRAGMA integrity_check` to return `ok`, `PRAGMA foreign_key_check` to return no rows, and `PRAGMA user_version` to equal `db.SCHEMA_VERSION` in this release.
   Keep the local backup separately from S3 version retention.
4. Build and deploy a reviewed change set using `sam deploy --template-file .aws-sam/build/template.yaml --stack-name raincheck --region us-east-2 --capabilities CAPABILITY_IAM --no-execute-changeset`.
   Inspect the change set, then deploy with the same command without `--no-execute-changeset` after authorization.
   Always deploy the built template so the explicit Makefile allowlist, rather than the repository root, becomes the Lambda package.
   The S3 bucket is retained if the stack is deleted, and its name is available from the `DatabaseBucketName` stack output.
5. Upload the verified snapshot to the stack's bucket at the configured `DatabaseKey` using an HTTPS S3 request with SSE-S3 (`AES256`).
   Record the returned S3 `VersionId`, download that exact version to a separate temporary file, and repeat the SQLite integrity, foreign-key, and schema-version checks on the downloaded bytes.
   Confirm the bucket and key in the Lambda configuration match this object before the schedule is enabled.

The execution role can access only that exact S3 object and the named SSM parameters, so a changed key or parameter name requires a reviewed stack update.
Stop the schedule before any manual object replacement; the handler's ETag condition rejects changed content, but equal-content replacements can share an ETag.

### Isolated dry-run and schedule enablement

Deploy a separate throwaway stack in `us-east-2` with `--parameter-overrides DryRun=1`, which creates a separate bucket and keeps its schedule disabled.
Upload a separate database containing only synthetic listings and no active items; `DryRun=1` sets `RAINCHECK_DRY_RUN=1` in that stack's Lambda configuration.
The empty active watchlist avoids live listing acquisition while exercising S3 download, schema checks, SSM access, the runner, and the conditional upload.
Invoke only that throwaway function with an empty event after the separate bucket and database have been verified.
The dry-run sends no email; the handler suppresses the complete printed digest before CloudWatch receives stdout.
Inspect its logs for success and confirm they contain no digest HTML, synthetic listing title, or recipient address.
Check that the production bucket's current S3 object version is unchanged.
The dry-run can never write the production S3 object because its execution role and environment point to the throwaway bucket.

After the CloudShell, parameter, backup, upload, and isolated dry-run checks pass, change only the production `DailySchedule` `State` in `template.yaml` from `DISABLED` to `ENABLED` in a reviewed deployment and inspect its change set before applying it.
Keep the default checked-in template disabled until that explicit enablement decision.
The schedule uses a fixed 09:00 Tokyo time, no flexible delivery window, and bounded Scheduler delivery attempts and event age.

### Rollback

Disable the schedule first by setting its template `State` back to `DISABLED` and deploying the reviewed change.
If the current database object is bad, inspect a prior S3 version, verify its SQLite integrity and schema version, then restore that version while retaining the bad version for investigation.
Redeploy the prior Lambda code and SAM template if the code or infrastructure caused the failure.
Keep the local backup and retained bucket until a replacement has been verified; decide whether to retain or remove billable resources before deleting the stack.

## Notes

This is a personal learning project, built to sharpen my Python and get comfortable with scraping and automation end-to-end.
