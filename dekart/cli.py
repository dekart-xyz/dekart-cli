import argparse
import datetime as dt
import json
import mimetypes
import os
import platform
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DEFAULT_DEKART_URL = "https://cloud.dekart.xyz"


def build_parser():
    """Build CLI parser for dekart commands."""
    parser = argparse.ArgumentParser(
        prog="dekart",
        description="Dekart CLI for auth, MCP tools, uploads, and snapshots.",
    )
    subparsers = parser.add_subparsers(dest="command")

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
        help="After auth, ask/install/skip local headless snapshot capability (default: ask).",
    )

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
        help="Timeout seconds for snapshot calls/rendering (default: 90).",
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
        "--remote-only",
        action="store_true",
        help="Skip local render attempt and use remote snapshot endpoint only.",
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

    return parser


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
        headers={"Content-Type": "application/json"},
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
        headers=headers or {},
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


def snapshot_token_from_render_url(render_url):
    """Extract snapshot_token query param from snapshot render URL."""
    try:
        parsed = urlparse(str(render_url or "").strip())
    except Exception:
        return ""
    if not parsed.query:
        return ""
    query = parse_qs(parsed.query, keep_blank_values=False)
    token_values = query.get("snapshot_token", [])
    if not token_values:
        return ""
    return str(token_values[0]).strip()


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


