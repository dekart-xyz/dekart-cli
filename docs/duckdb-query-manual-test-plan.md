# DuckDB query manual regression test plan

This plan is for an agent testing the current `dekart-cli` checkout against a user-started, already-authorized local Dekart branch. It covers DuckDB over an uploaded file and DuckDB over a BigQuery result while retaining the connection-query regression path.

## Safety and prerequisites

- Run commands from `../dekart-cli` with the CLI configured for `http://localhost:8080`.
- The user starts Dekart with the intended environment, such as `make server .env.cloud`, and keeps the frontend on `http://localhost:3000`.
- Never run `make up-and-down`, remove `data/*`, delete Docker volumes, recreate Postgres, or clean test rows. Those actions remove saved connections or CLI pairing.
- Do not restart Postgres. If changed Go code must be loaded, restart only the Dekart server with the same environment.
- Use unique timestamped report titles and leave created reports in place for browser inspection.
- Require `jq`, Python 3.9+, an authorized `dekart` command, and a writable temporary directory.
- Permit the first run to reach DuckDB's core and community extension repositories; signed extensions such as Spatial, Parquet, JSON, and H3 are reused from DuckDB's standard cache afterward.

Set reusable values:

```bash
STAMP=$(date +%Y%m%d-%H%M%S)
WORK_DIR=$(mktemp -d)
RESULT_DIR="$WORK_DIR/results"
mkdir -p "$RESULT_DIR"
dekart config
dekart tools --schema create_query --json
dekart tools --schema run_query --json
REPORT_ID=$(dekart call --name create_report --args '{}' --extract result.report.id || true)
```

Stop if `create_query` does not expose `QUERY_EXECUTION_ENGINE_DUCKDB` or if `run_query` does not expose `accept_duckdb_execution`.

If report creation reaches the workspace map limit, ask the user for an existing writable test report ID and set `REPORT_ID` to it. Do not delete reports to make room. All flows can share this report because every dataset name below includes `STAMP`.

## Flow A: uploaded file to DuckDB

Create deterministic input:

```bash
printf 'id,longitude,latitude,amount\n1,13.405,52.52,10\n2,-73.9857,40.7484,20\n' > "$WORK_DIR/points.csv"
FILE_DATASET_ID=$(dekart call --name create_dataset --args "{\"report_id\":\"$REPORT_ID\"}" --extract result.id)
dekart call --name update_dataset_name --args "{\"dataset_id\":\"$FILE_DATASET_ID\",\"name\":\"Points File $STAMP\"}"
FILE_ID=$(dekart call --name create_file --args "{\"dataset_id\":\"$FILE_DATASET_ID\"}" --extract result.file_id)
dekart upload-file --file "$WORK_DIR/points.csv" --file-id "$FILE_ID" --json
```

Create and run the DuckDB query:

```bash
DUCK_DATASET_ID=$(dekart call --name create_dataset --args "{\"report_id\":\"$REPORT_ID\"}" --extract result.id)
dekart call --name update_dataset_name --args "{\"dataset_id\":\"$DUCK_DATASET_ID\",\"name\":\"File Result $STAMP\"}"
DUCK_QUERY_ID=$(dekart call --name create_query --args "{\"dataset_id\":\"$DUCK_DATASET_ID\",\"execution_engine\":\"QUERY_EXECUTION_ENGINE_DUCKDB\"}" --extract result.query_id)
FILE_SQL="SELECT id, ST_Point(longitude, latitude) AS geometry, CAST(amount AS HUGEINT) AS amount FROM datasets.\"Points File $STAMP\" ORDER BY id"
dekart call --name update_query --args "$(jq -nc --arg id "$DUCK_QUERY_ID" --arg sql "$FILE_SQL" '{query_id:$id,query_text:$sql}')"
dekart run-query --query-id "$DUCK_QUERY_ID" --out-dir "$RESULT_DIR" --json > "$WORK_DIR/file-result.json"
FILE_RESULT=$(jq -r .path "$WORK_DIR/file-result.json")
test -s "$FILE_RESULT"
dekart preview "$FILE_RESULT" --schema
dekart preview "$FILE_RESULT" --limit 10
dekart run-query --query-id "$DUCK_QUERY_ID" --out-dir "$RESULT_DIR" --print
```

Expected: two ordered rows; `geometry` is published as WKB/BLOB; `amount` is `DOUBLE`; JSON stdout parses without warning text; a version warning, if any, appears only on stderr.

## Flow B: BigQuery to DuckDB

Resolve an existing service-account-backed BigQuery connection. Do not create or edit credentials:

```bash
dekart call --name list_connections --args '{}' --json > "$WORK_DIR/connections.json"
BQ_CONNECTION_ID=$(jq -r '.result.connections[]? | select(.connection_type=="CONNECTION_TYPE_BIGQUERY") | .id' "$WORK_DIR/connections.json" | head -1)
test -n "$BQ_CONNECTION_ID"
```

