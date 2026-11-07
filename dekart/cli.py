import argparse
import datetime as dt
import json
import math
import mimetypes
import os
import platform
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
from importlib import metadata
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urljoin, urlparse, urlsplit, urlunsplit

from dekart import local_docker
from dekart import client as client_operations
from dekart.installation_id import get_installation_id
from dekart.client import (
    DekartClient,
    extract_json_path,
    parse_int,
    header_get,
    request_id_from_headers,
    parse_http_error_body,
    find_nested_string,
    extract_structured_error_fields,
    parse_required_headers,
    build_put_url,
    tool_properties,
    save_binary_file,
    resolve_job_result_extension,
    is_terminal_done_status,
    query_job_status,
    is_terminal_failed_status,
    query_job_error,
    required_nested_scalar,
)

from dekart.snapshot import (
    LocalSnapshotRenderError, render_local_snapshot_png, snapshot_expiry_deadline,
    snapshot_url_debug_summary, print_snapshot_stderr, is_png_snapshot,
    sanitize_snapshot_diagnostic, snapshot_token_from_render_url,
)

DEFAULT_DEKART_URL = "https://cloud.dekart.xyz"
LOCALHOST_DEKART_URL = "http://localhost:8080"
DEFAULT_VERSION_CHECK_URL = "https://cloud.dekart.xyz/api/v1/version/dekart"


def build_parser():
    """Build CLI parser for dekart commands."""
    parser = argparse.ArgumentParser(
        prog="dekart",
        description="Dekart CLI for auth, MCP tools, uploads, and snapshots.",
    )
    subparsers = parser.add_subparsers(dest="command")

    mcp = subparsers.add_parser("mcp", help="MCP server commands.")
    mcp_commands = mcp.add_subparsers(dest="mcp_command")
    serve = mcp_commands.add_parser("serve", help="Serve GeoSQL MCP (Python 3.10 or newer).")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    config = subparsers.add_parser("config", help="Set or show Dekart base URL.")
    config.add_argument(
        "--url",
        help="Dekart base URL (examples: http://localhost:3000, https://my-dekart.company.com).",
    )

    init = subparsers.add_parser("init", help="Authorize this CLI against Dekart.")
    init.add_argument(
        "--no-browser",
        action="store_true",
        help="Print authorization URL without opening browser automatically.",
    )
    init.add_argument(
        "--local-snapshot",
        choices=("ask", "install", "skip"),
        default="ask",
        help="Install or skip local headless snapshot capability (default: install automatically).",
    )
    init.add_argument(
        "--bigquery-passthrough",
        choices=("ask", "enable", "disable"),
        default="ask",
        help="Configure local gcloud authentication for BigQuery passthrough (default: ask).",
    )

    local = subparsers.add_parser("local", help="Manage local Dekart with Docker.")
    local_subparsers = local.add_subparsers(dest="local_action")
    local_subparsers.add_parser("up", help="Start local Dekart in Docker.")
    local_subparsers.add_parser("down", help="Stop local Dekart and preserve its data.")
    local_remove = local_subparsers.add_parser("remove", help="Remove local Dekart and permanently delete its data.")
    local_remove.add_argument("-f", "--force", action="store_true", help="Delete without confirmation.")
    local_status = local_subparsers.add_parser("status", help="Show local Dekart status and URL.")
    local_status.add_argument("--json", action="store_true", help="Print machine-readable status JSON.")
    local.set_defaults(local_action="status")

    tools = subparsers.add_parser("tools", help="List MCP tools from configured Dekart.")
    tools.add_argument(
        "--json",
        action="store_true",
        help="Print raw JSON payload.",
    )
    tools.add_argument(
        "--names",
        action="store_true",
        help="Print only tool names (one per line).",
    )
    tools.add_argument(
        "--match",
        help="Case-insensitive substring filter for tool names (for example: file,upload).",
    )
    tools.add_argument(
        "--schema",
        help="Print one tool schema by exact name.",
    )
    tools.add_argument(
        "--arg-keys",
        action="store_true",
        help="Print each tool with declared inputSchema property keys.",
    )

    resolve_tools = subparsers.add_parser(
        "resolve-tools",
        help="Resolve required Dekart MCP tool names for report->dataset->file flow.",
    )
    resolve_tools.add_argument(
        "--json",
        action="store_true",
        help="Print resolved mapping as JSON.",
    )
    resolve_tools.add_argument(
        "--shell",
        action="store_true",
        help="Print shell exports for eval/source usage.",
    )
    resolve_tools.add_argument(
        "--out",
        help="Write output to file path instead of stdout.",
    )

    call = subparsers.add_parser("call", help="Call Dekart MCP tool.")
    call.add_argument("--name", required=True, help="MCP tool name.")
    call.add_argument(
        "--args",
        default="{}",
        help="Tool arguments as JSON object string. Supports @/path/to/args.json.",
    )
    call.add_argument(
        "--args-file",
        help="Path to JSON file with tool arguments object.",
    )
    call.add_argument(
        "--args-stdin",
        action="store_true",
        help="Read tool arguments JSON object from stdin.",
    )
    call.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="HTTP timeout seconds for MCP call (default: 30).",
    )
    call.add_argument(
        "--debug",
        "--verbose",
        dest="debug",
        action="store_true",
        help="Print MCP endpoint/tool/timeout diagnostics and request id.",
    )
    call.add_argument(
        "--json",
        action="store_true",
        help="Print raw JSON payload.",
    )
    call.add_argument(
        "--extract",
        help="Extract one scalar field from full response JSON path, for example: result.report.id",
    )
    call.add_argument(
        "--out",
        help="Write command output to file path instead of stdout.",
    )

    upload_parts = subparsers.add_parser(
        "upload-parts",
        help="Upload local file bytes with MCP-provided multipart part endpoint and headers.",
    )
    upload_parts.add_argument("--file", required=True, help="Local file path to upload.")
    upload_parts.add_argument(
        "--start-response-json",
        help="JSON string from start_file_upload_session (either full MCP response or result object).",
    )
    upload_parts.add_argument(
        "--start-response-file",
        help="Path to JSON file from start_file_upload_session.",
    )
    upload_parts.add_argument(
        "--upload-part-endpoint",
        help="Part endpoint template (for example /api/v1/file/.../parts/{part_number}).",
    )
    upload_parts.add_argument(
        "--required-headers-json",
        help="JSON list with headers, for example '[{\"key\":\"x-header\",\"value\":\"v\"}]'.",
    )
    upload_parts.add_argument(
        "--max-part-size",
        type=int,
        help="Max bytes per part. If omitted, uses value from start response.",
    )
    upload_parts.add_argument(
        "--file-id",
        help="File id override. Use when start response does not include file_id.",
    )
    upload_parts.add_argument(
        "--complete-args-out",
        help="Write ready JSON args for complete_file_upload_session to this file path.",
    )
    upload_parts.add_argument(
        "--json",
        action="store_true",
        help="Print raw JSON payload.",
    )

    upload_file = subparsers.add_parser(
        "upload-file",
        help="Start upload session, upload parts, and complete in one command.",
    )
    upload_file.add_argument("--file", help="Local file path to upload.")
    upload_file.add_argument(
        "--stdin",
        action="store_true",
        help="Read upload bytes from stdin instead of --file.",
    )
    upload_file.add_argument("--file-id", required=True, help="Dekart file_id created via MCP create_file.")
    upload_file.add_argument(
        "--name",
        help="Uploaded file name metadata. Defaults to local file basename.",
    )
    upload_file.add_argument(
        "--mime-type",
        help="Uploaded file MIME type. Defaults to inferred type or application/octet-stream.",
    )
    upload_file.add_argument(
        "--max-part-size",
        type=int,
        help="Optional max part size override. If omitted, uses start response value.",
    )
    upload_file.add_argument(
        "--json",
        action="store_true",
        help="Print raw JSON payload.",
    )
    upload_file.add_argument(
        "--out",
        help="Write JSON payload to file path instead of stdout.",
    )

    snapshot_local = subparsers.add_parser(
        "snapshot-local",
        help="Manage local headless snapshot rendering capability.",
    )
    snapshot_local.add_argument(
        "action",
        choices=("status", "install", "uninstall"),
        help="Capability action.",
    )
    snapshot_local.add_argument(
        "--json",
        action="store_true",
        help="Print JSON output.",
    )
    snapshot_local.add_argument(
        "--purge",
        action="store_true",
        help="With uninstall: also run Playwright Chromium uninstall.",
    )
    snapshot_local.add_argument(
        "--debug",
        action="store_true",
        help="Print detailed install/uninstall diagnostics.",
    )

    snapshot = subparsers.add_parser(
        "snapshot",
        help="Render report snapshot PNG (local when enabled; remote when disabled or --remote-only).",
        description=(
            "Render report snapshot PNG. Rendering is local when the local snapshot "
            "capability is enabled, and remote when it is disabled or --remote-only is used."
        ),
    )
    snapshot.add_argument("--report-id", required=True, help="Dekart report id.")
    snapshot.add_argument(
        "--out",
        help="Output PNG path. Defaults to snapshot-<report-id>.png in current directory.",
    )
    snapshot.add_argument(
        "--timeout",
        type=int,
        default=90,
        help="Per-operation timeout seconds for snapshot calls/rendering (default: 90).",
    )
    snapshot.add_argument(
        "--width",
        type=int,
        default=1600,
        help="Local render viewport width (default: 1600).",
    )
    snapshot.add_argument(
        "--height",
        type=int,
        default=900,
        help="Local render viewport height (default: 900).",
    )
    snapshot.add_argument(
        "--zoom",
        type=float,
        help="Snapshot map zoom override (0-24).",
    )
    snapshot.add_argument(
        "--lat",
        type=float,
        help="Snapshot map latitude override. Must be used with --lon.",
    )
    snapshot.add_argument(
        "--lon",
        type=float,
        help="Snapshot map longitude override. Must be used with --lat.",
    )
    snapshot.add_argument(
        "--include-widgets",
        action="store_true",
        help="Render the charts pane; the default is map only.",
    )
    snapshot.add_argument(
        "--remote-only",
        action="store_true",
        help="Skip local rendering and use the remote snapshot endpoint when available.",
    )
    snapshot.add_argument(
        "--json",
        action="store_true",
        help="Print JSON result metadata.",
    )
    snapshot.add_argument(
        "--debug",
        action="store_true",
        help="Print snapshot diagnostics.",
    )

    report_url = subparsers.add_parser(
        "report-url",
        help="Resolve a user-facing Dekart report URL from a report id.",
    )
    report_url.add_argument("--report-id", required=True, help="Dekart report id.")
    report_url.add_argument(
        "--json",
        action="store_true",
        help="Print JSON metadata.",
    )

    run_query = subparsers.add_parser(
        "run-query",
        help="Run an already-prepared Dekart query and download result rows.",
    )
    run_query.add_argument("--query-id", required=True, help="Prepared Dekart query id to run.")
    run_query.add_argument(
        "--params-json",
        default="{}",
        help="Query parameter values as a JSON object.",
    )
    run_query.add_argument(
        "--out-dir",
        help="Directory for downloaded result file. Filename is resolved from job metadata.",
    )
    run_query.add_argument("--out", dest="deprecated_out", help=argparse.SUPPRESS)
    run_query.add_argument(
        "--wait",
        dest="wait",
        action="store_true",
        default=True,
        help="Wait until the query reaches terminal DONE status before download.",
    )
    run_query.add_argument(
        "--no-wait",
        dest="wait",
        action="store_false",
        help="Do not wait; fail unless the first job status is already DONE.",
    )
    run_query.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Timeout seconds when --wait is set (default: 300).",
    )
    run_query.add_argument(
        "--interval",
        type=int,
        default=5,
        help="Polling interval seconds when --wait is set (default: 5).",
    )
    run_query.add_argument(
        "--print",
        dest="print_rows",
        action="store_true",
        help="Print fetched result rows to stdout after download.",
    )
    run_query.add_argument(
        "--json",
        action="store_true",
        help="Print JSON metadata.",
    )

    preview = subparsers.add_parser(
        "preview",
        help="Preview a downloaded Dekart result file.",
    )
    preview.add_argument("file", help="Downloaded parquet or CSV result file.")
    preview.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum rows to print (default: 20).",
    )
    preview.add_argument(
        "--schema",
        action="store_true",
        help="Print column names and types instead of rows.",
    )

    return parser


