# dekart-cli

[Dekart](https://github.com/dekart-xyz/dekart) MCP wrapper for your AI agent to create maps from SQL query results.

Works best with the [geosql](https://github.com/dekart-xyz/geosql) skill — it writes the SQL, this CLI renders the map.

Works with any Dekart instance:

- **Dekart Cloud** (SaaS, default)
- **Self-hosted** (your own URL)
- **Localhost** (`http://localhost:8080`)

## Init

```bash
pip install dekart
dekart init
```

`dekart init` walks you through:

1. Picking the instance (Cloud / self-hosted / localhost).
2. Authorizing the CLI in your browser.
3. Optionally enabling local snapshots.

To switch instance later:

```bash
dekart config --url <your-url>
dekart init
```

Config and token: `~/.config/dekart/`.

## Enable local snapshot

Local headless renderer for fast PNG snapshots without a round-trip to the server:

```bash
dekart snapshot-local install
```

Manage:

```bash
dekart snapshot-local status
dekart snapshot-local uninstall          # remove renderer
dekart snapshot-local uninstall --purge  # also remove its browser cache
```

When local snapshot is enabled, `dekart snapshot --report-id <id> --out ./snap.png` uses it automatically. Pass `--remote-only` to force server-side rendering.

## Run Query And Download Rows

Run a warehouse query through a Dekart connection, wait for the job, and save result rows:

```bash
dekart query --connection-id <id> --sql-file ./query.sql --out ./result.parquet --wait --print
```

Queries wait by default; use `--no-wait` only when you expect an already-finished job. Use `--json` instead of `--print` to return `report_id`, `dataset_id`, `query_id`, `job_id`, terminal status, `report_url`, and saved file metadata as JSON. Printing parquet rows uses the bundled DuckDB Python dependency. Empty downloads fail with `empty result (metadata/SHOW statement?)`.

## Download Query Result By Job ID

Fetch result bytes for a finished job:

```bash
dekart fetch-job --job-id <job-id> --wait
```

Notes:
- By default, dataset id is resolved from `check_job_status.query_job.dataset_id`.
- Output path defaults to `job-<job-id>.<extension>`.
- Extension is resolved from `check_job_status` (`csv` or `parquet`).

## Links

- Dekart: [dekart.xyz](https://dekart.xyz)
- geosql skill: [github.com/dekart-xyz/geosql](https://github.com/dekart-xyz/geosql)