Create a zero-scan literal BigQuery source and prove the legacy connection path:

```bash
BQ_DATASET_ID=$(dekart call --name create_dataset --args "{\"report_id\":\"$REPORT_ID\"}" --extract result.id)
dekart call --name update_dataset_name --args "{\"dataset_id\":\"$BQ_DATASET_ID\",\"name\":\"BigQuery Source $STAMP\"}"
BQ_QUERY_ID=$(dekart call --name create_query --args "{\"dataset_id\":\"$BQ_DATASET_ID\",\"connection_id\":\"$BQ_CONNECTION_ID\",\"execution_engine\":\"QUERY_EXECUTION_ENGINE_CONNECTION\"}" --extract result.query_id)
dekart call --name update_query --args "{\"query_id\":\"$BQ_QUERY_ID\",\"query_text\":\"SELECT 1 AS id, -73.9857 AS longitude, 40.7484 AS latitude UNION ALL SELECT 2, 13.405, 52.52\"}"
dekart run-query --query-id "$BQ_QUERY_ID" --out-dir "$RESULT_DIR" --json > "$WORK_DIR/bigquery-result.json"
test -s "$(jq -r .path "$WORK_DIR/bigquery-result.json")"
```

Create the dependent DuckDB query:

```bash
BQ_DUCK_DATASET_ID=$(dekart call --name create_dataset --args "{\"report_id\":\"$REPORT_ID\"}" --extract result.id)
dekart call --name update_dataset_name --args "{\"dataset_id\":\"$BQ_DUCK_DATASET_ID\",\"name\":\"BigQuery Result $STAMP\"}"
BQ_DUCK_QUERY_ID=$(dekart call --name create_query --args "{\"dataset_id\":\"$BQ_DUCK_DATASET_ID\",\"execution_engine\":\"QUERY_EXECUTION_ENGINE_DUCKDB\"}" --extract result.query_id)
BQ_DUCK_SQL="SELECT id, ST_Point(longitude, latitude) AS geometry, longitude FROM datasets.\"BigQuery Source $STAMP\" ORDER BY id"
dekart call --name update_query --args "$(jq -nc --arg id "$BQ_DUCK_QUERY_ID" --arg sql "$BQ_DUCK_SQL" '{query_id:$id,query_text:$sql}')"
dekart run-query --query-id "$BQ_DUCK_QUERY_ID" --out-dir "$RESULT_DIR" --json > "$WORK_DIR/bigquery-duckdb-result.json"
BQ_DUCK_RESULT=$(jq -r .path "$WORK_DIR/bigquery-duckdb-result.json")
test -s "$BQ_DUCK_RESULT"
dekart preview "$BQ_DUCK_RESULT" --schema
dekart preview "$BQ_DUCK_RESULT" --limit 10
```

Expected: the connection result still downloads normally; the dependent CLI call polls the exact BigQuery source job and writes local Parquet with two ordered rows and WKB geometry. Open both timestamped reports in the UI and verify the same saved DuckDB queries materialize there with matching columns and values.

Optionally repeat the dependent run with `--no-wait`. If BigQuery is still pending, expect pending JSON and no new Parquet; if it already completed, a normal result is valid.

## Flow C: constant HTTP sources and gzip

In Connections, create an HTTP source with the public fixture base URL and no
headers. Reuse one if it already exists. While `httpfs` is unmerged, use its raw
branch URL for the gzip fixture; confirm that URL returns 200 before testing.
Do not change an existing source merely to point it at a different branch.

```bash
HTTP_BASE=https://raw.githubusercontent.com/dekart-xyz/dekart/main/cypress/fixtures/
HTTP_DATASET_ID=$(dekart call --name create_dataset --args "$(jq -nc --arg r "$REPORT_ID" '{report_id:$r}')" --extract result.id)
HTTP_QUERY_ID=$(dekart call --name create_query --args "$(jq -nc --arg d "$HTTP_DATASET_ID" '{dataset_id:$d,execution_engine:"QUERY_EXECUTION_ENGINE_DUCKDB"}')" --extract result.query_id)
HTTP_SQL="SELECT primary_type, district FROM read_csv('${HTTP_BASE}sample.csv') LIMIT 2"
dekart call --name update_query --args "$(jq -nc --arg q "$HTTP_QUERY_ID" --arg sql "$HTTP_SQL" '{query_id:$q,query_text:$sql}')"
dekart run-query --query-id "$HTTP_QUERY_ID" --out-dir "$RESULT_DIR" --print
```

Expected: two THEFT rows in district 1. Repeat with two readers in one query
(`UNION ALL` over `sample.csv` and `sample.csv?station=A`, each limited to one
row). Record the two dataset-source requests in order and verify two final rows.
Repeat with `sample.csv.gz`: the route still ends in `.csv`, the local downloaded
file keeps `.csv.gz`, and the rows match the uncompressed file.