def telemetry_disabled():
    """Return True if telemetry/version ping is disabled by env."""
    for key in ("DO_NOT_TRACK", "DNT"):
        value = os.environ.get(key, "").strip().lower()
        if value in {"1", "true", "yes", "on"}:
            return True
    return False


def get_installed_version():
    """Return installed package version or 'unknown'."""
    try:
        return metadata.version("dekart")
    except metadata.PackageNotFoundError:
        return "unknown"
    except Exception:
        return "unknown"


def send_version_ping():
    """Send non-blocking CLI version ping to Dekart endpoint."""
    if telemetry_disabled():
        return
    url = os.environ.get("DEKART_VERSION_CHECK_URL", DEFAULT_VERSION_CHECK_URL).strip() or DEFAULT_VERSION_CHECK_URL
    try:
        installation_id = get_installation_id()
        url_parts = urlsplit(url)
        query = parse_qsl(url_parts.query, keep_blank_values=True)
        query.append(("installation_id", installation_id))
        url = urlunsplit((url_parts.scheme, url_parts.netloc, url_parts.path, urlencode(query), url_parts.fragment))
    except Exception:
        # Identity persistence is best effort and must not block the version check.
        pass
    version = get_installed_version()
    req = urllib.request.Request(url=url, method="GET")
    req.add_header("User-Agent", f"dekart-cli/{version}")
    try:
        with urllib.request.urlopen(req, timeout=2):
            return
    except (urllib.error.URLError, TimeoutError, ValueError):
        return
    except Exception:
        return


def get_config_path():
    """Return user config file path for dekart CLI."""
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
    config_root = Path(xdg_config_home) if xdg_config_home else (Path.home() / ".config")
    return config_root / "dekart" / "config.json"


