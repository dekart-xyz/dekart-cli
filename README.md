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

`--stdin` is intended for small/medium payloads and currently supports up to `100 MiB`.
For larger outputs, write to a file and use `--file`.