Open the report in the browser and verify matching values. A missing upstream
fixture is a blocked check, not a passing gzip test.

## Flow D: file to computed HTTP query to computed HTTP query

Use the Flow A file dataset. In the report's parameter settings create a text
parameter named `station`, default `B`. Create two DuckDB datasets named
`Computed A $STAMP` and `Computed B $STAMP` with these queries (substitute the
fixture base and timestamp):

```sql
-- A: resolves from an uploaded dependency and a non-default parameter.
SELECT primary_type, district FROM read_csv((
  SELECT format('https://raw.githubusercontent.com/dekart-xyz/dekart/main/cypress/fixtures/sample.csv?station={}',
                CAST(id AS VARCHAR) || {{station}})
  FROM datasets."Points File STAMP" WHERE id = 1
)) LIMIT 1

-- B: resolves only after A has downloaded and executed.
SELECT primary_type, district FROM read_csv((
  SELECT format('https://raw.githubusercontent.com/dekart-xyz/dekart/main/cypress/fixtures/sample.csv?station={}', primary_type)
  FROM datasets."Computed A STAMP"
)) LIMIT 1
```

```bash
dekart run-query --query-id "$COMPUTED_B_QUERY_ID" --params-json '{"station":"A"}' --out-dir "$RESULT_DIR" --print
```

Expected: file download first, then A's dataset-source route with URL ending
`?station=1A`, then B's route with URL ending `?station=THEFT`. The final row is
THEFT, district 1. Path variables continue from the uploaded file through both
HTTP statements. Verify the browser's output using the same parameter value.

Change A's URL subquery to `WHERE id = 0` and re-run B. Expected:
`HTTP source URL query returned 0 rows; exactly one non-empty URL is required.`
There must be no HTTP source request. Check the same error in the browser.
Repeat with two rows, NULL and an empty string; none may send an HTTP request.

For a route error, use a nonexistent file under the same permitted base. The
CLI must stop and display the server's explanation without publishing a result.

Version gate: send `list_connections` and `run_query` requests to
`/api/v1/mcp/call` using the paired device bearer token and a hand-set
`User-Agent: dekart-cli/0.21.0`. Both must return 412 with
`Update GeoSQL and the Dekart CLI: pip install --upgrade geosql dekart`.
Verify no query job was created. Repeat with `dekart-cli/0.22.0`, no agent, and
another agent: these pass. Keep the device token out of logs and evidence.

## Completion record

Record the Dekart commit, CLI commit, Python version, native DuckDB version, browser DuckDB version, created report IDs, command exit codes, result schemas/rows, and whether a version warning occurred. Preserve the reports for review; the temporary local directory may be removed after evidence is captured.


### Local verification, 2026-10-08

- Dekart `httpfs` at `acfae29b`; CLI base `10d1d3f`, working version 0.22.0.
- Backend: `.env.local`, process-local `DEKART_DEV_CLAIMS=1`; frontend:
  `NODE_OPTIONS=--dns-result-order=ipv4first make client`. Saved state retained.
- Python 3.14.5, native DuckDB 1.4.3; program's expected DuckDB version 1.4.3.
  No version warning. Local backend runs without required authentication; the CLI
  sent a device bearer token obtained through browser device authorization.
- `python -m unittest discover -s tests -q`: 171 tests passed.
  Bare `python -m unittest` discovers zero tests in this repository.
- Flow A passed: two ordered rows, WKB geometry, DOUBLE amount.
- Flow C passed for CSV and two sequential HTTP sources: two THEFT/district 1
  rows. Native gzip smoke passed under DuckDB configuration lockdown. Live gzip
  check blocked: `sample.csv.gz` returns 404 on both `main` and `httpfs` raw URLs.
- Flow D passed: uploaded file, A with `station=1A`, B with `station=THEFT`, final
  THEFT/district 1 row; the actual `run-query --params-json` command also passed.
  Zero-row, two-row and NULL URL results stopped before HTTP source downloads.
- Browser computed chain reached Ready with parameter A. Browser zero-row error
  passed with no request for that failing source; video:
  `../dekart/cypress/videos/cliSlice3Browser.cy.js.mp4`.
- Missing-file route preserved the server's 404 explanation. Both old-agent
  calls returned 412 with the upgrade message; query-job count stayed unchanged.
- Flow B blocked: no BigQuery connection is available in this local workspace.
- Wheel and source archive built and passed `twine check`. Clean code review
  found no actionable issue. PyPI release remains pending the blocked checks.
- Report retained for inspection:
  `http://localhost:3000/reports/4fffb63f-bcec-4da8-ab85-3e4817d5f59d/source?qp_station=A`.