def load_config(path):
    """Load JSON config from disk."""
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_config(path, data):
    """Persist JSON config to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def get_local_snapshot_settings(config):
    """Extract local snapshot settings from config payload."""
    local_snapshot = config.get("local_snapshot", {})
    if not isinstance(local_snapshot, dict):
        local_snapshot = {}
    enabled = bool(local_snapshot.get("enabled", False))
    provider = str(local_snapshot.get("provider", "playwright")).strip() or "playwright"
    installed_at = str(local_snapshot.get("installed_at", "")).strip()
    return {
        "enabled": enabled,
        "provider": provider,
        "installed_at": installed_at,
    }


def update_local_snapshot_settings(enabled, provider="playwright", installed_at=""):
    """Update local snapshot capability flags in config."""
    config_path = get_config_path()
    config = load_config(config_path)
    local_snapshot = config.get("local_snapshot", {})
    if not isinstance(local_snapshot, dict):
        local_snapshot = {}
    local_snapshot["enabled"] = bool(enabled)
    local_snapshot["provider"] = str(provider or "playwright").strip() or "playwright"
    if installed_at:
        local_snapshot["installed_at"] = installed_at
    elif not enabled:
        local_snapshot.pop("installed_at", None)
    config["local_snapshot"] = local_snapshot
    save_config(config_path, config)


def is_valid_http_url(url):
    """Return True when URL uses http/https and has a hostname."""
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def handle_config(url):
    """Set or show configured Dekart URL."""
    config_path = get_config_path()
    config = load_config(config_path)

    if url:
        normalized_url = url.rstrip("/")
        if not is_valid_http_url(normalized_url):
            print("Invalid URL. Use full http(s) URL, for example: http://localhost:3000", file=sys.stderr)
            return 2
        config["dekart_url"] = normalized_url
        save_config(config_path, config)
        print(f"Dekart URL saved: {normalized_url}")
        print(f"Config file: {config_path}")
        return 0

    configured_url = config.get("dekart_url", "").strip()
    effective_url = configured_url or DEFAULT_DEKART_URL
    source = "user_config" if configured_url else "default_cloud"
    print(f"Dekart URL: {effective_url}")
    print(f"Source: {source}")
    print(f"Config file: {config_path}")
    return 0


def get_dekart_url():
    """Resolve configured Dekart URL with cloud default fallback."""
    config_path = get_config_path()
    config = load_config(config_path)
    configured_url = config.get("dekart_url", "").strip()
    return configured_url or DEFAULT_DEKART_URL


def resolve_dekart_url_reference(value, dekart_url=None):
    """Resolve a URL reference returned by Dekart against the configured base URL."""
    url_reference = str(value or "").strip()
    if not url_reference:
        return ""
    base_url = str(dekart_url or get_dekart_url()).strip().rstrip("/") + "/"
    return urljoin(base_url, url_reference)


def get_token_path():
    """Return token storage path for Dekart CLI auth."""
    config_path = get_config_path()
    return config_path.parent / "token.json"


def post_json(url, payload, timeout_seconds=15):
    """Send JSON POST request and return decoded JSON response."""
    request_body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url=url,
        data=request_body,
        headers={"Content-Type": "application/json", "User-Agent": f"dekart-cli/{get_installed_version()}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        body = response.read().decode("utf-8")
        if not body.strip():
            return {}
        return json.loads(body)


def read_json(url, headers=None, timeout_seconds=15):
    """Send JSON GET request and return decoded JSON response."""
    request = urllib.request.Request(
        url=url,
        headers={**(headers or {}), "User-Agent": f"dekart-cli/{get_installed_version()}"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        body = response.read().decode("utf-8")
        if not body.strip():
            return {}
        return json.loads(body)


def save_token(token_path, dekart_url, token_payload):
    """Persist authorized Dekart token payload for future CLI operations."""
    expires_in = int(token_payload.get("expires_in", 0) or 0)
    now = dt.datetime.now(dt.timezone.utc)
    expires_at = now + dt.timedelta(seconds=expires_in) if expires_in > 0 else None
    payload = {
        "dekart_url": dekart_url,
        "token": token_payload.get("token", ""),
        "email": token_payload.get("email", ""),
        "workspace_id": token_payload.get("workspace_id", ""),
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat() if expires_at else None,
    }
    token_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=2) + "\n"

    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{token_path.name}.", dir=str(token_path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            try:
                os.fchmod(tmp_file.fileno(), 0o600)
            except AttributeError:
                pass
            tmp_file.write(serialized)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_name, token_path)
        try:
            os.chmod(token_path, 0o600)
        except OSError:
            pass
    finally:
        if tmp_name and os.path.exists(tmp_name):
            os.unlink(tmp_name)


def load_token(token_path):
    """Load saved token payload for authenticated Dekart API requests."""
    if not token_path.exists():
        return {}
    try:
        payload = json.loads(token_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def get_auth_headers():
    """Build Authorization headers from locally saved device token."""
    token_path = get_token_path()
    payload = load_token(token_path)
    token = str(payload.get("token", "")).strip()
    if not token:
        return None
    return {"Authorization": f"Bearer {token}"}


GOOGLE_ACCESS_TOKEN_HEADER = "X-Dekart-Google-Access-Token"
_google_access_token_cache = {}
_google_token_warning_shown = False


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get_google_bigquery_passthrough_binding():
    """Return the endpoint-scoped local gcloud binding."""
    config = load_config(get_config_path())
    binding = config.get("google_bigquery_passthrough")
    if not isinstance(binding, dict):
        return None
    dekart_url = get_dekart_url().rstrip("/")
    account = str(binding.get("gcloud_account", "")).strip()
    if str(binding.get("dekart_url", "")).strip().rstrip("/") != dekart_url or not account:
        return None
    token_payload = load_token(get_token_path())
    if str(token_payload.get("dekart_url", "")).strip().rstrip("/") != dekart_url:
        return None
    return {"dekart_url": dekart_url, "gcloud_account": account}


def mint_google_access_token(account, force_refresh=False):
    """Mint and process-cache a short-lived gcloud access token."""
    cache_key = str(account).strip().casefold()
    if force_refresh:
        _google_access_token_cache.pop(cache_key, None)
    if not force_refresh and _google_access_token_cache.get(cache_key):
        return _google_access_token_cache[cache_key]
    try:
        result = subprocess.run(
            ["gcloud", "auth", "print-access-token", "--account", account],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            "gcloud could not provide an access token. "
            f"Run `gcloud auth login --account {account}`, then retry."
        ) from exc
    token = (result.stdout or "").strip()
    if result.returncode != 0 or not token or any(character.isspace() for character in token):
        raise RuntimeError(
            "gcloud could not provide an access token. "
            f"Run `gcloud auth login --account {account}`, then retry."
        )
    _google_access_token_cache[cache_key] = token
    return token


def is_google_header_url_allowed(url, dekart_url):
    """Limit the delegated Google token to exact trusted Dekart API URLs."""
    try:
        target = urlsplit(str(url or ""))
        base = urlsplit(str(dekart_url or ""))
        target_port = target.port or (443 if target.scheme == "https" else 80)
        base_port = base.port or (443 if base.scheme == "https" else 80)
    except ValueError:
        return False
    same_origin = (
        target.scheme == base.scheme
        and (target.hostname or "").casefold() == (base.hostname or "").casefold()
        and target_port == base_port
    )
    secure = target.scheme == "https" or (
        target.scheme == "http"
        and target.hostname in {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
    )
    api_base = base.path.rstrip("/")
    mcp_path = f"{api_base}/api/v1/mcp/call"
    dataset_prefix = f"{api_base}/api/v1/dataset-source/"
    allowed_path = target.path == mcp_path or (
        target.path.startswith(dataset_prefix) and bool(target.path[len(dataset_prefix):])
    )
    return (
        same_origin
        and secure
        and allowed_path
        and target.username is None
        and target.password is None
        and not target.query
        and not target.fragment
    )


def get_google_passthrough_headers(url, force_refresh=False):
    """Build the optional delegated credential header for one trusted URL."""
    binding = get_google_bigquery_passthrough_binding()
    if not binding or not is_google_header_url_allowed(url, binding["dekart_url"]):
        return {}
    token = mint_google_access_token(binding["gcloud_account"], force_refresh=force_refresh)
    return {GOOGLE_ACCESS_TOKEN_HEADER: f"Bearer {token}"}


def get_optional_google_passthrough_headers(url, force_refresh=False):
    """Return optional delegated headers without breaking unrelated CLI work."""
    global _google_token_warning_shown
    try:
        return get_google_passthrough_headers(url, force_refresh=force_refresh)
    except RuntimeError as exc:
        if not _google_token_warning_shown:
            print(f"Warning: {exc}", file=sys.stderr)
            _google_token_warning_shown = True
        return {}


def urlopen_without_redirects(request, timeout_seconds):
    """Open a credentialed request while refusing every redirect."""
    opener = urllib.request.build_opener(_RejectRedirects())
    return opener.open(request, timeout=timeout_seconds)


def run_command_capture(cmd):
    """Run subprocess and capture exit code/stdout/stderr as text."""
    result = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
    )
    return {
        "code": result.returncode,
        "stdout": result.stdout or "",
        "stderr": result.stderr or "",
        "cmd": " ".join(cmd),
    }


def run_command_live(cmd):
    """Run subprocess attached to current terminal for interactive progress."""
    result = subprocess.run(
        cmd,
        check=False,
    )
    return {
        "code": result.returncode,
        "stdout": "",
        "stderr": "",
        "cmd": " ".join(cmd),
    }


def check_local_snapshot_runtime():
    """Check whether local Playwright Chromium rendering is available."""
    status = {
        "provider": "playwright",
        "available": False,
        "reason": "",
    }
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception:
        status["reason"] = "Playwright Python package is not installed."
        return status

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        status["available"] = True
        return status
    except Exception as exc:
        status["reason"] = f"Playwright Chromium runtime unavailable: {exc}"
        return status


def install_local_snapshot_capability(debug=False, interactive=False):
    """Install local Playwright Chromium capability for snapshot rendering."""
    runtime = check_local_snapshot_runtime()
    if runtime.get("available"):
        update_local_snapshot_settings(
            enabled=True,
            provider="playwright",
            installed_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        )
        return {
            "ok": True,
            "steps": [],
            "message": "Local snapshot capability already available.",
        }

    steps = []
    install_steps = (
        ("Installing Playwright Python package", [sys.executable, "-m", "pip", "install", "playwright"]),
        ("Installing Chromium browser runtime", [sys.executable, "-m", "playwright", "install", "chromium"]),
    )
    for index, (title, cmd) in enumerate(install_steps, start=1):
        if interactive:
            print(f"[{index}/{len(install_steps)}] {title}...")
            print(f"$ {' '.join(cmd)}")
            step = run_command_live(cmd)
            if debug:
                print(f"exit: {step.get('code')}")
        else:
            step = run_command_capture(cmd)
        steps.append(step)
        if step.get("code") != 0:
            return {
                "ok": False,
                "steps": steps,
                "message": f"Command failed: {step.get('cmd')}",
            }

    runtime = check_local_snapshot_runtime()
    if not runtime.get("available"):
        return {
            "ok": False,
            "steps": steps,
            "message": runtime.get("reason", "Local snapshot runtime check failed."),
        }

    update_local_snapshot_settings(
        enabled=True,
        provider="playwright",
        installed_at=dt.datetime.now(dt.timezone.utc).isoformat(),
    )
    return {
        "ok": True,
        "steps": steps,
        "message": "Local snapshot capability installed.",
    }


def uninstall_local_snapshot_capability(purge=False):
    """Disable local snapshot capability and optionally purge Chromium."""
    purge_step = None
    if purge:
        purge_step = run_command_capture([sys.executable, "-m", "playwright", "uninstall", "chromium"])
    update_local_snapshot_settings(enabled=False, provider="playwright", installed_at="")
    ok = True if purge_step is None else purge_step.get("code") == 0
    return {
        "ok": ok,
        "purge_step": purge_step,
        "message": "Local snapshot capability disabled.",
    }


def download_binary(url, timeout_seconds=30, google_passthrough=False):
    return DekartClient.from_config()._download_binary(url, timeout_seconds, google_passthrough)


def parse_yes_no_input(value, default=True):
    """Parse yes/no interactive input with default fallback."""
    normalized = str(value or "").strip().lower()
    if not normalized:
        return bool(default)
    if normalized in {"y", "yes"}:
        return True
    if normalized in {"n", "no"}:
        return False
    return bool(default)


def is_interactive_terminal():
    """Return True when both stdin/stdout are interactive terminals."""
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


def supports_ansi_colors():
    """Return True when ANSI color output is likely supported."""
    if not sys.stdout.isatty():
        return False
    if os.environ.get("NO_COLOR"):
        return False
    term = os.environ.get("TERM", "").strip().lower()
    return term not in {"", "dumb"}


def ansi_rgb(text, red, green, blue, bold=False):
    """Wrap text with 24-bit ANSI color escape sequence."""
    if not supports_ansi_colors():
        return text
    style = "1;" if bold else ""
    return f"\033[{style}38;2;{red};{green};{blue}m{text}\033[0m"


def print_init_banner():
    """Print Dekart init banner inspired by Dekart logo colors."""
    lines = [
        "  ____       _             _   ",
        " |  _ \\  ___| | ____ _ _ __| |_ ",
        " | | | |/ _ \\ |/ / _` | '__| __|",
        " | |_| |  __/   < (_| | |  | |_ ",
        " |____/ \\___|_|\\_\\__,_|_|   \\__|",
        "                                ",
    ]
    for line in lines:
        print(ansi_rgb(line, 112, 181, 208, bold=True))
    version = get_installed_version()
    subtitle = f"Interactive setup for Dekart CLI v{version}"
    print(ansi_rgb(subtitle, 235, 235, 235, bold=True))
    print()


def read_menu_key():
    """Read one key press and normalize it for interactive menus."""
    if os.name == "nt":
        import msvcrt

        ch = msvcrt.getwch()
        if ch in {"\r", "\n"}:
            return "enter"
        if ch in {"\x00", "\xe0"}:
            ch2 = msvcrt.getwch()
            if ch2 == "H":
                return "up"
            if ch2 == "P":
                return "down"
            return ""
        if ch in {"k", "K"}:
            return "up"
        if ch in {"j", "J"}:
            return "down"
        return ""

    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = os.read(fd, 1)
        if ch == b"\x03":
            raise KeyboardInterrupt
        if ch in {b"\r", b"\n"}:
            return "enter"
        if ch in {b"k", b"K"}:
            return "up"
        if ch in {b"j", b"J"}:
            return "down"
        if ch == b"\x1b":
            ready, _, _ = select.select([sys.stdin], [], [], 0.03)
            if not ready:
                return "cancel"
            seq = os.read(fd, 2)
            if seq == b"[A":
                return "up"
            if seq == b"[B":
                return "down"
            return ""
        return ""
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def select_menu_option(title, options, default_index=0):
    """Render interactive cursor menu and return selected option index."""
    if not is_interactive_terminal() or not options or not supports_ansi_colors():
        return None

    selected = max(0, min(int(default_index), len(options) - 1))
    cancelled = False
    step_color = (112, 181, 208)

    def format_line(index):
        prefix = ">" if index == selected else " "
        line = f"  {prefix} {options[index]}"
        if index == selected:
            return ansi_rgb(line, step_color[0], step_color[1], step_color[2], bold=True)
        return line

    try:
        print(ansi_rgb(title, step_color[0], step_color[1], step_color[2], bold=True))
        print("Use ↑/↓ (or j/k) and Enter.")
        print()
        for idx in range(len(options)):
            print(format_line(idx))
        if supports_ansi_colors():
            sys.stdout.write("\033[?25l")
            sys.stdout.flush()

        while True:
            key = read_menu_key()
            previous = selected
            if key == "up":
                selected = (selected - 1) % len(options)
            elif key == "down":
                selected = (selected + 1) % len(options)
            elif key == "enter":
                break
            elif key == "cancel":
                cancelled = True
                break

            if selected != previous:
                sys.stdout.write(f"\033[{len(options)}F")
                for idx in range(len(options)):
                    sys.stdout.write("\033[2K\r" + format_line(idx) + "\n")
                sys.stdout.flush()
    except Exception:
        return None
    finally:
        if supports_ansi_colors():
            sys.stdout.write("\033[?25h")
            sys.stdout.flush()
    print()
    if cancelled:
        return "cancel"
    return selected


def save_dekart_url(url):
    """Persist Dekart URL into CLI config."""
    normalized_url = str(url or "").strip().rstrip("/")
    config_path = get_config_path()
    config = load_config(config_path)
    config["dekart_url"] = normalized_url
    save_config(config_path, config)


def configure_google_bigquery_passthrough(mode, dekart_url, interactive):
    """Configure the non-secret local gcloud binding created by dekart init."""
    config_path = get_config_path()
    config = load_config(config_path)
    binding_key = "google_bigquery_passthrough"
    selected_mode = str(mode or "ask").strip().lower()

    if selected_mode == "disable":
        config.pop(binding_key, None)
        save_config(config_path, config)
        print("BigQuery passthrough: disabled")
        return True

    if selected_mode == "ask" and not interactive:
        binding = get_google_bigquery_passthrough_binding()
        if binding:
            print(f"BigQuery passthrough: enabled with {binding['gcloud_account']}")
        else:
            print("BigQuery passthrough: disabled")
            print("Enable later with: dekart init --bigquery-passthrough enable")
        return True

    if selected_mode == "ask":
        print("BigQuery passthrough can use your local gcloud login.")
        print("Dekart will send a short-lived cloud-platform token only to this Dekart endpoint.")
        if not parse_yes_no_input(input("Enable BigQuery passthrough? [y/N]: "), default=False):
            config.pop(binding_key, None)
            save_config(config_path, config)
            print("BigQuery passthrough: disabled")
            return True

    config.pop(binding_key, None)
    save_config(config_path, config)

    if shutil.which("gcloud") is None:
        print("BigQuery passthrough setup failed: gcloud is not installed.", file=sys.stderr)
        print("Install Google Cloud CLI, run `gcloud auth login`, then rerun `dekart init`.", file=sys.stderr)
        return False

    try:
        account_result = subprocess.run(
            ["gcloud", "config", "get-value", "account", "--quiet"], check=False,
            capture_output=True, text=True, timeout=15,
        )
        auth_result = subprocess.run(
            ["gcloud", "auth", "list", "--format=value(account)"], check=False,
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        print("BigQuery passthrough setup failed: gcloud account lookup failed.", file=sys.stderr)
        print("Run `gcloud auth login`, then rerun `dekart init`.", file=sys.stderr)
        return False
    active_account = (account_result.stdout or "").strip() if account_result.returncode == 0 else ""
    user_accounts = [
        account for value in (auth_result.stdout or "").splitlines()
        if (account := value.strip()) and not account.casefold().endswith(".gserviceaccount.com")
    ]
    account = next(
        (value for value in user_accounts if value.casefold() == active_account.casefold()),
        "",
    )
    if not account and len(user_accounts) == 1:
        account = user_accounts[0]
    elif not account and len(user_accounts) > 1 and interactive:
        selection = select_menu_option(
            title="Choose gcloud account for BigQuery passthrough",
            options=user_accounts,
            default_index=0,
        )
        if isinstance(selection, int):
            account = user_accounts[selection]
        elif selection is None:
            for index, value in enumerate(user_accounts, start=1):
                print(f"  {index}) {value}")
            choice = input(f"Choose [1-{len(user_accounts)}]: ").strip()
            if choice.isdigit() and 1 <= int(choice) <= len(user_accounts):
                account = user_accounts[int(choice) - 1]
    if auth_result.returncode != 0 or not account:
        print("BigQuery passthrough setup failed: choose an authenticated gcloud user account.", file=sys.stderr)
        print("Run `gcloud auth login`, then rerun `dekart init` interactively.", file=sys.stderr)
        return False
    print(f"Using authenticated gcloud account {account}; active account remains unchanged.")

    try:
        impersonation_result = subprocess.run(
            ["gcloud", "config", "get-value", "auth/impersonate_service_account", "--quiet"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        impersonation_result = None
    if impersonation_result is None:
        print("BigQuery passthrough setup failed: gcloud configuration lookup failed.", file=sys.stderr)
        return False
    if impersonation_result.returncode != 0:
        print("BigQuery passthrough setup failed: gcloud configuration lookup failed.", file=sys.stderr)
        return False
    impersonated_account = (impersonation_result.stdout or "").strip()
    if impersonated_account not in {"", "(unset)"}:
        print("BigQuery passthrough setup failed: gcloud service-account impersonation is enabled.", file=sys.stderr)
        print("Unset auth/impersonate_service_account, then rerun `dekart init`.", file=sys.stderr)
        return False
    try:
        mint_google_access_token(account, force_refresh=True)
    except RuntimeError as exc:
        print(f"BigQuery passthrough setup failed: {exc}", file=sys.stderr)
        return False
    _google_access_token_cache.clear()
    config[binding_key] = {
        "dekart_url": str(dekart_url).rstrip("/"),
        "gcloud_account": account,
    }
    save_config(config_path, config)
    print(f"BigQuery passthrough: enabled with {account}")
    return True


def _select_local_action(options, default_index=0):
    labels = [label for _action, label in options]
    cursor_selection = select_menu_option(
        title="Choose how to use Local Dekart",
        options=labels,
        default_index=default_index,
    )
    if cursor_selection == "cancel":
        return "cancel"
    if cursor_selection is None:
        for index, label in enumerate(labels, start=1):
            print("  {0}) {1}".format(index, label))
        print()
        default_choice = str(default_index + 1)
        choice = input("Choose [1-{0}] (default: {1}): ".format(len(options), default_choice)).strip()
        if not choice:
            choice = default_choice
        if not choice.isdigit() or not 1 <= int(choice) <= len(options):
            print("Unknown choice, using the default.")
            choice = default_choice
        return options[int(choice) - 1][0]
    return options[cursor_selection][0]


def _manual_local_command(port):
    if port:
        return local_docker.standalone_docker_run_command(port)
    return None


def prompt_local_dekart_url(docker_state="running"):
    """Offer explicit start, connect, and manual paths for Local Dekart."""
    status = local_docker.get_status()
    state = status.get("status")
    detected_port = status.get("port")
    can_manage_docker = docker_state == "running"
    if state == "absent":
        free_port = detected_port
    elif state == "external":
        free_port = local_docker.first_available_port()
    elif state == "docker_unavailable":
        free_port = local_docker.first_available_port()
    elif state == "stopped":
        free_port = detected_port
    else:
        free_port = local_docker.first_available_port()
    options = []

    if can_manage_docker and state == "stopped" and detected_port:
        options.append(("start", "Start existing local Dekart Docker now on port {0}".format(detected_port)))
    elif can_manage_docker and state == "unhealthy" and detected_port:
        options.append(("start", "Wait for local Dekart Docker on port {0}".format(detected_port)))
    elif can_manage_docker and state not in {"running", "ownership_conflict"} and free_port:
        options.append(("start", "Start new local Dekart Docker now on port {0}".format(free_port)))

    if state in {"running", "external"} and detected_port:
        options.append(("connect", "Connect to running Dekart on port {0}".format(detected_port)))

    options.append(("manual", "I will start or connect to Dekart myself"))
    if not can_manage_docker:
        options.append(("back", "Back to backend choices"))
    action = _select_local_action(options)
    if action == "cancel":
        print("Local setup cancelled.")
        return None
    if action == "back":
        return "back"

    if action == "connect":
        print("Using running Dekart at {0}.".format(status["url"]))
        return status["url"]

    if action == "start":
        result = local_docker.up(
            preferred_port=free_port,
            reuse_external=state != "external",
            on_execute=local_docker.print_executing,
        )
        local_docker.print_result(result)
        if result["code"] != 0:
            raise LocalSetupError(result["code"])
        local_docker.print_management_commands()
        return result["status"]["url"]

    planned_port = free_port or detected_port
    command = _manual_local_command(planned_port)
    if command:
        print("Run this command in another terminal:")
        print("  {0}".format(local_docker.format_command(command)))
    elif state == "ownership_conflict":
        print(status.get("message", "The dekart-local container is not managed by this CLI."))
        print("The CLI will not modify it.")

    default_url = local_docker.local_url(planned_port) if planned_port else status.get("url", "")
    while True:
        prompt = "Enter Dekart URL"
        if default_url:
            prompt += " (default: {0})".format(default_url)
        entered_url = input(prompt + ": ").strip().rstrip("/")
        selected_url = entered_url or default_url
        if is_valid_http_url(selected_url):
            break
        print("Invalid URL. Example: http://localhost:8080")

    parsed_url = urlparse(selected_url)
    is_local_url = parsed_url.hostname in {"localhost", "127.0.0.1", "::1"}
    if is_local_url and not local_docker.is_dekart_endpoint(selected_url):
        print("Dekart is not reachable at {0} yet.".format(selected_url))
        print("Setup paused. Start Dekart, then run dekart init again.")
        return None
    if is_local_url:
        print("Connected to Dekart at {0}.".format(selected_url))
    else:
        print("Using Dekart endpoint: {0}.".format(selected_url))
    return selected_url


def prompt_init_dekart_url():
    """Prompt user to select Dekart endpoint for init flow."""
    print(ansi_rgb("[Step 1 of 4] Map backend", 112, 181, 208, bold=True))
    print("Dekart turns your SQL results into maps. Where should it run?")
    current_url = get_dekart_url().rstrip("/")
    default_index = 1
    custom_default = ""
    if current_url and current_url != DEFAULT_DEKART_URL and not current_url.startswith("http://localhost:"):
        custom_default = current_url
    while True:
        docker_state = local_docker.docker_availability()
        local_label = "Local (Docker)   On your machine. Data stays local."
        if docker_state == "missing":
            local_label = "Local (Docker required)   Install Docker to run on your machine."
        elif docker_state == "stopped":
            local_label = "Local (start Docker)   Docker is installed but not running."
        options = [
            local_label,
            "Dekart Cloud     Fastest. Sign in, nothing to install.",
            "Self-hosted      I already run Dekart somewhere.",
        ]
        cursor_selection = select_menu_option(
            title="Select backend option",
            options=options,
            default_index=default_index,
        )
        if cursor_selection == "cancel":
            selected_url = current_url or DEFAULT_DEKART_URL
            print("Selection cancelled. Keeping current endpoint.")
            print(f"Selected endpoint: {selected_url}")
            print("You can change it later with: dekart config --url <URL>")
            print()
            return selected_url

        if cursor_selection is None:
            print(f"  1) {options[0]}")
            print(f"  2) {options[1]}")
            print(f"  3) {options[2]}")
            print()
            choice = input("Choose [1-3] (default: 2): ").strip() or "2"
            if choice not in {"1", "2", "3"}:
                print(f"Unknown choice '{choice}', defaulting to Dekart Cloud.")
                choice = "2"
        else:
            choice = str(cursor_selection + 1)

        if choice != "1":
            break
        if docker_state != "running":
            print(local_docker.docker_guidance(docker_state))
            print("You can install/start Docker, enter another Dekart URL, or go back.")
            print()
        selected_url = prompt_local_dekart_url(docker_state=docker_state)
        if selected_url == "back":
            continue
        if not selected_url:
            return None
        break

    if choice == "3":
        while True:
            if custom_default:
                prompt = f"Enter custom http(s) URL (default: {custom_default}): "
                custom = input(prompt).strip().rstrip("/")
                if not custom:
                    custom = custom_default
            else:
                custom = input("Enter custom http(s) URL: ").strip().rstrip("/")
            if is_valid_http_url(custom):
                selected_url = custom
                break
            print("Invalid URL. Example: https://my-dekart.company.com")
    elif choice == "2":
        selected_url = DEFAULT_DEKART_URL

    print(f"Selected endpoint: {selected_url}")
    print("This endpoint will be saved after successful authorization.")
    print("You can change it later with: dekart config --url <URL>")
    print()
    return selected_url


def pretty_print_json(payload):
    """Print stable JSON for CLI output."""
    print(json.dumps(payload, indent=2, sort_keys=True))


def write_json_file(path, payload):
    """Write pretty JSON payload to file path."""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_text_file(path, content):
    """Write plain text content to file path."""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def format_scalar(value):
    """Format scalar JSON value as plain text."""
    if isinstance(value, bool):
        return "true"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    raise ValueError("Extracted value is not scalar.")


def parse_optional_float(value):
    """Convert optional CLI float values while preserving omitted arguments."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def is_valid_finite_number(value, minimum, maximum):
    """Return true for finite map viewport numbers inside the server-supported range."""
    return value is not None and math.isfinite(value) and minimum <= value <= maximum


