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

Agents can request a specific snapshot viewport without editing the saved map:

```bash
dekart snapshot --report-id <id> --zoom 12 --lat 52.52 --lon 13.405 --out ./snap.png
```

`--zoom` accepts `0` through `24`. Use `--lat` and `--lon` together.

## Run Prepared Query And Download Rows

Run an already-prepared Dekart query, wait for the job, and save result rows:

```bash
dekart run-query --query-id <query-id> --out-dir ./results --wait --json
```

Queries wait by default; use `--no-wait` only when you expect an already-finished job. Prepare the query separately with `dekart call --name create_query` and `dekart call --name update_query`, then use `dekart run-query` to run, poll, and download the result. Use `--json` to return `dataset_id`, `query_id`, `job_id`, terminal status, and `result_file` metadata. Empty downloads fail with `empty result (metadata/SHOW statement?)`.

Resolve a report URL explicitly from a report id:

```bash
dekart report-url --report-id <report-id>
dekart report-url --report-id <report-id> --json
```

## Preview Downloaded Rows

Preview a CSV or parquet file produced by `dekart run-query`:

```bash
dekart preview ./results/<result-file>.parquet --limit 20
dekart preview ./results/<result-file>.parquet --schema
```

Preview output is tab-separated. Parquet and CSV previewing use the bundled DuckDB Python dependency.

## Links

- Dekart: [dekart.xyz](https://dekart.xyz)
- geosql skill: [github.com/dekart-xyz/geosql](https://github.com/dekart-xyz/geosql)
