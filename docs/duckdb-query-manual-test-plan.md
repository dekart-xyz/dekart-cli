# DuckDB query manual regression test plan

This plan is for an agent testing the current `dekart-cli` checkout against a user-started, already-authorized local Dekart branch. It covers DuckDB over an uploaded file and DuckDB over a BigQuery result while retaining the connection-query regression path.

## Safety and prerequisites

- Run commands from `../dekart-cli` with the CLI configured for `http://localhost:8080`.
- The user starts Dekart with the intended environment, such as `make server .env.cloud`, and keeps the frontend on `http://localhost:3000`.
- Never run `make up-and-down`, remove `data/*`, delete Docker volumes, recreate Postgres, or clean test rows. Those actions remove saved connections or CLI pairing.
- Do not restart Postgres. If changed Go code must be loaded, restart only the Dekart server with the same environment.
- Use unique timestamped report titles and leave created reports in place for browser inspection.
- Require `jq`, Python 3.9+, an authorized `dekart` command, and a writable temporary directory.
- Permit the first run to reach DuckDB's official extension repository; signed spatial/parquet extensions are reused from DuckDB's standard cache afterward.

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

If report creation reaches the workspace map limit, ask the user for an existing writable test report ID and set `REPORT_ID` to it. Do not delete reports to make room. Both flows can share this report because every dataset name below includes `STAMP`.

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

## Completion record

Record the Dekart commit, CLI commit, Python version, native DuckDB version, browser DuckDB version, created report IDs, command exit codes, result schemas/rows, and whether a version warning occurred. Preserve the reports for review; the temporary local directory may be removed after evidence is captured.