def resolve_call_args_source(args_json, args_file, args_stdin):
    """Resolve raw --args JSON content from inline, file, @file, or stdin."""
    inline = str(args_json or "").strip()
    file_arg = str(args_file or "").strip()

    if args_stdin and file_arg:
        raise ValueError("Use only one source: --args-stdin or --args-file.")
    if args_stdin and inline not in {"", "{}"}:
        raise ValueError("Use --args-stdin without --args inline JSON.")
    if file_arg and inline not in {"", "{}"}:
        raise ValueError("Use --args-file without --args inline JSON.")

    if args_stdin:
        return sys.stdin.read()
    if file_arg:
        return Path(file_arg).expanduser().read_text(encoding="utf-8")
    if inline.startswith("@"):
        path = inline[1:].strip()
        if not path:
            raise ValueError("Invalid --args value: @<file-path> expected.")
        return Path(path).expanduser().read_text(encoding="utf-8")
    return inline or "{}"


def mcp_call(name, args, timeout_seconds=30, return_metadata=False):
    client = DekartClient.from_config()
    if return_metadata:
        return client._call(name, args, timeout_seconds)
    return client.call(name, args, timeout_seconds)


def parse_upload_start_payload(start_response_json, start_response_file):
    """Parse start_file_upload_session JSON and return normalized result object."""
    raw_payload = None
    if start_response_json:
        raw_payload = start_response_json
    elif start_response_file:
        try:
            raw_payload = Path(start_response_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"Failed to read --start-response-file: {exc}") from exc
    if raw_payload is None:
        return {}
    try:
        parsed = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid start response JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Start response JSON must be an object.")
    result = parsed.get("result")
    if isinstance(result, dict):
        return result
    return parsed


def upload_part_binary(put_url, body_bytes, required_headers):
    return DekartClient.from_config()._upload_part_binary(put_url, body_bytes, required_headers)


def upload_parts_from_start_result(local_file, start_result, upload_part_endpoint, required_headers_json, max_part_size, file_id):
    with local_file.open("rb") as source:
        return client_operations._upload_parts_from_stream(
            DekartClient.from_config(), source, local_file.stat().st_size,
            start_result, upload_part_endpoint, required_headers_json, max_part_size, file_id,
        )


