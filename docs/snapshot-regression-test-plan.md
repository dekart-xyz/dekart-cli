# Snapshot Manual Regression Checklist

Related design: [Local Snapshot Crash Retry Design](local-snapshot-crash-retry-design.md)

## Purpose

This is a manual checklist for an agent validating the `dekart snapshot`
feature before release. It covers the user journeys most likely to regress.

Use the existing CLI, an existing Dekart instance, and representative reports.
Do not add test helpers, generated fixtures, or a separate automation harness
to execute this checklist.

## Record Before Testing

- OS and architecture.
- Python, Dekart CLI, Playwright, and Chromium versions.
- Dekart server version or commit.
- Whether local snapshots are enabled.
- Whether remote Browserless capture is available.
- IDs and approximate sizes of the reports used.

Never record snapshot tokens, signed URLs, or credentials.

## Reports

Use two existing reports:

1. A small report that normally renders quickly.
2. A large report representative of issue #298. Prefer four GeoJSON layers near
   60,000 points per layer when that fixture is available.

## Common User Scenarios

### 1. Local snapshot

Run a snapshot of the small report with local snapshots enabled.

```sh
dekart snapshot --report-id <small-report-id>
```

Verify exit status `0`, a PNG that opens successfully in the current directory,
and `source: local` in the success output.

### 2. Custom output and viewport

```sh
dekart snapshot \
  --report-id <small-report-id> \
  --out "snapshot custom.png" \
  --zoom 12 \
  --lat 52.52 \
  --lon 13.405
```

Verify the requested file is created and the captured map uses the requested
viewport.

### 3. JSON output

```sh
dekart snapshot --report-id <small-report-id> --json
```

Verify stdout is one valid JSON object, the file exists, and warnings or debug
messages do not contaminate stdout.

### 4. Large local snapshot

Run the large report with the default timeout, then repeat with `--debug`.

```sh
dekart snapshot --report-id <large-report-id>
dekart snapshot --report-id <large-report-id> --debug
```

Verify successful runs produce valid PNG files. If Chromium closes during the
first attempt, verify the CLI retries exactly once with a fresh browser. Debug
stderr must not expose the render token or signed URL values.

Record elapsed time, PNG size, and whether the result was success, timeout, or
target closure. Do not infer a portable point-count limit from one machine.

### 5. Timeout recovery

When the large report reaches a normal Playwright timeout, retry with:

```sh
dekart snapshot --report-id <large-report-id> --timeout 180
```

Verify the first timeout is not automatically retried and the error recommends
the doubled timeout. Record whether `180` succeeds.

### 6. Persistent local browser failure

Optionally, on a disposable local run only, force the first browser page or
process to close and allow the automatic retry to finish. Do not add a helper
harness solely for this check. Verify there are at most two local attempts and
a persistent failure returns exit status `1` without creating a false-success
output.

If remote capture is advertised, verify the message offers `--remote-only`.
If it is not advertised, verify the message says remote capture is unavailable.

### 7. Remote-only snapshot

Run this only when Browserless remote capture is available:

```sh
dekart snapshot --report-id <small-report-id> --remote-only
```

Verify Playwright is not launched, the remote PNG is saved, and the output
reports `source: remote`.

When remote capture is unavailable, verify the command fails clearly instead
of silently falling back to local rendering.

### 8. Local snapshot lifecycle

```sh
dekart snapshot-local status
dekart snapshot-local uninstall
dekart snapshot-local install
dekart snapshot-local status
```

Verify status reflects both configuration and Chromium availability. Restore
the machine to its original enabled/disabled state after testing.

## Completion

The check is complete when:

- Small local capture, custom output/viewport, and JSON output pass.
- The large report was exercised with the default timeout and `180`.
- Retry and recovery messages match the observed failure class.
- Remote-only was tested when a remote service was available, or marked
  unavailable.
- No secrets appeared in stderr or recorded notes.
- The environment and results were added to the related design document when
  they materially change its benchmark findings.
