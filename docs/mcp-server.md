# GeoSQL MCP server

Requires Python 3.10 or newer. Install the HTTP server and point it at one Dekart:

```sh
pip install 'dekart[mcp]'
DEKART_URL=https://your-dekart.example dekart mcp serve --host 0.0.0.0 --port 8000
```

Connect an MCP client to `/mcp` using Streamable HTTP and
`Authorization: Bearer <device token>`. Create the token in Dekart workspace
settings. Every request needs a token; browser Origin requests are refused.
The server reads no CLI configuration or saved credentials. Put TLS in front of
it when serving outside localhost.

The server passes through Dekart's tools, replacing `run_query` and
`create_report_snapshot` and adding `upload_file` and `get_report_url`.
Queries return `row_count`, `schema`, positional `rows` and `truncated`.
`query_params_values` uses the CLI's `qp_` URL encoding, for example `qp_year=2026`.
A warehouse query still running after the wait returns `job_id`; poll with
`check_job_status`, then call `run_query` with both `query_id` and `job_id`.
DuckDB queries cannot resume by job id. Their deadline includes source downloads
and execution, after waiting for warehouse source jobs. Slow queries should be
simplified because they also run in users' browsers; the agent may explicitly
increase `duckdb_timeout_seconds`, up to 120 seconds.

`upload_file` accepts exactly one of UTF-8 `content` and `content_base64`.
It uploads to an existing `file_id`, created through Dekart's dataset/file tools.
It never opens a path supplied by the caller.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `DEKART_URL` | required | Dekart API URL |
| `DEKART_APP_URL` | `DEKART_URL` | Browser URL used for report links; also an allowed snapshot render origin |
| `GEOSQL_MCP_HEAVY_SERVICE_URL` | unset | Forward queries and snapshots to this server's `/mcp`; unset runs everything here |
| `GEOSQL_MCP_DUCKDB_MEMORY_LIMIT` | `1GB` | DuckDB memory limit |
| `GEOSQL_MCP_ROW_LIMIT` | `1000` | Maximum returned rows; each call defaults to 50 |
| `GEOSQL_MCP_BYTE_LIMIT` | `1048576` | Maximum query preview JSON bytes |
| `GEOSQL_MCP_UPLOAD_LIMIT` | `2097152` | Maximum decoded inline upload bytes |
| `GEOSQL_MCP_WAIT_SECONDS` | `45` | Wait for warehouse jobs |
| `GEOSQL_MCP_DUCKDB_TIMEOUT_SECONDS` | `10` | Default DuckDB deadline |
| `GEOSQL_MCP_DUCKDB_MAX_TIMEOUT_SECONDS` | `120` | Largest agent-requested DuckDB deadline, never above 120 |

Both light and heavy services must use the same Dekart and access policy. A heavy
HTTP 429 becomes the tool error `Busy, retry`; HTTP 401 is forwarded unchanged.
There are no sessions, tool caches or stored caller credentials, so replicas
and restarts do not require affinity.

## Full runtime

The process executing snapshots needs Playwright and Chromium:

```sh
pip install playwright
python -m playwright install --with-deps chromium
```

Preinstall the DuckDB `spatial` and community `h3` extensions for the same DuckDB
version, platform and runtime user as the server. Execution disables automatic
extension installation and can load those extensions without internet access.
Snapshots require egress to the render origin and `api.mapbox.com`.
Use one Dekart replica: snapshot render tokens live in Dekart's memory.
The MCP server itself may have multiple replicas.

Snapshots currently use the CLI's 1600 by 900 PNG default. Claude Desktop sizing
verification is still pending; width and height can be supplied explicitly.
Render URLs must use the `DEKART_URL` or `DEKART_APP_URL` origin.

## Desktop extension

`GET /geosql.mcpb` serves the Claude Desktop extension. Python wheel and sdist
builds bundle it automatically. In a checkout, run `python3 scripts/build_mcpb.py`
to create `dekart/geosql.mcpb` before starting the server. Both server images must
install the built wheel or run that command when copying the checkout.

Download `/geosql.mcpb` and open it in Claude Desktop. Enter a token created in
Dekart workspace settings. The MCP Server URL defaults to
`https://geosql.dekart.xyz/mcp`; for self-hosted installations, enter the server's
own `/mcp` endpoint. Dekart's optional `DEKART_GEOSQL_MCP_URL` setting is the base
address (without `/mcp`) and adds the download link and endpoint to the settings
card. An unset setting keeps the existing Claude skill instructions.

The extension uses Claude Desktop's bundled Node.js (18 or newer), has no npm
dependencies, and forwards stdio messages as authenticated HTTP POSTs without
OAuth discovery. It targets GeoSQL's stateless JSON responses, not arbitrary
SSE MCP servers. Tokens are passed through the extension's sensitive setting,
never command arguments or logs. Missing or revoked tokens say “Create a new
token in Dekart”; HTTP 429 says “Busy, retry”.

## Tests

```sh
pip install 'dekart[mcp]' pytest
python -m pytest tests/test_mcp_server.py
python -m pytest tests
node --test tests/desktop-extension.test.cjs
```

The full-image extension test and the Claude Desktop, Cloud and self-hosted
checks also require their respective deployment runtimes.
