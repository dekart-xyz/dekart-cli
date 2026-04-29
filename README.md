# dekart-cli
Standalone Dekart CLI.

## Install

```bash
pip install -e .
```

After install, command is:

```bash
dekart --help
```

## Config and auth

```bash
dekart config --url http://localhost:3000
dekart init
```

Config and token are stored under:

- `~/.config/dekart/config.json`
- `~/.config/dekart/token.json`

After successful `dekart init`, the CLI can optionally install local snapshot capability.
You can control this behavior with:

```bash
dekart init --local-snapshot ask      # default
dekart init --local-snapshot install  # install right after auth
dekart init --local-snapshot skip     # skip prompt/install
```

## MCP tools

```bash
dekart tools --json
dekart call --name create_report --args '{}'
dekart upload-file --file /tmp/result.csv --file-id <file-id>
```

You can also stream upload content from stdin:

```bash
bq query --use_legacy_sql=false --format=csv 'SELECT 1 AS x' \
  | dekart upload-file --stdin --file-id <file-id> --name result.csv --mime-type text/csv
```

`--stdin` is staged to a temporary file and uploaded in parts, so it avoids loading the full stream
into memory. Total-size limits are enforced by Dekart server configuration.

## Snapshots

Render report PNG snapshots for agent verification:

```bash
dekart snapshot --report-id <report-id> --out ./snapshot.png
```

`dekart snapshot` uses local headless rendering when local snapshot is enabled.
It uses remote snapshot only when local snapshot is disabled (or when you pass `--remote-only`).

Manage local snapshot capability:

```bash
dekart snapshot-local status
dekart snapshot-local install
dekart snapshot-local uninstall
```

Optional full browser cleanup on uninstall:

```bash
dekart snapshot-local uninstall --purge
```

## Local release hook (main branch)

Install repo hooks:

```bash
./scripts/install_hooks.sh
```

On `main`, pre-push will:

1. Bump minor version in `pyproject.toml` and commit it.
2. Build and publish to PyPI with `twine`.
3. Stop the first push intentionally.

Then run push again:

```bash
git push origin HEAD
git push origin HEAD
```

Auth:
- If `PYPI_API_TOKEN` is set, hook uses token auth.
- Otherwise it uses local Twine auth (`~/.pypirc`, keyring, or prompt).