def render_local_snapshot_png(render_url, width, height, timeout_seconds):
    """Render snapshot PNG bytes with local Playwright Chromium."""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception as exc:
        raise RuntimeError("Playwright is not installed. Run `dekart snapshot-local install`.") from exc

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": width, "height": height},
            ignore_https_errors=True,
        )
        page = context.new_page()
        page.goto(render_url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
        snapshot_token = snapshot_token_from_render_url(render_url)
        if snapshot_token:
            page.wait_for_function(
                "(expectedToken) => window.__dekartSnapshotReadyToken === expectedToken",
                arg=snapshot_token,
                timeout=timeout_seconds * 1000,
            )
        else:
            page.wait_for_timeout(1500)
            for selector in ("canvas", ".kepler-gl", "img"):
                try:
                    page.wait_for_selector(selector, timeout=5000)
                    break
                except Exception:
                    continue
        png_bytes = page.screenshot(type="png", timeout=timeout_seconds * 1000)
        context.close()
        browser.close()
        return png_bytes


def download_binary(url, timeout_seconds=30):
    """Download binary payload from URL with optional auth headers."""
    headers = {}
    auth_headers = get_auth_headers()
    if auth_headers:
        headers.update(auth_headers)
    request = urllib.request.Request(
        url=url,
        headers=headers,
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return response.read()


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


def extract_json_path(payload, json_path):
    """Extract value by dot-separated JSON path from payload."""
    path = str(json_path or "").strip()
    if not path:
        raise ValueError("Empty extract path.")
    if path.startswith("$."):
        path = path[2:]
    if path.startswith("$"):
        path = path[1:]
    if not path:
        raise ValueError("Empty extract path.")

    current = payload
    for segment in path.split("."):
        key = segment.strip()
        if not key:
            raise ValueError(f"Invalid extract path: {json_path}")
        if isinstance(current, dict):
            if key not in current:
                raise ValueError(f"Path not found: {json_path}")
            current = current[key]
            continue
        if isinstance(current, list):
            try:
                index = int(key)
            except ValueError as exc:
                raise ValueError(f"Expected numeric index in path segment `{key}`.") from exc
            if index < 0 or index >= len(current):
                raise ValueError(f"List index out of range in path segment `{key}`.")
            current = current[index]
            continue
        raise ValueError(f"Path segment `{key}` cannot be resolved on scalar value.")
    return current


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


def parse_int(value, default=0):
    """Convert protobuf/json numeric field to int with safe fallback."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def header_get(headers, key):
    """Read one header value with case-insensitive fallback."""
    if not headers:
        return ""
    value = ""
    try:
        value = str(headers.get(key, "")).strip()
    except Exception:
        value = ""
    if value:
        return value
    lower_key = str(key).lower()
    for existing_key in headers.keys():
        if str(existing_key).lower() == lower_key:
            return str(headers.get(existing_key, "")).strip()
    return ""


def request_id_from_headers(headers):
    """Extract best-effort request id from response headers."""
    for key in ("x-request-id", "x-correlation-id", "x-amzn-requestid", "x-cloud-trace-context"):
        value = header_get(headers, key)
        if value:
            return value
    return ""


def parse_http_error_body(exc):
    """Read and parse HTTPError response body for diagnostics."""
    raw_text = ""
    parsed = None
    try:
        body = exc.read()
    except Exception:
        body = b""
    if isinstance(body, bytes):
        raw_text = body.decode("utf-8", errors="replace").strip()
    elif isinstance(body, str):
        raw_text = body.strip()
    if raw_text:
        try:
            candidate = json.loads(raw_text)
            if isinstance(candidate, dict):
                parsed = candidate
        except json.JSONDecodeError:
            parsed = None
    return raw_text, parsed


def find_nested_string(payload, path):
    """Return nested string by path list, else empty string."""
    current = payload
    for segment in path:
        if not isinstance(current, dict) or segment not in current:
            return ""
        current = current.get(segment)
    if isinstance(current, str):
        return current.strip()
    return ""


def extract_structured_error_fields(payload):
    """Extract common error fields from parsed error JSON."""
    if not isinstance(payload, dict):
        return {}
    fields = {}
    candidates = {
        "error": (("error",), ("code",), ("status",), ("error", "code"), ("error", "status")),
        "message": (
            ("message",),
            ("detail",),
            ("error_message",),
            ("error", "message"),
            ("error", "detail"),
            ("error", "error"),
        ),
        "path": (("path",), ("field",), ("location",), ("error", "path"), ("error", "field")),
        "hint": (("hint",), ("suggestion",), ("error", "hint"), ("error", "suggestion")),
    }
    for label, path_options in candidates.items():
        for path in path_options:
            value = find_nested_string(payload, path)
            if value:
                fields[label] = value
                break
    return fields


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
    """Call one MCP tool and return parsed response payload."""
    dekart_url = get_dekart_url().rstrip("/")
    endpoint = f"{dekart_url}/api/v1/mcp/call"
    headers = {"Content-Type": "application/json"}
    auth_headers = get_auth_headers()
    if auth_headers:
        headers.update(auth_headers)

    request_body = json.dumps({"name": name, "arguments": args}).encode("utf-8")
    request = urllib.request.Request(url=endpoint, data=request_body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        body = response.read().decode("utf-8")
        payload = json.loads(body) if body.strip() else {}
        if not return_metadata:
            return payload
        return payload, {
            "endpoint": endpoint,
            "timeout_seconds": timeout_seconds,
            "request_id": request_id_from_headers(response.headers),
        }


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
    """Upload one multipart chunk to Dekart part endpoint and return JSON manifest item."""
    headers = {"Content-Type": "application/octet-stream", "Content-Length": str(len(body_bytes))}
    auth_headers = get_auth_headers()
    if auth_headers:
        headers.update(auth_headers)
    for item in required_headers or []:
        key = str(item.get("key", "")).strip()
        value = str(item.get("value", ""))
        if key:
            headers[key] = value

    request = urllib.request.Request(url=put_url, data=body_bytes, headers=headers, method="PUT")
    with urllib.request.urlopen(request, timeout=120) as response:
        body = response.read().decode("utf-8")
        return json.loads(body) if body.strip() else {}


def parse_required_headers(required_headers_json):
    """Parse headers JSON into MCP-compatible key/value list."""
    if not required_headers_json:
        return []
    try:
        parsed = json.loads(required_headers_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid --required-headers-json: {exc}") from exc
    if not isinstance(parsed, list):
        raise ValueError("--required-headers-json must be a JSON array.")
    headers = []
    for item in parsed:
        if not isinstance(item, dict):
            raise ValueError("--required-headers-json items must be objects with key/value.")
        key = str(item.get("key", "")).strip()
        value = str(item.get("value", ""))
        if key:
            headers.append({"key": key, "value": value})
    return headers


def build_put_url(dekart_url, endpoint_template, part_number, part_size):
    """Build part upload URL from endpoint template and current part metadata."""
    if "{part_number}" not in endpoint_template:
        raise ValueError("upload_part_endpoint must include {part_number}.")
    endpoint = endpoint_template.replace("{part_number}", str(part_number))
    base_url = endpoint if endpoint.startswith("http://") or endpoint.startswith("https://") else f"{dekart_url}{endpoint}"
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}part_size={part_size}"


def upload_parts_from_start_result(local_file, start_result, upload_part_endpoint, required_headers_json, max_part_size, file_id):
    """Upload local file bytes using one upload session result payload."""
    file_size = local_file.stat().st_size
    resolved_endpoint = (upload_part_endpoint or str(start_result.get("upload_part_endpoint", ""))).strip()
    if not resolved_endpoint:
        raise ValueError("Missing upload part endpoint. Provide --upload-part-endpoint or start response JSON/file.")
    resolved_max_part_size = max_part_size if max_part_size and max_part_size > 0 else parse_int(start_result.get("max_part_size"), 0)
    if resolved_max_part_size <= 0:
        raise ValueError("Missing max part size. Provide --max-part-size or start response JSON/file.")
    if required_headers_json:
        required_headers = parse_required_headers(required_headers_json)
    else:
        required_headers = start_result.get("required_headers", [])
        if not isinstance(required_headers, list):
            required_headers = []
    upload_session_id = str(start_result.get("upload_session_id", "")).strip()
    start_file_id = str(start_result.get("file_id", "")).strip()
    resolved_file_id = str(file_id or "").strip() or start_file_id
    if not resolved_file_id:
        raise ValueError("Missing file id. Provide --file-id or include file_id in start response.")
    if not upload_session_id:
        raise ValueError("Missing upload_session_id in start response.")
    dekart_url = get_dekart_url().rstrip("/")

    parts = []
    part_number = 1
    with local_file.open("rb") as source:
        while True:
            chunk = source.read(resolved_max_part_size)
            if not chunk:
                break
            put_url = build_put_url(dekart_url, resolved_endpoint, part_number, len(chunk))
            part_response = upload_part_binary(put_url, chunk, required_headers)
            parts.append(
                {
                    "part_number": parse_int(part_response.get("part_number"), part_number),
                    "etag": str(part_response.get("etag", "")),
                    "size": parse_int(part_response.get("size"), len(chunk)),
                }
            )
            part_number += 1

    complete_args = {
        "file_id": resolved_file_id,
        "upload_session_id": upload_session_id,
        "parts": parts,
        "total_size": file_size,
    }
    return {
        "parts": parts,
        "parts_uploaded": len(parts),
        "total_size": file_size,
        "max_part_size": resolved_max_part_size,
        "upload_session_id": upload_session_id,
        "file_id": resolved_file_id,
        "complete_args": complete_args,
    }


def upload_parts_from_bytes(file_bytes, start_result, upload_part_endpoint, required_headers_json, max_part_size, file_id):
    """Upload in-memory bytes using one upload session result payload."""
    file_size = len(file_bytes)
    if file_size <= 0:
        raise ValueError("File is empty.")

    resolved_endpoint = (upload_part_endpoint or str(start_result.get("upload_part_endpoint", ""))).strip()
    if not resolved_endpoint:
        raise ValueError("Missing upload part endpoint. Provide --upload-part-endpoint or start response JSON/file.")
    resolved_max_part_size = max_part_size if max_part_size and max_part_size > 0 else parse_int(start_result.get("max_part_size"), 0)
    if resolved_max_part_size <= 0:
        raise ValueError("Missing max part size. Provide --max-part-size or start response JSON/file.")
    if required_headers_json:
        required_headers = parse_required_headers(required_headers_json)
    else:
        required_headers = start_result.get("required_headers", [])
        if not isinstance(required_headers, list):
            required_headers = []
    upload_session_id = str(start_result.get("upload_session_id", "")).strip()
    start_file_id = str(start_result.get("file_id", "")).strip()
    resolved_file_id = str(file_id or "").strip() or start_file_id
    if not resolved_file_id:
        raise ValueError("Missing file id. Provide --file-id or include file_id in start response.")
    if not upload_session_id:
        raise ValueError("Missing upload_session_id in start response.")
    dekart_url = get_dekart_url().rstrip("/")

    parts = []
    part_number = 1
    offset = 0
    while offset < file_size:
        chunk = file_bytes[offset: offset + resolved_max_part_size]
        put_url = build_put_url(dekart_url, resolved_endpoint, part_number, len(chunk))
        part_response = upload_part_binary(put_url, chunk, required_headers)
        parts.append(
            {
                "part_number": parse_int(part_response.get("part_number"), part_number),
                "etag": str(part_response.get("etag", "")),
                "size": parse_int(part_response.get("size"), len(chunk)),
            }
        )
        offset += len(chunk)
        part_number += 1

    complete_args = {
        "file_id": resolved_file_id,
        "upload_session_id": upload_session_id,
        "parts": parts,
        "total_size": file_size,
    }
    return {
        "parts": parts,
        "parts_uploaded": len(parts),
        "total_size": file_size,
        "max_part_size": resolved_max_part_size,
        "upload_session_id": upload_session_id,
        "file_id": resolved_file_id,
        "complete_args": complete_args,
    }


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
    """Fetch MCP tool catalog payload from configured Dekart instance."""
    dekart_url = get_dekart_url().rstrip("/")
    endpoint = f"{dekart_url}/api/v1/mcp/tools"
    headers = get_auth_headers()
    return read_json(endpoint, headers=headers)


def tool_required_fields(tool):
    """Return required input schema fields for one MCP tool definition."""
    schema = tool.get("inputSchema", {}) if isinstance(tool, dict) else {}
    required = schema.get("required", []) if isinstance(schema, dict) else []
    return set(required) if isinstance(required, list) else set()


def tool_properties(tool):
    """Return declared input schema properties for one MCP tool definition."""
    schema = tool.get("inputSchema", {}) if isinstance(tool, dict) else {}
    props = schema.get("properties", {}) if isinstance(schema, dict) else {}
    return set(props.keys()) if isinstance(props, dict) else set()


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
        start_payload = mcp_call("start_file_upload_session", start_args, timeout_seconds=30)
        start_result = start_payload.get("result", {}) if isinstance(start_payload, dict) else {}
        if not isinstance(start_result, dict):
            raise ValueError("Invalid start_file_upload_session response.")

        upload_payload = upload_parts_from_start_result(
            local_file=staged_file,
            start_result=start_result,
            upload_part_endpoint="",
            required_headers_json="",
            max_part_size=max_part_size,
            file_id=file_id,
        )
        complete_args = upload_payload.get("complete_args", {})
        complete_payload = mcp_call("complete_file_upload_session", complete_args, timeout_seconds=60)
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
        return 0 if result.get("ok") else 1

    print(f"Unknown action: {action}", file=sys.stderr)
    return 2


def resolve_snapshot_output_path(report_id, out):
    """Resolve PNG output path for snapshot command."""
    default_name = f"snapshot-{report_id}.png"
    return Path(out or default_name).expanduser()


def save_binary_file(path, body):
    """Write raw bytes to file path, creating parent directories."""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return target


def handle_snapshot(report_id, out, timeout, width, height, remote_only, raw_json, debug):
    """Render report snapshot PNG locally when configured, else use remote endpoint."""
    timeout_seconds = parse_int(timeout, 90)
    if timeout_seconds <= 0:
        print("Invalid --timeout: must be positive.", file=sys.stderr)
        return 2
    width_px = parse_int(width, 1600)
    height_px = parse_int(height, 900)
    if width_px <= 0 or height_px <= 0:
        print("Invalid --width/--height: must be positive integers.", file=sys.stderr)
        return 2

    report_id_value = str(report_id or "").strip()
    if not report_id_value:
        print("Invalid --report-id.", file=sys.stderr)
        return 2

    try:
        snapshot_payload = mcp_call("create_report_snapshot", {"report_id": report_id_value}, timeout_seconds=timeout_seconds)
    except urllib.error.HTTPError as exc:
        print(f"Snapshot request failed ({exc.code}): {exc.reason}", file=sys.stderr)
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
        print(f"Snapshot request failed: {exc}", file=sys.stderr)
        return 1

    result = snapshot_payload.get("result", {}) if isinstance(snapshot_payload, dict) else {}
    if not isinstance(result, dict):
        print("Invalid snapshot response payload.", file=sys.stderr)
        return 1

    snapshot_url = str(result.get("snapshot_url", "")).strip()
    snapshot_render_url = str(result.get("snapshot_render_url", "")).strip()
    expires_in = parse_int(result.get("expires_in"), 0)
    config = load_config(get_config_path())
    settings = get_local_snapshot_settings(config)
    local_enabled = bool(settings.get("enabled", False))
    prefer_local = (not remote_only) and local_enabled
    can_attempt_local = prefer_local and bool(snapshot_render_url)
    local_error = ""
    source = "local" if prefer_local else "remote"
    png_bytes = b""

    if prefer_local:
        if not snapshot_render_url:
            print("Snapshot response did not include snapshot_render_url, but local snapshot is enabled.", file=sys.stderr)
            print("Run with --remote-only to force remote snapshot.", file=sys.stderr)
            return 1
        try:
            if debug:
                print(f"[debug] local_snapshot_render_url={snapshot_render_url}", file=sys.stderr)
            png_bytes = render_local_snapshot_png(
                snapshot_render_url,
                width=width_px,
                height=height_px,
                timeout_seconds=timeout_seconds,
            )
            source = "local"
        except Exception as exc:
            local_error = str(exc)
            print(f"Local snapshot render failed: {local_error}", file=sys.stderr)
            print("Remote fallback is disabled while local snapshot is enabled.", file=sys.stderr)
            print("Use --remote-only to force remote snapshot.", file=sys.stderr)
            return 1

    if not png_bytes:
        if not snapshot_url:
            print("Snapshot response did not include snapshot_url.", file=sys.stderr)
            return 1
        try:
            png_bytes = download_binary(snapshot_url, timeout_seconds=timeout_seconds)
            source = "remote"
        except urllib.error.HTTPError as exc:
            print(f"Remote snapshot download failed ({exc.code}): {exc.reason}", file=sys.stderr)
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
            print(f"Remote snapshot download failed: {exc}", file=sys.stderr)
            return 1

    try:
        output_path = resolve_snapshot_output_path(report_id_value, out)
        save_binary_file(output_path, png_bytes)
    except OSError as exc:
        print(f"Failed to write snapshot file: {exc}", file=sys.stderr)
        return 1

    payload = {
        "report_id": report_id_value,
        "path": str(output_path),
        "source": source,
        "bytes": len(png_bytes),
        "snapshot_url": snapshot_url,
        "snapshot_render_url": snapshot_render_url,
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


def build_device_name():
    """Build local device label for Dekart authorization records."""
    node_name = platform.node().strip() or "unknown-host"
    system_name = platform.system().strip() or "UnknownOS"
    return f"{node_name} ({system_name})"


def handle_init(no_browser, local_snapshot_mode):
    """Run device authorization flow and save returned Dekart CLI token."""
    dekart_url = get_dekart_url().rstrip("/")
    device_endpoint = f"{dekart_url}/api/v1/device"
    token_endpoint = f"{dekart_url}/api/v1/device/token"
    token_path = get_token_path()

    try:
        start_payload = post_json(device_endpoint, {"device_name": build_device_name()})
    except urllib.error.HTTPError as exc:
        print(f"Device registration failed ({exc.code}): {exc.reason}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Device registration failed: {exc}", file=sys.stderr)
        return 1

    device_id = str(start_payload.get("device_id", "")).strip()
    auth_url = str(start_payload.get("auth_url", "")).strip()
    expires_in = int(start_payload.get("expires_in", 0) or 0)
    interval = int(start_payload.get("interval", 3) or 3)
    interval = max(interval, 1)

    if not device_id or not auth_url or expires_in <= 0:
        print("Invalid response from Dekart device endpoint.", file=sys.stderr)
        return 1

    print("Opening browser to authorize...")
    print(f"Auth URL: {auth_url}")
    if not no_browser:
        try:
            webbrowser.open(auth_url, new=2, autoraise=True)
        except Exception:
            print("Could not open browser automatically. Open the URL manually.", file=sys.stderr)

    print("Waiting for authorization...")
    deadline = time.monotonic() + expires_in
    while time.monotonic() <= deadline:
        try:
            token_payload = post_json(token_endpoint, {"device_id": device_id}, timeout_seconds=max(interval + 5, 10))
        except urllib.error.HTTPError as exc:
            print(f"Token polling failed ({exc.code}): {exc.reason}", file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"Token polling failed: {exc}", file=sys.stderr)
            return 1

        status = str(token_payload.get("status", "")).strip()
        if status == "authorized" and token_payload.get("token"):
            save_token(token_path, dekart_url, token_payload)
            email = token_payload.get("email", "")
            print(f"Done. Authenticated as {email}")
            print(f"Token saved: {token_path}")
            mode = str(local_snapshot_mode or "ask").strip().lower()
            if mode == "install":
                print("Installing optional local snapshot capability...")
                install_result = install_local_snapshot_capability(
                    debug=False,
                    interactive=bool(sys.stdin.isatty() and sys.stdout.isatty()),
                )
                if install_result.get("ok"):
                    print(install_result.get("message", "Local snapshot capability installed."))
                else:
                    print(install_result.get("message", "Failed to install local snapshot capability."), file=sys.stderr)
                    print("You can retry later with `dekart snapshot-local install`.", file=sys.stderr)
                    return 1
            elif mode == "ask":
                if sys.stdin.isatty() and sys.stdout.isatty():
                    print()
                    print("Optional: install local snapshot capability for faster local renders?")
                    answer = input("Install now? [Y/n]: ")
                    if parse_yes_no_input(answer, default=True):
                        print("Installing local snapshot capability...")
                        install_result = install_local_snapshot_capability(
                            debug=False,
                            interactive=bool(sys.stdin.isatty() and sys.stdout.isatty()),
                        )
                        if install_result.get("ok"):
                            print(install_result.get("message", "Local snapshot capability installed."))
                        else:
                            print(install_result.get("message", "Failed to install local snapshot capability."), file=sys.stderr)
                            print("You can retry later with `dekart snapshot-local install`.", file=sys.stderr)
                    else:
                        print("Skipped local snapshot install. You can install later with `dekart snapshot-local install`.")
                else:
                    print("Tip: install local snapshot capability later with `dekart snapshot-local install`.")
            return 0
        if status == "expired":
            print("Authorization expired. Run dekart init again.", file=sys.stderr)
            return 1
        if status not in {"pending", ""}:
            error = token_payload.get("error", "unknown_error")
            print(f"Authorization failed: {error}", file=sys.stderr)
            return 1

        time.sleep(interval)

    print("Authorization timed out. Run dekart init again.", file=sys.stderr)
    return 1


def main():
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "config":
        raise SystemExit(handle_config(args.url))
    if args.command == "init":
        raise SystemExit(handle_init(args.no_browser, args.local_snapshot))
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
            )
        )

    parser.print_help()


if __name__ == "__main__":
    main()