def handle_tools(raw_json, names_only, match, schema_name, arg_keys):
    """Fetch MCP tool catalog from configured Dekart instance."""
    if schema_name and names_only:
        print("Use either --schema or --names, not both.", file=sys.stderr)
        return 2
    if raw_json and (names_only or arg_keys):
        print("Use --json alone (optionally with --match/--schema), not with --names/--arg-keys.", file=sys.stderr)
        return 2
    try:
        payload = fetch_mcp_tools_payload()
    except urllib.error.HTTPError as exc:
        print(f"MCP tools request failed ({exc.code}): {exc.reason}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"MCP tools request failed: {exc}", file=sys.stderr)
        return 1

    tools = payload.get("tools", [])
    if not isinstance(tools, list):
        print("Invalid MCP tools response.", file=sys.stderr)
        return 1

    if match:
        needle = str(match).lower()
        tools = [item for item in tools if needle in str(item.get("name", "")).lower()]

    if schema_name:
        selected = next((item for item in tools if str(item.get("name", "")).strip() == schema_name), None)
        if not selected:
            print(f"Tool not found: {schema_name}", file=sys.stderr)
            return 1
        if raw_json:
            pretty_print_json(selected)
            return 0
        print(str(selected.get("name", "")).strip())
        pretty_print_json(selected.get("inputSchema", {}))
        return 0

    if raw_json:
        filtered_payload = dict(payload)
        filtered_payload["tools"] = tools
        pretty_print_json(filtered_payload)
        return 0

    if names_only:
        for item in tools:
            name = str(item.get("name", "")).strip()
            if name:
                print(name)
        return 0

    if arg_keys:
        for item in tools:
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            props = item.get("inputSchema", {}).get("properties", {})
            keys = sorted(props.keys()) if isinstance(props, dict) else []
            print(f"{name}: {', '.join(keys)}")
        return 0

    print(f"MCP tools: {len(tools)}")
    for item in tools:
        name = str(item.get("name", "")).strip()
        description = str(item.get("description", "")).strip()
        if name:
            if description:
                print(f"- {name}: {description}")
            else:
                print(f"- {name}")
    return 0


def fetch_mcp_tools_payload():
    return DekartClient.from_config().list_tools()


def tool_required_fields(tool):
    """Return required input schema fields for one MCP tool definition."""
    schema = tool.get("inputSchema", {}) if isinstance(tool, dict) else {}
    required = schema.get("required", []) if isinstance(schema, dict) else []
    return set(required) if isinstance(required, list) else set()


def pick_mcp_tool_name(tools, required_fields=(), required_props=(), preferred_names=(), name_contains=()):
    """Resolve one MCP tool name by schema constraints and optional preferred names."""
    required_fields = set(required_fields)
    required_props = set(required_props)
    name_contains = tuple(str(token).lower() for token in name_contains)

    def collect(filter_by_name):
        matches = []
        for tool in tools:
            name = str(tool.get("name", "")).strip()
            if not name:
                continue
            req = tool_required_fields(tool)
            props = tool_properties(tool)
            if not required_fields.issubset(req) or not required_props.issubset(props):
                continue
            if filter_by_name:
                lowered = name.lower()
                if not all(token in lowered for token in name_contains):
                    continue
            matches.append(name)
        return matches

    candidates = collect(filter_by_name=bool(name_contains))
    if not candidates and name_contains:
        candidates = collect(filter_by_name=False)

    for preferred in preferred_names:
        if preferred in candidates:
            return preferred
    return candidates[0] if candidates else ""


def resolve_dekart_tool_mapping(tools):
    """Resolve canonical report->dataset->file tool mapping from MCP tool list."""
    mapping = {
        "create_report_tool": pick_mcp_tool_name(
            tools,
            required_fields=(),
            required_props=(),
            preferred_names=("create_report",),
            name_contains=("report",),
        ),
        "create_dataset_tool": pick_mcp_tool_name(
            tools,
            required_fields=("report_id",),
            required_props=("report_id",),
            preferred_names=("create_dataset",),
            name_contains=("dataset",),
        ),
        "create_file_tool": pick_mcp_tool_name(
            tools,
            required_fields=("dataset_id",),
            required_props=("dataset_id",),
            preferred_names=("create_file",),
            name_contains=("file",),
        ),
    }
    missing = [key for key, value in mapping.items() if not str(value).strip()]
    if missing:
        raise ValueError(f"Missing required MCP tools for workflow: {', '.join(missing)}")
    return mapping


def render_shell_exports(mapping):
    """Render mapping as shell export lines safe for eval/source."""
    lines = []
    for key, value in mapping.items():
        env_key = key.upper()
        lines.append(f"export {env_key}={json.dumps(value)}")
    return "\n".join(lines) + "\n"


def handle_resolve_tools(raw_json, shell, out):
    """Resolve required MCP tool names and print as JSON or shell exports."""
    if raw_json and shell:
        print("Use either --json or --shell, not both.", file=sys.stderr)
        return 2
    try:
        payload = fetch_mcp_tools_payload()
        tools = payload.get("tools", [])
        if not isinstance(tools, list):
            print("Invalid MCP tools response.", file=sys.stderr)
            return 1
        mapping = resolve_dekart_tool_mapping(tools)
    except ValueError as exc:
        print(f"Tool resolution failed: {exc}", file=sys.stderr)
        return 1
    except urllib.error.HTTPError as exc:
        print(f"MCP tools request failed ({exc.code}): {exc.reason}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"MCP tools request failed: {exc}", file=sys.stderr)
        return 1

    try:
        if shell:
            shell_text = render_shell_exports(mapping)
            if out:
                write_text_file(out, shell_text)
            else:
                print(shell_text, end="")
            return 0
        if raw_json:
            if out:
                write_json_file(out, mapping)
            else:
                pretty_print_json(mapping)
            return 0
        lines = [f"{key}: {value}" for key, value in mapping.items()]
        text = "\n".join(lines) + "\n"
        if out:
            write_text_file(out, text)
        else:
            print(text, end="")
        return 0
    except OSError as exc:
        print(f"Failed to write output file: {exc}", file=sys.stderr)
        return 1


def handle_call(name, args_json, args_file, args_stdin, timeout, debug, raw_json, extract, out):
    """Call one MCP tool using dynamic tool name and JSON arguments."""
    try:
        raw_args = resolve_call_args_source(args_json, args_file, args_stdin)
    except OSError as exc:
        print(f"Failed to read arguments source: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"Invalid arguments source: {exc}", file=sys.stderr)
        return 2

    try:
        parsed_args = json.loads(raw_args)
    except json.JSONDecodeError as exc:
        print(f"Invalid --args JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(parsed_args, dict):
        print("Invalid --args JSON: must be an object.", file=sys.stderr)
        return 2

    timeout_seconds = parse_int(timeout, 30)
    if timeout_seconds <= 0:
        print("Invalid --timeout: must be a positive integer.", file=sys.stderr)
        return 2

    dekart_url = get_dekart_url().rstrip("/")
    endpoint = f"{dekart_url}/api/v1/mcp/call"
    if debug:
        print(f"[debug] endpoint={endpoint}", file=sys.stderr)
        print(f"[debug] tool={name}", file=sys.stderr)
        print(f"[debug] timeout_seconds={timeout_seconds}", file=sys.stderr)

    try:
        if debug:
            payload, meta = mcp_call(name, parsed_args, timeout_seconds=timeout_seconds, return_metadata=True)
            request_id = str(meta.get("request_id", "")).strip()
            if request_id:
                print(f"[debug] request_id={request_id}", file=sys.stderr)
        else:
            payload = mcp_call(name, parsed_args, timeout_seconds=timeout_seconds)
    except urllib.error.HTTPError as exc:
        print(f"MCP call failed ({exc.code}): {exc.reason}", file=sys.stderr)
        request_id = request_id_from_headers(getattr(exc, "headers", None))
        if request_id:
            print(f"request_id: {request_id}", file=sys.stderr)
        raw_body, parsed_body = parse_http_error_body(exc)
        fields = extract_structured_error_fields(parsed_body)
        for key in ("error", "message", "path", "hint"):
            value = fields.get(key, "").strip()
            if value:
                print(f"{key}: {value}", file=sys.stderr)
        if raw_body and not fields:
            print(f"response: {raw_body}", file=sys.stderr)
        elif raw_body and debug:
            print(f"[debug] response_body: {raw_body}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"MCP call failed: {exc}", file=sys.stderr)
        return 1

    try:
        if extract:
            value = extract_json_path(payload, extract)
            scalar = format_scalar(value)
            if out:
                write_text_file(out, scalar + "\n")
            else:
                print(scalar)
            return 0

        output_payload = payload if raw_json else payload.get("result", {})
        if out:
            write_json_file(out, output_payload)
        else:
            pretty_print_json(output_payload)
        return 0
    except ValueError as exc:
        print(f"Invalid extract/output request: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"Failed to write output file: {exc}", file=sys.stderr)
        return 1


def handle_upload_parts(
    file_path,
    start_response_json,
    start_response_file,
    upload_part_endpoint,
    required_headers_json,
    max_part_size,
    file_id,
    complete_args_out,
    raw_json,
):
    """Upload local file chunks using start session metadata prepared by MCP."""
    local_file = Path(file_path)
    if not local_file.exists() or not local_file.is_file():
        print(f"File not found: {local_file}", file=sys.stderr)
        return 2
    file_size = local_file.stat().st_size
    if file_size <= 0:
        print("File is empty.", file=sys.stderr)
        return 2

    try:
        start_result = parse_upload_start_payload(start_response_json, start_response_file)
        payload = upload_parts_from_start_result(
            local_file=local_file,
            start_result=start_result,
            upload_part_endpoint=upload_part_endpoint,
            required_headers_json=required_headers_json,
            max_part_size=max_part_size,
            file_id=file_id,
        )
        if complete_args_out:
            write_json_file(complete_args_out, payload.get("complete_args", {}))
        if raw_json:
            pretty_print_json(payload)
        else:
            print(f"Uploaded parts: {payload.get('parts_uploaded', 0)}")
            print(f"Total size: {payload.get('total_size', file_size)} bytes")
            pretty_print_json(payload.get("complete_args", {}))
        return 0
    except ValueError as exc:
        print(f"Invalid upload arguments: {exc}", file=sys.stderr)
        return 2
    except urllib.error.HTTPError as exc:
        print(f"Upload part request failed ({exc.code}): {exc.reason}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Failed to write output file: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Upload failed: {exc}", file=sys.stderr)
        return 1


def resolve_upload_file_name(source_name, explicit_name):
    """Resolve uploaded file name from explicit value or source name fallback."""
    name = str(explicit_name or "").strip()
    if name:
        return name
    return source_name


def resolve_upload_mime_type(source_name, explicit_mime_type):
    """Resolve MIME type from explicit value or source name extension."""
    mime_type = str(explicit_mime_type or "").strip()
    if mime_type:
        return mime_type
    guessed, _ = mimetypes.guess_type(str(source_name))
    return guessed or "application/octet-stream"


def stage_upload_source(file_path, use_stdin):
    """Stage upload source as local file path, using temp file for stdin."""
    if bool(use_stdin) == bool(file_path):
        raise ValueError("Use exactly one input source: --file or --stdin.")

    if use_stdin:
        staged_file = tempfile.NamedTemporaryFile(prefix="dekart-upload-", suffix=".bin", delete=False)
        total_size = 0
        try:
            chunk_size = 4 * 1024 * 1024
            while True:
                chunk = sys.stdin.buffer.read(chunk_size)
                if not chunk:
                    break
                total_size += len(chunk)
                staged_file.write(chunk)
            staged_file.flush()
        except Exception:
            Path(staged_file.name).unlink(missing_ok=True)
            raise
        finally:
            staged_file.close()
        if total_size <= 0:
            Path(staged_file.name).unlink(missing_ok=True)
            raise ValueError("Stdin is empty.")
        return Path(staged_file.name), "stdin-upload.bin", True

    local_file = Path(file_path)
    if not local_file.exists() or not local_file.is_file():
        raise ValueError(f"File not found: {local_file}")
    file_size = local_file.stat().st_size
    if file_size <= 0:
        raise ValueError("File is empty.")
    return local_file, local_file.name, False


def handle_upload_file(file_path, use_stdin, file_id, name, mime_type, max_part_size, raw_json, out):
    """Upload one file end-to-end using MCP start/complete and CLI part PUT automation."""
    staged_file = None
    cleanup_staged_file = False
    try:
        staged_file, source_name, cleanup_staged_file = stage_upload_source(file_path, use_stdin)
    except ValueError as exc:
        print(f"Invalid upload-file arguments: {exc}", file=sys.stderr)
        return 2
    total_size = staged_file.stat().st_size

    try:
        start_args = {
            "file_id": str(file_id).strip(),
            "name": resolve_upload_file_name(source_name, name),
            "mime_type": resolve_upload_mime_type(source_name, mime_type),
            "total_size": total_size,
        }
        client = DekartClient.from_config()
        start_payload = client.call("start_file_upload_session", start_args, timeout_seconds=30)
        start_result = start_payload.get("result", {}) if isinstance(start_payload, dict) else {}
        if not isinstance(start_result, dict):
            raise ValueError("Invalid start_file_upload_session response.")

        with staged_file.open("rb") as source:
            upload_payload = client_operations._upload_parts_from_stream(
                client, source, total_size, start_result, "", "", max_part_size, file_id,
            )
        complete_args = upload_payload.get("complete_args", {})
        complete_payload = client.call("complete_file_upload_session", complete_args, timeout_seconds=60)
        complete_result = complete_payload.get("result", complete_payload) if isinstance(complete_payload, dict) else complete_payload

        payload = {
            "start": start_result,
            "upload": upload_payload,
            "complete": complete_result,
        }
        if out:
            write_json_file(out, payload)
        if raw_json:
            pretty_print_json(payload)
        elif not out:
            print(f"Uploaded parts: {upload_payload.get('parts_uploaded', 0)}")
            print(f"Total size: {upload_payload.get('total_size', total_size)} bytes")
            pretty_print_json(complete_result)
        return 0
    except ValueError as exc:
        print(f"Invalid upload-file arguments: {exc}", file=sys.stderr)
        return 2
    except urllib.error.HTTPError as exc:
        print(f"Upload-file request failed ({exc.code}): {exc.reason}", file=sys.stderr)
        request_id = request_id_from_headers(getattr(exc, "headers", None))
        if request_id:
            print(f"request_id: {request_id}", file=sys.stderr)
        raw_body, parsed_body = parse_http_error_body(exc)
        fields = extract_structured_error_fields(parsed_body)
        for key in ("error", "message", "path", "hint"):
            value = fields.get(key, "").strip()
            if value:
                print(f"{key}: {value}", file=sys.stderr)
        if exc.code == 413:
            print("Upload rejected by Dekart size limit. Reduce file size or increase server upload limit.", file=sys.stderr)
        if raw_body and not fields:
            print(f"response: {raw_body}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Failed to write output file: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Upload-file failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if cleanup_staged_file and staged_file is not None:
            try:
                staged_file.unlink(missing_ok=True)
            except OSError:
                pass


def handle_snapshot_local(action, raw_json, purge, debug):
    """Handle local snapshot capability status/install/uninstall."""
    config = load_config(get_config_path())
    settings = get_local_snapshot_settings(config)
    runtime = check_local_snapshot_runtime()

    if action == "status":
        payload = {
            "enabled": settings.get("enabled", False),
            "provider": settings.get("provider", "playwright"),
            "installed_at": settings.get("installed_at", ""),
            "runtime_available": runtime.get("available", False),
            "runtime_reason": runtime.get("reason", ""),
        }
        if raw_json:
            pretty_print_json(payload)
        else:
            print(f"Enabled: {payload['enabled']}")
            print(f"Provider: {payload['provider']}")
            print(f"Runtime available: {payload['runtime_available']}")
            if payload.get("runtime_reason"):
                print(f"Runtime note: {payload['runtime_reason']}")
        if debug:
            print(
                "[debug] snapshot_local_action=status enabled={0} runtime_available={1}".format(
                    str(bool(payload["enabled"])).lower(),
                    str(bool(payload["runtime_available"])).lower(),
                ),
                file=sys.stderr,
            )
        return 0

    if action == "install":
        interactive_install = bool(not raw_json and sys.stdin.isatty() and sys.stdout.isatty())
        if interactive_install:
            print("Preparing local snapshot capability install.")
            print("This installs Playwright and Chromium for local headless snapshot rendering.")
            print()
        result = install_local_snapshot_capability(debug=debug, interactive=interactive_install)
        if raw_json:
            payload = {
                "action": "install",
                "ok": bool(result.get("ok")),
                "message": result.get("message", ""),
                "steps": result.get("steps", []),
            }
            pretty_print_json(payload)
        else:
            print(result.get("message", ""))
            if debug and not interactive_install:
                for step in result.get("steps", []):
                    print(f"command: {step.get('cmd')}")
                    print(f"exit: {step.get('code')}")
                    if step.get("stdout"):
                        print(step.get("stdout").rstrip())
                    if step.get("stderr"):
                        print(step.get("stderr").rstrip(), file=sys.stderr)
            if not result.get("ok"):
                print("Install failed. You can retry with `dekart snapshot-local install --debug`.", file=sys.stderr)
        if debug:
            print(
                "[debug] snapshot_local_action=install ok={0} steps={1}".format(
                    str(bool(result.get("ok"))).lower(),
                    len(result.get("steps", [])),
                ),
                file=sys.stderr,
            )
        return 0 if result.get("ok") else 1

    if action == "uninstall":
        result = uninstall_local_snapshot_capability(purge=purge)
        purge_step = result.get("purge_step")
        if raw_json:
            payload = {
                "action": "uninstall",
                "ok": bool(result.get("ok")),
                "message": result.get("message", ""),
                "purge": purge,
                "purge_step": purge_step,
            }
            pretty_print_json(payload)
        else:
            print(result.get("message", ""))
            if purge_step and debug:
                print(f"command: {purge_step.get('cmd')}")
                print(f"exit: {purge_step.get('code')}")
                if purge_step.get("stdout"):
                    print(purge_step.get("stdout").rstrip())
                if purge_step.get("stderr"):
                    print(purge_step.get("stderr").rstrip(), file=sys.stderr)
            print("To re-enable later run: dekart snapshot-local install")
            if purge and purge_step and purge_step.get("code") != 0:
                print("Chromium purge failed. Capability is disabled, but browser uninstall failed.", file=sys.stderr)
        if debug:
            purge_code = purge_step.get("code") if purge_step else "not-run"
            print(
                "[debug] snapshot_local_action=uninstall ok={0} purge={1} purge_exit={2}".format(
                    str(bool(result.get("ok"))).lower(),
                    str(bool(purge)).lower(),
                    purge_code,
                ),
                file=sys.stderr,
            )
        return 0 if result.get("ok") else 1

    print(f"Unknown action: {action}", file=sys.stderr)
    return 2


def resolve_snapshot_output_path(report_id, out):
    """Resolve PNG output path for snapshot command."""
    default_name = f"snapshot-{report_id}.png"
    return Path(out or default_name).expanduser()


def build_snapshot_viewport_args(zoom=None, lat=None, lon=None):
    """Validate optional agent-facing snapshot viewport overrides."""
    viewport = {}
    zoom_value = parse_optional_float(zoom)
    lat_value = parse_optional_float(lat)
    lon_value = parse_optional_float(lon)

    if zoom is not None:
        if not is_valid_finite_number(zoom_value, 0, 24):
            raise ValueError("Invalid --zoom: must be between 0 and 24.")
        viewport["zoom"] = zoom_value

    has_lat = lat is not None
    has_lon = lon is not None
    if has_lat != has_lon:
        raise ValueError("Invalid --lat/--lon: pass both latitude and longitude.")
    if has_lat:
        if not is_valid_finite_number(lat_value, -90, 90):
            raise ValueError("Invalid --lat: must be between -90 and 90.")
        if not is_valid_finite_number(lon_value, -180, 180):
            raise ValueError("Invalid --lon: must be between -180 and 180.")
        viewport["lat"] = lat_value
        viewport["lon"] = lon_value

    return viewport


def print_local_snapshot_failure_guidance(error, snapshot_url, timeout_seconds):
    """Print recovery actions appropriate to a classified local failure."""
    if not isinstance(error, LocalSnapshotRenderError):
        return

    if error.retry_skipped_reason == "expired":
        print_snapshot_stderr(
            "Local snapshot retry skipped because the render URL expired."
        )
    elif error.retry_skipped_reason == "unverified_lifetime":
        print_snapshot_stderr(
            "Local snapshot retry skipped because the render URL lifetime could not be verified."
        )

    if error.timed_out:
        if snapshot_url:
            print_snapshot_stderr(
                "For reliability, rerun with the argument change: add `--remote-only`."
            )
        else:
            print_snapshot_stderr(
                "Remote snapshot capture is unavailable for this Dekart instance."
            )
        print_snapshot_stderr(
            "To keep rendering locally, add or replace `--timeout` with "
            f"`--timeout {timeout_seconds * 2}`."
        )
        return

    if error.retryable:
        if snapshot_url:
            print_snapshot_stderr(
                "Rerun with the argument change: add `--remote-only`."
            )
        else:
            print_snapshot_stderr(
                "Remote snapshot capture is unavailable for this Dekart instance."
            )


def handle_snapshot(
    report_id,
    out,
    timeout,
    width,
    height,
    remote_only,
    raw_json,
    debug,
    zoom=None,
    lat=None,
    lon=None,
    include_widgets=False,
):
    """Render report snapshot PNG locally when configured, else use remote endpoint."""
    timeout_seconds = parse_int(timeout, 90)
    if timeout_seconds <= 0:
        print_snapshot_stderr("Invalid --timeout: must be positive.")
        return 2
    width_px = parse_int(width, 1600)
    height_px = parse_int(height, 900)
    if width_px <= 0 or height_px <= 0:
        print_snapshot_stderr("Invalid --width/--height: must be positive integers.")
        return 2

    report_id_value = str(report_id or "").strip()
    if not report_id_value:
        print_snapshot_stderr("Invalid --report-id.")
        return 2

    try:
        snapshot_viewport = build_snapshot_viewport_args(zoom=zoom, lat=lat, lon=lon)
    except ValueError as exc:
        print_snapshot_stderr(exc)
        return 2

    snapshot_args = {"report_id": report_id_value}
    snapshot_args.update(snapshot_viewport)
    if include_widgets:
        snapshot_args["include_widgets"] = True

    try:
        client = DekartClient.from_config()
        snapshot_payload = client.call("create_report_snapshot", snapshot_args, timeout_seconds=timeout_seconds)
        snapshot_response_received_at = time.monotonic()
    except urllib.error.HTTPError as exc:
        print_snapshot_stderr(f"Snapshot request failed ({exc.code}): {exc.reason}")
        raw_body, parsed_body = parse_http_error_body(exc)
        fields = extract_structured_error_fields(parsed_body)
        for key in ("error", "message", "path", "hint"):
            value = fields.get(key, "").strip()
            if value:
                print_snapshot_stderr(f"{key}: {value}")
        if raw_body and not fields:
            print_snapshot_stderr(f"response: {raw_body}")
        # Older servers reject the opt-in field before snapshot rendering begins.
        if (
            include_widgets
            and exc.code == 400
            and "unknown field" in raw_body
            and "include_widgets" in raw_body
        ):
            print_snapshot_stderr(
                "The Dekart server does not support --include-widgets; upgrade it."
            )
        return 1
    except Exception as exc:
        print_snapshot_stderr(f"Snapshot request failed: {exc}")
        return 1

    if not isinstance(snapshot_payload, dict):
        print_snapshot_stderr("Invalid snapshot response payload.")
        return 1
    result = snapshot_payload.get("result", {})
    if not isinstance(result, dict):
        print_snapshot_stderr("Invalid snapshot response payload.")
        return 1

    snapshot_url = resolve_dekart_url_reference(result.get("snapshot_url", ""))
    snapshot_render_url = resolve_dekart_url_reference(result.get("snapshot_render_url", ""))
    expires_in_raw = result.get("expires_in")
    expires_in = parse_int(expires_in_raw, 0)
    expires_at = snapshot_expiry_deadline(
        expires_in_raw,
        snapshot_response_received_at,
    )
    config = load_config(get_config_path())
    settings = get_local_snapshot_settings(config)
    local_enabled = bool(settings.get("enabled", False))
    prefer_local = (not remote_only) and local_enabled
    can_attempt_local = prefer_local and bool(snapshot_render_url)
    local_error = ""
    source = "local" if prefer_local else "remote"
    png_bytes = None

    if prefer_local:
        if not snapshot_render_url:
            print_snapshot_stderr(
                "Snapshot response did not include snapshot_render_url, but local snapshot is enabled."
            )
            if snapshot_url:
                print_snapshot_stderr(
                    "Rerun with the argument change: add `--remote-only`."
                )
            else:
                print_snapshot_stderr(
                    "Remote snapshot capture is unavailable for this Dekart instance."
                )
            return 1
        try:
            if debug:
                print_snapshot_stderr(
                    "[debug] local_snapshot_render_url="
                    f"{snapshot_url_debug_summary(snapshot_render_url)}"
                )
            png_bytes = render_local_snapshot_png(
                snapshot_render_url,
                width=width_px,
                height=height_px,
                timeout_seconds=timeout_seconds,
                expires_at=expires_at,
                debug=debug,
            )
            source = "local"
        except Exception as exc:
            local_error = sanitize_snapshot_diagnostic(exc)
            if isinstance(exc, LocalSnapshotRenderError) and exc.attempts > 1:
                print_snapshot_stderr(
                    f"Local snapshot render failed after {exc.attempts} attempts: {exc}"
                )
            else:
                print_snapshot_stderr(f"Local snapshot render failed: {exc}")
            print_local_snapshot_failure_guidance(
                exc,
                snapshot_url=snapshot_url,
                timeout_seconds=timeout_seconds,
            )
            return 1

    if png_bytes is None:
        if not snapshot_url:
            print_snapshot_stderr(
                "Remote snapshot capture is unavailable: snapshot response did not include snapshot_url."
            )
            return 1
        try:
            png_bytes = client._download_binary(snapshot_url, timeout_seconds=timeout_seconds)
            source = "remote"
        except urllib.error.HTTPError as exc:
            print_snapshot_stderr(
                f"Remote snapshot download failed ({exc.code}): {exc.reason}"
            )
            raw_body, parsed_body = parse_http_error_body(exc)
            fields = extract_structured_error_fields(parsed_body)
            for key in ("error", "message", "path", "hint"):
                value = fields.get(key, "").strip()
                if value:
                    print_snapshot_stderr(f"{key}: {value}")
            if raw_body and not fields:
                print_snapshot_stderr(f"response: {raw_body}")
            return 1
        except Exception as exc:
            print_snapshot_stderr(f"Remote snapshot download failed: {exc}")
            return 1

    if not is_png_snapshot(png_bytes):
        print_snapshot_stderr(
            f"{source.capitalize()} snapshot capture returned invalid PNG data."
        )
        return 1

    try:
        output_path = resolve_snapshot_output_path(report_id_value, out)
        save_binary_file(output_path, png_bytes)
    except OSError as exc:
        print_snapshot_stderr(f"Failed to write snapshot file: {exc}")
        return 1

    payload = {
        "report_id": report_id_value,
        "path": str(output_path),
        "source": source,
        "bytes": len(png_bytes),
        "snapshot_url": snapshot_url,
        "snapshot_render_url": snapshot_render_url,
        "snapshot_viewport": snapshot_viewport,
        "include_widgets": bool(include_widgets),
        "expires_in": expires_in,
        "local_enabled": local_enabled,
        "local_attempted": can_attempt_local,
        "local_error": local_error,
    }
    if raw_json:
        pretty_print_json(payload)
    else:
        print(f"Snapshot saved: {payload['path']}")
        print(f"Source: {payload['source']}")
        if local_error:
            print(f"Local fallback reason: {local_error}")
    return 0


def result_file_path(out_dir, job_id_value, query_job):
    """Build the final result file path from resolved job metadata."""
    directory = Path(out_dir).expanduser()
    source_id = str(query_job.get("job_result_id", "")).strip() or job_id_value
    dataset_ref = str(query_job.get("dataset_id", "")).strip()
    stem = source_id or dataset_ref
    if not stem:
        raise ValueError("Could not resolve result file name from job.")
    extension = resolve_job_result_extension(query_job)
    return directory / f"{stem}.{extension}"


def report_url_for_report_id(report_id_value, dekart_url=None):
    """Build a user-facing Dekart report URL from a report id."""
    return resolve_dekart_url_reference(f"reports/{quote(report_id_value, safe='')}", dekart_url=dekart_url)


def handle_report_url(report_id, raw_json):
    """Resolve a report id to a user-facing Dekart report URL."""
    report_id_value = str(report_id or "").strip()
    if not report_id_value:
        print("Invalid --report-id.", file=sys.stderr)
        return 2
    report_url = report_url_for_report_id(report_id_value)
    if raw_json:
        pretty_print_json({"report_id": report_id_value, "report_url": report_url})
    else:
        print(report_url)
    return 0


def encode_query_params_values(params_json):
    """Encode a JSON object as Dekart qp_ execution values."""
    try:
        values = json.loads(str(params_json or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid --params-json: {0}".format(exc)) from exc
    if not isinstance(values, dict):
        raise ValueError("Invalid --params-json: expected a JSON object.")
    encoded = []
    for key, value in values.items():
        name = str(key).strip()
        if not name:
            raise ValueError("Invalid --params-json: parameter names must not be empty.")
        if isinstance(value, (dict, list)):
            raise ValueError("Invalid --params-json: values must be scalars.")
        if value is None:
            text = ""
        elif isinstance(value, bool):
            text = "true" if value else "false"
        else:
            text = str(value)
        encoded.append(("qp_" + name, text))
    return urlencode(encoded)


def print_run_query_result(payload, print_rows, raw_json):
    """Render one completed connection or DuckDB result."""
    if print_rows:
        print_query_rows(payload["path"], payload["result_extension"])
    if raw_json:
        pretty_print_json(payload)
    elif not print_rows:
        print("Saved: {0}".format(payload["path"]))
        print("Bytes: {0}".format(payload["bytes"]))


def print_query_rows(path, extension):
    """Print downloaded query rows using local tooling."""
    result_path = Path(path).expanduser()
    normalized_extension = str(extension or result_path.suffix.lstrip(".")).strip().lower()
    if normalized_extension == "csv":
        print(result_path.read_text(encoding="utf-8"), end="")
        return
    if normalized_extension != "parquet":
        raise RuntimeError(f"Cannot print unsupported result format: {normalized_extension}")
    try:
        import duckdb  # type: ignore
    except Exception as exc:
        raise RuntimeError("Cannot print parquet rows because the duckdb Python package is not installed.") from exc

    try:
        connection = duckdb.connect()
        try:
            cursor = connection.execute("SELECT * FROM read_parquet(?)", [str(result_path)])
            columns = [str(item[0]) for item in (cursor.description or [])]
            if columns:
                print("\t".join(columns))
            while True:
                rows = cursor.fetchmany(1000)
                if not rows:
                    break
                for row in rows:
                    print("\t".join("" if value is None else str(value) for value in row))
        finally:
            connection.close()
    except Exception as exc:
        raise RuntimeError(f"Failed to print parquet rows with duckdb: {exc}") from exc


def validate_preview_file(path):
    """Resolve and validate a local preview file."""
    result_path = Path(path).expanduser()
    if not result_path.exists():
        raise ValueError(f"File does not exist: {result_path}")
    if not result_path.is_file():
        raise ValueError(f"Not a file: {result_path}")
    if result_path.stat().st_size <= 0:
        raise ValueError(f"File is empty: {result_path}")
    extension = result_path.suffix.lstrip(".").lower()
    if extension not in {"csv", "parquet"}:
        raise ValueError("Unsupported preview format; expected .csv or .parquet.")
    return result_path, extension


def duckdb_relation_for_file(connection, path, extension):
    """Return a DuckDB relation over a supported result file."""
    if extension == "parquet":
        return connection.execute("SELECT * FROM read_parquet(?)", [str(path)])
    if extension == "csv":
        return connection.execute("SELECT * FROM read_csv_auto(?)", [str(path)])
    raise ValueError("Unsupported preview format; expected .csv or .parquet.")


def print_preview(path, limit, schema):
    """Print schema or rows for a downloaded CSV/parquet result."""
    result_path, extension = validate_preview_file(path)
    limit_value = parse_int(limit, 20)
    if limit_value < 0:
        raise ValueError("--limit must be zero or positive.")
    try:
        import duckdb  # type: ignore
    except Exception as exc:
        raise RuntimeError("Cannot preview rows because the duckdb Python package is not installed.") from exc

    try:
        connection = duckdb.connect()
        try:
            if schema:
                if extension == "parquet":
                    cursor = connection.execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(result_path)])
                else:
                    cursor = connection.execute("DESCRIBE SELECT * FROM read_csv_auto(?)", [str(result_path)])
                for row in cursor.fetchall():
                    print(f"{row[0]}\t{row[1]}")
                return

            cursor = duckdb_relation_for_file(connection, result_path, extension)
            columns = [str(item[0]) for item in (cursor.description or [])]
            if columns:
                print("\t".join(columns))
            remaining = limit_value
            while remaining > 0:
                rows = cursor.fetchmany(min(remaining, 1000))
                if not rows:
                    break
                remaining -= len(rows)
                for row in rows:
                    print("\t".join("" if value is None else str(value) for value in row))
        finally:
            connection.close()
    except Exception as exc:
        raise RuntimeError(f"Failed to preview result file with duckdb: {exc}") from exc


def handle_preview(file_path, limit, schema):
    """Preview a downloaded result file."""
    try:
        print_preview(file_path, limit, schema)
        return 0
    except ValueError as exc:
        print(f"Invalid preview arguments: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def handle_run_query(query_id, out_dir, deprecated_out, wait, timeout, interval, print_rows, raw_json, params_json="{}"):
    """Run a prepared query through Dekart and download result rows."""
    if print_rows and raw_json:
        print("Use either --print or --json, not both.", file=sys.stderr)
        return 2
    if str(deprecated_out or "").strip():
        print("Invalid run-query arguments: --out was removed; use --out-dir instead.", file=sys.stderr)
        return 2
    query_id_value = str(query_id or "").strip()
    if not query_id_value:
        print("Invalid --query-id.", file=sys.stderr)
        return 2
    out_dir_value = str(out_dir or "").strip()
    if not out_dir_value:
        print("Invalid run-query arguments: provide --out-dir.", file=sys.stderr)
        return 2
    timeout_seconds = parse_int(timeout, 300)
    interval_seconds = parse_int(interval, 5)
    if timeout_seconds <= 0:
        print("Invalid --timeout: must be positive.", file=sys.stderr)
        return 2
    if interval_seconds <= 0:
        print("Invalid --interval: must be positive.", file=sys.stderr)
        return 2

    try:
        query_params_values = encode_query_params_values(params_json)
        client = DekartClient.from_config()
        metadata = client_operations.run_prepared_query(client, query_id_value, query_params_values, timeout_seconds=timeout_seconds)
        if "duckdb_execution" in metadata:
            payload = client_operations.materialize_duckdb_query(
                client, metadata,
                out_dir_value,
                wait,
                timeout_seconds,
                interval_seconds,
            )
            if payload.get("pending_source_jobs"):
                if raw_json:
                    pretty_print_json(payload)
                else:
                    print("Pending source jobs: {0}".format(len(payload["pending_source_jobs"])))
                return 0
            print_run_query_result(payload, print_rows, raw_json)
            return 0
        query_job = client_operations.wait_for_query_job(client, metadata["job_id"], wait, timeout_seconds, interval_seconds)
        if not is_terminal_done_status(query_job):
            status = str(query_job.get("job_status", "")).strip()
            print(f"Job is not done: {status}", file=sys.stderr)
            return 1
        out_path = result_file_path(out_dir_value, metadata["job_id"], query_job)
        download = client_operations.download_query_job_result(client, metadata["job_id"], query_job, out_path, timeout_seconds=60)
        if parse_int(download.get("bytes"), 0) <= 0:
            print("empty result (metadata/SHOW statement?)", file=sys.stderr)
            return 1

        status = str(query_job.get("job_status", "")).strip()
        payload = {
            **metadata,
            "dataset_id": str(query_job.get("dataset_id", "")).strip(),
            "terminal_status": status,
            "job_result_id": download["job_result_id"],
            "dataset_ref": download["dataset_ref"],
            "result_extension": download["result_extension"],
            "result_file": download["path"],
            "path": download["path"],
            "bytes": download["bytes"],
        }

        print_run_query_result(payload, print_rows, raw_json)
        return 0
    except TimeoutError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"Query failed: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Invalid response: {exc}", file=sys.stderr)
        return 1
    except urllib.error.HTTPError as exc:
        print(f"Query request failed ({exc.code}): {exc.reason}", file=sys.stderr)
        raw_body, parsed_body = parse_http_error_body(exc)
        fields = extract_structured_error_fields(parsed_body)
        for key in ("error", "message", "path", "hint"):
            value = fields.get(key, "").strip()
            if value:
                print(f"{key}: {value}", file=sys.stderr)
        if raw_body and not fields:
            print(f"response: {raw_body}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Query failed: {exc}", file=sys.stderr)
        return 1


def build_device_name():
    """Build local device label for Dekart authorization records."""
    node_name = platform.node().strip() or "unknown-host"
    system_name = platform.system().strip() or "UnknownOS"
    return f"{node_name} ({system_name})"


class LocalSetupError(Exception):
    """Signal that interactive local setup failed before authorization."""

    def __init__(self, exit_code):
        super().__init__("Local Dekart setup failed")
        self.exit_code = exit_code


def handle_init(no_browser, local_snapshot_mode, bigquery_passthrough_mode="ask"):
    """Run device authorization flow and save returned Dekart CLI token."""
    interactive = is_interactive_terminal()
    if interactive:
        print_init_banner()
        try:
            selected_url = prompt_init_dekart_url()
        except LocalSetupError as exc:
            return exc.exit_code
        except KeyboardInterrupt:
            print("\nSetup cancelled.", file=sys.stderr)
            return 130
        if not selected_url:
            return 0
        dekart_url = selected_url.rstrip("/")
    else:
        dekart_url = get_dekart_url().rstrip("/")
        print(ansi_rgb("[Step 1 of 4] Select Dekart endpoint", 112, 181, 208, bold=True))
        print(f"Using Dekart endpoint: {dekart_url}")
        print("Tip: change endpoint later with: dekart config --url <URL>")
        print()

    save_dekart_url(dekart_url)

    print(ansi_rgb("[Step 2 of 4] BigQuery passthrough setup", 112, 181, 208, bold=True))
    if not configure_google_bigquery_passthrough(
        bigquery_passthrough_mode,
        dekart_url,
        interactive,
    ):
        print("Warning: continuing without BigQuery passthrough.", file=sys.stderr)

    mode = str(local_snapshot_mode or "ask").strip().lower()
    print()
    print(ansi_rgb("[Step 3 of 4] Local snapshot setup", 112, 181, 208, bold=True))
    if mode == "skip":
        print("Skipped local snapshot install (--local-snapshot skip).")
        print("You can enable later with: dekart snapshot-local install")
        print("You can disable/uninstall with: dekart snapshot-local uninstall")
    else:
        print("Installing local snapshot capability for best snapshot performance...")
        install_result = install_local_snapshot_capability(
            debug=False,
            interactive=interactive,
        )
        if install_result.get("ok"):
            print(install_result.get("message", "Local snapshot capability installed."))
            print("Manage later with: dekart snapshot-local status|install|uninstall")
        else:
            print(install_result.get("message", "Failed to install local snapshot capability."), file=sys.stderr)
            print("Warning: continuing without local snapshots. Retry with `dekart snapshot-local install`.", file=sys.stderr)

    device_endpoint = f"{dekart_url}/api/v1/device"
    token_endpoint = f"{dekart_url}/api/v1/device/token"
    token_path = get_token_path()

    print()
    print(ansi_rgb("[Step 4 of 4] Authorize this device", 112, 181, 208, bold=True))
    print("Registering device with Dekart...")
    try:
        start_payload = post_json(device_endpoint, {"device_name": build_device_name()})
    except urllib.error.HTTPError as exc:
        print(f"Device registration failed ({exc.code}): {exc.reason}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Device registration failed: {exc}", file=sys.stderr)
        return 1

    device_id = str(start_payload.get("device_id", "")).strip()
    auth_url = resolve_dekart_url_reference(start_payload.get("auth_url", ""), dekart_url=dekart_url)
    expires_in = int(start_payload.get("expires_in", 0) or 0)
    interval = int(start_payload.get("interval", 3) or 3)
    interval = max(interval, 1)

    if not device_id or not auth_url or expires_in <= 0:
        print("Invalid response from Dekart device endpoint.", file=sys.stderr)
        return 1

    print("Open this URL and approve access:")
    print(f"  {auth_url}")
    if no_browser:
        print("Browser auto-open is disabled (--no-browser).")
    else:
        print("Opening browser...")
        try:
            webbrowser.open(auth_url, new=2, autoraise=True)
        except Exception:
            print("Could not open browser automatically. Open the URL manually.", file=sys.stderr)
            print("If browser does not open, open this link:", file=sys.stderr)
            print(f"  {auth_url}", file=sys.stderr)

    print(f"Waiting for authorization (poll every {interval}s, expires in {expires_in}s)...")
    deadline = time.monotonic() + expires_in
    while time.monotonic() <= deadline:
        try:
            token_payload = post_json(token_endpoint, {"device_id": device_id}, timeout_seconds=max(interval + 5, 10))
        except KeyboardInterrupt:
            print("\nAuthorization cancelled.", file=sys.stderr)
            return 130
        except urllib.error.HTTPError as exc:
            print(f"Token polling failed ({exc.code}): {exc.reason}", file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"Token polling failed: {exc}", file=sys.stderr)
            return 1

        status = str(token_payload.get("status", "")).strip()
        if status == "authorized" and token_payload.get("token"):
            print("Finalizing setup...")
            save_token(token_path, dekart_url, token_payload)
            email = token_payload.get("email", "")
            print(f"Done. Authenticated as {email}")
            print(f"Token saved: {token_path}")

            local_snapshot_enabled = get_local_snapshot_settings(load_config(get_config_path())).get("enabled", False)
            passthrough_binding = get_google_bigquery_passthrough_binding()
            print()
            print(ansi_rgb("Hey, success!", 112, 181, 208, bold=True))
            print("Setup summary:")
            print(f"  Endpoint: {dekart_url}")
            print(f"  Account: {email}")
            print(f"  Config: {get_config_path()}")
            print(f"  Token: {token_path}")
            print(
                "  BigQuery passthrough: {0}".format(
                    passthrough_binding["gcloud_account"] if passthrough_binding else "disabled"
                )
            )
            print(f"  Local snapshot: {'enabled' if local_snapshot_enabled else 'disabled'}")
            print("You can run `dekart init` again anytime.")
            return 0
        if status == "expired":
            print("Authorization expired. Run dekart init again.", file=sys.stderr)
            return 1
        if status not in {"pending", ""}:
            error = token_payload.get("error", "unknown_error")
            print(f"Authorization failed: {error}", file=sys.stderr)
            return 1

        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\nAuthorization cancelled.", file=sys.stderr)
            return 130

    print("Authorization timed out. Run dekart init again.", file=sys.stderr)
    return 1


def main():
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "mcp" and args.mcp_command == "serve":
        if sys.version_info < (3, 10):
            parser.exit(2, "dekart mcp serve needs Python 3.10 or newer.\n")
        try:
            from dekart.mcp_server import serve
        except ImportError:
            parser.exit(2, 'Install MCP support with pip install "dekart[mcp]".\n')
        try:
            serve(args.host, args.port)
        except ValueError as exc:
            parser.exit(2, str(exc) + "\n")
        return
    send_version_ping()

    if args.command == "config":
        raise SystemExit(handle_config(args.url))
    if args.command == "init":
        raise SystemExit(handle_init(args.no_browser, args.local_snapshot, args.bigquery_passthrough))
    if args.command == "local":
        raise SystemExit(
            local_docker.handle_local(
                args.local_action,
                getattr(args, "json", False),
                save_url=save_dekart_url,
                force=getattr(args, "force", False),
            )
        )
    if args.command == "tools":
        raise SystemExit(handle_tools(args.json, args.names, args.match, args.schema, args.arg_keys))
    if args.command == "resolve-tools":
        raise SystemExit(handle_resolve_tools(args.json, args.shell, args.out))
    if args.command == "call":
        raise SystemExit(
            handle_call(
                args.name,
                args.args,
                args.args_file,
                args.args_stdin,
                args.timeout,
                args.debug,
                args.json,
                args.extract,
                args.out,
            )
        )
    if args.command == "upload-parts":
        raise SystemExit(
            handle_upload_parts(
                file_path=args.file,
                start_response_json=args.start_response_json,
                start_response_file=args.start_response_file,
                upload_part_endpoint=args.upload_part_endpoint,
                required_headers_json=args.required_headers_json,
                max_part_size=args.max_part_size,
                file_id=args.file_id,
                complete_args_out=args.complete_args_out,
                raw_json=args.json,
            )
        )
    if args.command == "upload-file":
        raise SystemExit(
            handle_upload_file(
                file_path=args.file,
                use_stdin=args.stdin,
                file_id=args.file_id,
                name=args.name,
                mime_type=args.mime_type,
                max_part_size=args.max_part_size,
                raw_json=args.json,
                out=args.out,
            )
        )
    if args.command == "snapshot-local":
        raise SystemExit(
            handle_snapshot_local(
                action=args.action,
                raw_json=args.json,
                purge=args.purge,
                debug=args.debug,
            )
        )
    if args.command == "snapshot":
        raise SystemExit(
            handle_snapshot(
                report_id=args.report_id,
                out=args.out,
                timeout=args.timeout,
                width=args.width,
                height=args.height,
                remote_only=args.remote_only,
                raw_json=args.json,
                debug=args.debug,
                zoom=args.zoom,
                lat=args.lat,
                lon=args.lon,
                include_widgets=args.include_widgets,
            )
        )
    if args.command == "report-url":
        raise SystemExit(handle_report_url(report_id=args.report_id, raw_json=args.json))
    if args.command == "run-query":
        raise SystemExit(
            handle_run_query(
                query_id=args.query_id,
                params_json=args.params_json,
                out_dir=args.out_dir,
                deprecated_out=args.deprecated_out,
                wait=args.wait,
                timeout=args.timeout,
                interval=args.interval,
                print_rows=args.print_rows,
                raw_json=args.json,
            )
        )
    if args.command == "preview":
        raise SystemExit(handle_preview(file_path=args.file, limit=args.limit, schema=args.schema))

    parser.print_help()


if __name__ == "__main__":
    main()
