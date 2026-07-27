# dekart-cli

[Dekart](https://github.com/dekart-xyz/dekart) MCP wrapper for your AI agent to create maps from SQL query results.

Works best with the [geosql](https://github.com/dekart-xyz/geosql) skill — it writes the SQL, this CLI renders the map.

Works with any Dekart instance:

- **Dekart Cloud** (SaaS, default)
- **Local (Docker)** (on your machine with persistent local data)
- **Self-hosted** (your own URL)

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

## Version telemetry

The CLI sends a best-effort version check with its package version and an opaque random installation ID. GeoSQL and Dekart CLI share the ID at `${XDG_CONFIG_HOME:-~/.config}/dekart/installation_id`; deleting that file resets it. Set `DO_NOT_TRACK=1` (or `DNT=1`) to disable the version check before an ID is created. CI sends a reserved non-persisted test ID.

## Local Dekart with Docker

Choose Local during `dekart init`, or manage it directly:

```bash
dekart local up
dekart local status
dekart local status --json
dekart local down
dekart local remove
```

The CLI uses a labeled container named `dekart-local` and a labeled persistent Docker volume named `dekart-local-data`. `down` stops the container without deleting the container or volume. `remove` asks for confirmation, then removes both the container and its data volume; use `remove --force` to skip confirmation. If port 8080 is occupied by another service, the CLI selects the first free port through 8099 and saves that URL before authorization begins.

When the CLI starts or stops Docker, it prints `Executing: <docker command>` before running it. You can also choose “I will start or connect to Dekart myself” during `dekart init`; the CLI prints a simple standalone `docker run` command without CLI labels, container names, or volumes, then asks for the Dekart URL.

The initial Docker command is:

```bash
docker run -d \
  --name dekart-local \
  --label xyz.dekart.cli.managed=true \
  --restart unless-stopped \
  -p 127.0.0.1:8080:8080 \
  -v dekart-local-data:/dekart/data \
  dekartxyz/dekart:latest
```

The CLI never starts, stops, removes, or replaces a same-name container without its management label. Docker is not installed or started automatically; `dekart init` gives platform-specific instructions when it is unavailable.

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

When the server provides a verified render-URL lifetime and an established local
page or browser target closes unexpectedly, the CLI retries once with a fresh
browser and the same per-operation timeout. Browser launch failures and normal
timeouts are not retried; increase a normal timeout explicitly, for example with
`--timeout 180`.
Remote capture is never selected automatically. Use `--remote-only` when the
configured Dekart instance provides a remote snapshot endpoint.

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
