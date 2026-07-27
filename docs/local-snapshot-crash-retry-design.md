# Local Snapshot Crash Retry Design

Status: Implemented

Issue: [dekart-xyz/dekart#298](https://github.com/dekart-xyz/dekart/issues/298)

## Problem

`dekart snapshot` renders locally with Playwright when the local snapshot capability is enabled. A large report with four GeoJSON layers of about 60,000 features each intermittently failed on Windows with:

```text
Page.wait_for_function: Target page, context or browser has been closed
```

The same report usually succeeded, smaller reports did not reproduce the failure, and Cloud Browserless capture via `--remote-only` remained reliable.

The exact error is not a normal Playwright timeout. A timeout raises Playwright's `TimeoutError`; a closed-target error means the page, context, or Chromium process ended while the CLI was waiting. Renderer memory or process pressure is a plausible hypothesis for the heavier report, but one failure in eight full-size local attempts is not enough to identify whether the backend, page, renderer process, GPU process, or browser process caused the exit.

The CLI currently launches Chromium once, performs one render attempt, and returns an error for every exception. It does not distinguish a transient browser exit from a timeout or permanent input/network error.

## Goals

- Recover automatically from one transient local page or browser crash.
- Keep local snapshot behavior deterministic and avoid silently switching render providers.
- Distinguish closed-target failures from genuine Playwright timeouts.
- Add useful `--debug` diagnostics without exposing the snapshot token.
- Preserve the existing CLI command shape, output schema, and default timeout.

## Non-goals

- Dynamically calculate a timeout from report row or layer counts.
- Change Dekart's snapshot readiness contract.
- Add Chromium flags, software rendering, or memory tuning without evidence that they address this failure.
- Add or configure Browserless for self-hosted Dekart.
- Automatically fall back to remote capture.
- Pin or otherwise change the installed Playwright version.

## Current Behavior

`render_local_snapshot_png` creates one Playwright instance, browser, context, and page. It then:

1. Navigates to `snapshot_render_url`.
2. Waits for `window.__dekartSnapshotReadyToken` to match the expected token.
3. Takes a PNG screenshot.
4. Closes the context and browser on the success path.

Any exception reaches `handle_snapshot`, which exits with status `1` and tells the user to rerun with `--remote-only`. Cleanup is not explicitly guaranteed for partial setup or failure paths.

The `--timeout` value is independently applied to the MCP call, page navigation, readiness wait, screenshot, and remote download. It is not an end-to-end command deadline. This design preserves that behavior.

## Observational Local Reproduction Benchmark

Benchmark date: 2026-07-25

### Result

The exact issue signature was not reproduced locally:

```text
Target page, context or browser has been closed
```

At the issue's reported scale of four layers with 60,000 points each, all 8/8 local snapshots succeeded. The first observed failure with the default 90-second timeout occurred at 65,000 points per layer, but it was a normal Playwright readiness timeout:

```text
Page.wait_for_function: Timeout 90000ms exceeded.
```

At 65,000 points per layer, one trial succeeded and one timed out with the default timeout. Repeating the same report twice with `--timeout 180` produced 2/2 successful snapshots. This is a timing-sensitive boundary, not a deterministic point limit. Reports with 80,000 and 100,000 points per layer also succeeded with larger timeout values and without page or browser termination.

On this machine and synthetic payload, the observed default-timeout transition is between 62,500 and 65,000 points per layer, or between 250,000 and 260,000 total rendered points across four layers. There is no observed local threshold for the issue's target-closed failure up to the maximum tested 100,000 points per layer, or 400,000 total points.

### Local PC and Runtime

- MacBook Pro model `Mac14,9`.
- macOS 26.5, build `25F71`, arm64.
- Apple M2 Pro with 10 CPU cores and 16 GPU cores.
- 16 GiB host RAM.
- Docker Desktop engine 27.4.0 with an 8-CPU, 7.75-GiB Linux arm64 VM.
- Disposable Dekart container with no explicit CPU or memory limit.
- Dekart image `dekartxyz/dekart@sha256:4730611263d4ef9a81f076c843fab5cce8fab2937c79f0e220f5aa93d24caa28`, created 2026-07-02.
- Python 3.13.7.
- Playwright Python 1.58.0.
- Playwright Chromium bundle 1208, Chrome for Testing 145.0.7632.6.
- Dekart CLI commit `d78ecc9597faa0c413e353c274ce501c7741c729` on `main`.
- Snapshot code matched that commit; the working tree also contained unrelated installation-ID changes.
- Normal desktop load remained present; no deliberate memory pressure or application shutdown was used.

The shell's default Homebrew Python 3.14.5 did not have Playwright and could not install it into the externally managed environment. The benchmark therefore selected the existing Python 3.13.7 runtime with Playwright explicitly. This interpreter choice is recorded as part of the observed setup and did not modify the system Python.

### Isolation and Method

- Ran a disposable `dekartxyz/dekart` container bound only to loopback on a dedicated port, with no persistent Docker volume.
- Used a temporary `XDG_CONFIG_HOME`; the normal Dekart CLI configuration and tokens were not read or changed.
- Used the container's default auth-free local mode, SQLite database, local file storage, and embedded frontend.
- Browserless was not configured, so all measured snapshots used local Playwright and remote capture was unavailable.
- Created a separate report for each point-count tier.
- Created four independent uploaded GeoJSON datasets per report and four visible `geojson` Kepler layers using `columnMode: geojson` and the `_geojson` column.
- Used the same deterministic point grid in each layer. Each feature contained `id`, `category`, `value`, and `name` properties plus Point geometry.
- Kept viewport at 1600 by 900, zoom 6, map center, map style, layer styling, and data schema constant.
- Launched a fresh Playwright Chromium process through the normal `dekart snapshot` command for every trial.
- Classified closed-target failures separately from Playwright timeouts and other errors.
- Measured end-to-end command time, PNG size, exit status, and approximate aggregate RSS for the CLI/Playwright/Chromium process tree sampled every 200 ms.

The RSS sampler ran `ps -axo pid,ppid,rss`, recursively discovered descendants from the CLI process ID, summed their reported KiB values, and divided by 1,048,576 to report GiB. The figure is useful for comparison between tiers but is not a precise physical-memory measurement because summing process RSS can count shared pages more than once.

This is an observational local benchmark, not a checked-in reproducibility suite. The temporary generator, raw result log, uploaded reports, and disposable container were intentionally kept outside the repository. The table preserves the evidence needed for this design decision without adding a permanent benchmark harness for one machine-specific investigation.

### Measurements

| Points per layer | Total points | GeoJSON per layer | Timeout | Trials | Result | Successful command time | Approx. peak process RSS |
| ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: |
| 101 | 404 | 15 KB | 90s | 1 | 1/1 success | 4.4s | 1.02 GiB |
| 20,000 | 80,000 | 3.12 MB | 90s | 3 | 3/3 success | 13.9-14.3s | 1.11-1.16 GiB |
| 40,000 | 160,000 | 6.25 MB | 90s | 3 | 3/3 success | 39.6-43.0s | 1.16-1.25 GiB |
| 60,000 | 240,000 | 9.39 MB | 90s | 8 | 8/8 success | 76.9-86.9s | 0.95-1.21 GiB |
| 62,500 | 250,000 | 9.78 MB | 90s | 2 | 2/2 success | 85.4-87.1s | 1.10-1.18 GiB |
| 65,000 | 260,000 | 10.18 MB | 90s | 2 | 1/2 success, 1 timeout | 98.1s success | 1.09-1.10 GiB |
| 65,000 | 260,000 | 10.18 MB | 180s | 2 | 2/2 success | 88.7-91.3s | 1.19-1.20 GiB |
| 80,000 | 320,000 | 12.53 MB | 180s | 1 | 1/1 success | 125.6s | 1.17 GiB |
| 100,000 | 400,000 | 15.67 MB | 240s | 1 | 1/1 success | 185.6s | 1.09 GiB |

The successful 65,000-point command took 98.1 seconds even with `--timeout 90` because the timeout is applied separately to navigation, readiness, and screenshot rather than to the full command.

### Interpretation and Limits

- The paired 65,000-point trials support failure-specific guidance: increasing the timeout from 90 to 180 seconds changed the same report from 1/2 to 2/2 successful, while it would not explain the issue's target-closed error.
- The reported Windows crash remains credible but unverified on macOS arm64. Real Windows or captured-report validation is a residual risk, not a release gate, because no deterministic failing fixture is available. The implementation must still include a mocked regression for the exact reported target-closed error.
- Point count is not a portable capacity limit. Geometry complexity, property width, layer styling, browser/GPU build, host memory pressure, basemap/network timing, and operating system can move the boundary.
- The synthetic properties were controlled and moderately compact. A real 60,000-row layer with wider attributes can impose more parsing and memory pressure than the 9.39-MB benchmark layer.
- Trial counts above 65,000 points were intentionally small because each render took two to three minutes and higher tiers approached the five-minute snapshot-token lifetime.
- The maximum tier was selected to avoid mixing renderer stability with token expiry. Absence of a crash up to 400,000 total points does not prove that Chromium cannot crash above that workload.

## Proposed Behavior

Split local rendering into a single-attempt operation and a small retry orchestrator.

The single-attempt operation records its current stage (`launch`, `navigate`, `ready`, or `screenshot`), observes Playwright's page crash and browser disconnect events, and always closes any created context and browser in `finally`.

Failure classification must snapshot events and connection state before cleanup begins. A `cleanup_started` guard prevents an intentional `browser.close()` from being recorded as an unexpected disconnect. Cleanup errors are secondary diagnostics: they never replace the original render error, trigger a retry by themselves, or turn successfully captured PNG bytes into a failed attempt.

The orchestrator retries exactly once with a fresh Playwright Chromium process when the failed attempt shows that its target ended unexpectedly:

- Playwright emitted a page crash event.
- Playwright emitted a browser disconnect before CLI cleanup.
- The page reports that it is closed.
- The browser reports that it is disconnected.
- The normalized Playwright error contains the observed phrase `target page, context or browser has been closed` or the documented phrase `page crashed`.

Use public Playwright APIs and events for classification. Check `TimeoutError` first, then the pre-cleanup event and connection-state snapshot, then the two exact normalized message fixtures above. Do not use broad `closed` or `crash` substring matching. The narrow message fallback is necessary because Playwright Python does not expose a stable public target-closed exception type across installed releases.

Do not retry:

- Playwright `TimeoutError`.
- HTTP, DNS, TLS, or authentication failures.
- Snapshot readiness failures where the target remains alive.
- Screenshot or filesystem errors where the target remains alive.
- Playwright installation or Chromium launch failures that occur before a browser target exists.

Each attempt uses the existing per-operation `--timeout`. `handle_snapshot` captures the monotonic response-receipt time immediately after `mcp_call` returns, before config loading or response parsing. It adds a positive integer `expires_in` value to that timestamp and passes the resulting deadline to the retry orchestrator. A retry does not repeat the MCP call; it reuses `snapshot_render_url` only when the current monotonic time is strictly less than the deadline. At or after the deadline, the CLI skips the retry and reports that the render URL expired.

Missing, invalid, or non-positive `expires_in` does not reject the response or prevent the initial render attempt, preserving compatibility with older or incomplete servers. It does prevent a retry because the CLI cannot verify that the same signed render URL is still valid. Diagnostics distinguish an expired URL from an unverified lifetime. The token can still expire during a best-effort retry that starts before a verified deadline; refreshing it is outside this design.

If the retry succeeds, the command behaves like any successful local render. If it fails, the CLI exits with status `1` and reports both the final failure and that one local retry was attempted.

Because navigation, readiness, and screenshot each use the same per-operation timeout, one 90-second attempt can theoretically take about 270 seconds. A late crash followed by a retry can raise the local-render worst case from about 270 to 540 seconds, excluding the initial MCP call. The retry remains limited to target termination and never follows a normal timeout.

## Failure Guidance

When local rendering ultimately fails, print recovery options that match the classified failure and the capabilities returned by the server:

- After target closure or page crash, suggest rerunning the same command with `--remote-only` only when the response contains a non-empty `snapshot_url`. Do not suggest a larger timeout because target closure is not a timeout.
- After Playwright `TimeoutError`, suggest rerunning locally after adding or replacing `--timeout` with twice the current value, so the default `90` becomes `180`.
- When a genuine timeout occurs and `snapshot_url` is available, show both alternatives: recommend remote capture for reliability, and offer the larger timeout for users who need local rendering.
- For target-closure and timeout failures where `snapshot_url` is empty, state that remote capture is unavailable for the configured Dekart instance instead of suggesting an unusable option.
- For authentication, network, installation, and other permanent errors, print the relevant error without unrelated remote or timeout advice.

Print argument changes rather than reconstructing a full command from user-controlled values. This avoids shell-specific quoting problems across POSIX shells, PowerShell, and `cmd.exe`. Guidance must not contain `snapshot_render_url`, `snapshot_url`, or a snapshot token.

## Remote Capture

The CLI must not automatically switch from local rendering to remote capture. Local rendering can be selected to keep rendering on the user's machine, while self-hosted remote capture may send the render through an operator-configured Browserless service.

`--remote-only` remains the explicit opt-in. It works only when the Dekart server returns a non-empty `snapshot_url`. Self-hosted Dekart already provides that URL when its Browserless capture is configured; without Browserless, the server intentionally returns only `snapshot_render_url`. Enabling remote capture for such an instance is a server deployment concern, not a CLI retry concern.

## Diagnostics

With `--debug`, report:

- Local render attempt number.
- Failed stage.
- Exception type and message.
- Whether page crash or browser disconnect was observed.
- Chromium version and host platform when available.
- Elapsed time for the failed attempt.

Use one shared sanitizer for every exception, warning, diagnostic, and final error written to stderr, including non-debug output. It must remove snapshot-token query values even when Playwright embeds the full URL in an exception or call log. Do not log the full `snapshot_render_url`; log its origin and path plus query parameter names, or redact all query parameter values.

Normal non-debug output remains concise. A recovered retry may print one sanitized warning to stderr so human users know the first renderer exited. JSON stdout must remain valid and unchanged; its existing `snapshot_render_url` field intentionally retains the URL when the user requests JSON, so the no-token rule applies to stderr diagnostics rather than the command's requested machine-readable result.

## Compatibility

- No new CLI flags or environment variables.
- No change to `--timeout` default or semantics.
- No change to the JSON result fields.
- No automatic remote requests.
- No server or proto changes.
- Existing successful local and remote flows remain unchanged.

## Tests

Add focused regression coverage using mocked Playwright boundaries:

- A closed-target first attempt followed by success launches a fresh browser and returns local PNG bytes.
- Two closed-target failures stop after two attempts and return the final classified error.
- A page crash event is retryable.
- A browser disconnect is retryable.
- The exact supported target-closed and page-crashed message fixtures are retryable; broader similar wording is not.
- Playwright `TimeoutError` is not retried.
- A non-target Playwright error followed by normal cleanup is not retried.
- An expired render URL skips retry; a retry that starts before expiry remains best effort.
- Partial browser/context setup is cleaned up after failure.
- Cleanup exceptions after render failure do not mask the primary error.
- Cleanup exceptions after successful capture do not discard valid PNG bytes.
- Debug and normal stderr sanitize a token embedded in an exception message; requested JSON output remains unchanged.
- `handle_snapshot` still does not download `snapshot_url` after local failure.
- A target-closed failure suggests `--remote-only` only when `snapshot_url` is available and never suggests increasing the timeout.
- Genuine timeouts at the default and a non-default value, such as `120`, suggest `180` and `240` respectively, and also offer `--remote-only` when available.
- Recovery guidance contains only safe argument changes and no verbatim free-form user strings such as report IDs or output paths, signed URLs, or tokens. It may use the validated numeric timeout to calculate the larger value.
- Missing `snapshot_url` produces a remote-unavailable message only after target-closure and timeout failures.
- Authentication, network, installation, and other permanent errors emit neither remote nor timeout guidance.
- Existing local success, `--remote-only`, viewport, and validation tests remain green.

## Implementation Plan

1. Refactor local rendering in `dekart/cli.py` into one render attempt plus one bounded retry.
2. Classify target termination from evidence captured before cleanup and enforce the snapshot-token expiry deadline.
3. Guarantee best-effort cleanup without letting cleanup events or errors change classification or primary results.
4. Apply shared token sanitization to normal and debug stderr, then add lifecycle diagnostics behind `--debug`.
5. Add failure-specific recovery guidance and argument changes, then update snapshot help and `README.md` with timeout and remote-capture guidance.
6. Add regression tests in `tests/test_snapshot_urls.py` and run the full unit test suite.

## Acceptance Criteria

- The reported closed-target failure receives one automatic fresh-browser retry when its render token is valid at retry start.
- A successful retry produces the PNG with `source: local` and exit status `0`.
- Persistent target closure stops after two attempts and exits with status `1`.
- Genuine timeouts and permanent errors are not retried.
- No remote capture occurs unless the user passes `--remote-only`.
- Failed renders offer only recovery actions that apply to the classified failure and available server capabilities.
- Normal and debug stderr contain enough lifecycle evidence to distinguish timeout, page crash, and browser disconnect without exposing the snapshot token.
