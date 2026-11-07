"""Authenticated Dekart operations shared by CLI commands and the MCP server."""
import io
import json
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from importlib import metadata
from pathlib import Path
from urllib.parse import quote, urlencode

from dekart import duckdb_execution
from dekart.snapshot import render_local_snapshot_png

__all__ = [
    "DekartClient", "run_prepared_query", "wait_for_query_job",
    "download_query_job_result", "materialize_duckdb_query",
    "upload_parts_from_bytes", "render_local_snapshot_png",
]


class DekartClient:
    def __init__(self, dekart_url, token):
        self.dekart_url = dekart_url.rstrip("/")
        try:
            version = metadata.version("dekart")
        except metadata.PackageNotFoundError:
            version = "unknown"
        self._headers = {"User-Agent": "dekart-cli/" + version}
        if token:
            self._headers["Authorization"] = "Bearer " + token
        self._google_headers = None

    @classmethod
    def from_config(cls):
        # Keep local configuration and gcloud access out of request-token clients.
        from dekart import cli
        headers = cli.get_auth_headers() or {}
        client = cls(cli.get_dekart_url(), headers.get("Authorization", "")[7:])
        binding = cli.get_google_bigquery_passthrough_binding()
        if binding:
            warning_shown = False

            def google_headers(url, force_refresh=False):
                nonlocal warning_shown
                if not cli.is_google_header_url_allowed(url, client.dekart_url):
                    return {}
                try:
                    token = cli.mint_google_access_token(binding["gcloud_account"], force_refresh=force_refresh)
                except RuntimeError as exc:
                    if not warning_shown:
                        print("Warning: {0}".format(exc), file=sys.stderr)
                        warning_shown = True
                    return {}
                return {cli.GOOGLE_ACCESS_TOKEN_HEADER: "Bearer " + token}
            client._google_headers = google_headers
        return client

    def _request(self, url, data=None, method="GET", headers=None, timeout_seconds=30, google_passthrough=False, _deadline=None):
        for attempt in range(2):
            request_headers = dict(self._headers)
            request_headers.update(headers or {})
            google_headers = (self._google_headers(url, force_refresh=attempt > 0)
                              if google_passthrough and self._google_headers else {})
            request_headers.update(google_headers)
            request = urllib.request.Request(url=url, data=data, headers=request_headers, method=method)
            try:
                response_context = (_urlopen_without_redirects(request, timeout_seconds)
                                    if google_headers else urllib.request.urlopen(request, timeout=timeout_seconds))
                with response_context as response:
                    if _deadline is None:
                        return response.read(), response.headers
                    chunks = []
                    while True:
                        _deadline.remaining()
                        chunk = response.read1(64 * 1024)
                        _deadline.remaining()
                        if not chunk:
                            break
                        chunks.append(chunk)
                    return b"".join(chunks), response.headers
            except urllib.error.HTTPError as exc:
                _body, payload = parse_http_error_body(exc)
                if attempt == 0 and google_headers and extract_structured_error_fields(payload).get("error") == "google_access_token_invalid":
                    exc.close()
                    continue
                raise

    def list_tools(self):
        body, _headers = self._request(self.dekart_url + "/api/v1/mcp/tools")
        return json.loads(body) if body.strip() else {}

    def call(self, name, arguments, timeout_seconds=30):
        return self._call(name, arguments, timeout_seconds)[0]

    def _call(self, name, arguments, timeout_seconds=30):
        endpoint = self.dekart_url + "/api/v1/mcp/call"
        body, headers = self._request(
            endpoint, json.dumps({"name": name, "arguments": arguments}).encode("utf-8"),
            "POST", {"Content-Type": "application/json"}, timeout_seconds, True,
        )
        return (json.loads(body) if body.strip() else {}), {
            "endpoint": endpoint, "timeout_seconds": timeout_seconds,
            "request_id": request_id_from_headers(headers),
        }

    def _download_binary(self, url, timeout_seconds=30, google_passthrough=False, _deadline=None):
        return self._request(url, timeout_seconds=timeout_seconds, google_passthrough=google_passthrough, _deadline=_deadline)[0]

    def _upload_part_binary(self, put_url, body_bytes, required_headers):
        headers = {"Content-Type": "application/octet-stream", "Content-Length": str(len(body_bytes))}
        headers.update({str(item["key"]): str(item.get("value", "")) for item in required_headers or [] if item.get("key")})
        body, _headers = self._request(put_url, body_bytes, "PUT", headers, 120)
        return json.loads(body) if body.strip() else {}


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _urlopen_without_redirects(request, timeout_seconds):
    return urllib.request.build_opener(_RejectRedirects()).open(request, timeout=timeout_seconds)


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
    cached = getattr(exc, "_dekart_error_body", None)
    if cached is not None:
        return cached
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
    result = (raw_text, parsed)
    setattr(exc, "_dekart_error_body", result)
    return result


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


def parse_int(value, default=0):
    """Convert protobuf/json numeric field to int with safe fallback."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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


def save_binary_file(path, body):
    """Write raw bytes to file path, creating parent directories."""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return target


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


def resolve_job_result_extension(query_job):
    """Resolve output extension from query job payload."""
    extension = str(query_job.get("result_extension", "")).strip().lower()
    if extension not in {"csv", "parquet"}:
        extension = "csv"
    return extension


def is_terminal_done_status(query_job):
    """Return True when job status indicates results are ready for download."""
    return query_job_status(query_job) == "JOB_STATUS_DONE"


def query_job_status(query_job):
    """Return a normalized query job status string."""
    return str(query_job.get("job_status", "")).strip().upper()


def is_terminal_failed_status(query_job):
    """Return True when job status is terminal and cannot produce results."""
    return query_job_status(query_job) in {"JOB_STATUS_UNSPECIFIED", "UNSPECIFIED", "0"}


def query_job_error(query_job):
    """Return a normalized query job error string."""
    return str(query_job.get("job_error", "") or query_job.get("jobError", "")).strip()


def required_nested_scalar(payload, paths, label):
    """Return first non-empty scalar found at one of the candidate paths."""
    for path in paths:
        try:
            value = extract_json_path(payload, path)
        except ValueError:
            continue
        if isinstance(value, (str, int, float)):
            text = str(value).strip()
            if text:
                return text
    raise ValueError(f"Could not resolve {label}.")


def tool_properties(tool):
    """Return declared input schema properties for one MCP tool definition."""
    schema = tool.get("inputSchema", {}) if isinstance(tool, dict) else {}
    props = schema.get("properties", {}) if isinstance(schema, dict) else {}
    return set(props.keys()) if isinstance(props, dict) else set()


def fetch_query_job(client, job_id, timeout_seconds=30):
    """Fetch one query job via MCP check_job_status."""
    payload = client.call("check_job_status", {"job_id": job_id}, timeout_seconds=timeout_seconds)
    result = payload.get("result", {}) if isinstance(payload, dict) else {}
    query_job = result.get("query_job") if isinstance(result, dict) else None
    if not isinstance(query_job, dict):
        raise ValueError("check_job_status returned invalid query_job payload.")
    return query_job


def wait_for_query_job(client, job_id, wait, timeout_seconds, interval_seconds):
    """Fetch query job status, optionally waiting for terminal DONE."""
    started = time.time()
    while True:
        query_job = fetch_query_job(client, job_id, timeout_seconds=30)
        error = query_job_error(query_job)
        if error:
            raise RuntimeError(error)
        if is_terminal_failed_status(query_job):
            status = str(query_job.get("job_status", "")).strip()
            raise RuntimeError(f"Query job failed with status {status or 'UNKNOWN'}")
        if not wait or is_terminal_done_status(query_job):
            return query_job
        if (time.time() - started) >= timeout_seconds:
            status = str(query_job.get("job_status", "")).strip()
            raise TimeoutError(f"Timed out waiting for job completion; last status={status}")
        time.sleep(interval_seconds)


def download_query_job_result(client, job_id, query_job, out, timeout_seconds=60):
    """Download query job result bytes and save them locally."""
    source_id = str(query_job.get("job_result_id", "")).strip() or job_id
    dataset_ref = str(query_job.get("dataset_id", "")).strip()
    if not dataset_ref:
        raise ValueError("Could not resolve dataset id from job.")

    extension = resolve_job_result_extension(query_job)
    output_path = Path(out or f"job-{job_id}.{extension}").expanduser()
    saved, size = _download_dataset_source(client, dataset_ref, source_id, extension, output_path, timeout_seconds)
    return {
        "job_result_id": source_id,
        "dataset_ref": dataset_ref,
        "result_extension": extension,
        "path": str(saved),
        "bytes": size,
    }


def _download_dataset_source(client, dataset_id, source_id, extension, output_path, timeout_seconds=60, url=None, _deadline=None):
    """Download one authenticated dataset source to a local path."""
    dekart_url = client.dekart_url.rstrip("/")
    source_url = "{0}/api/v1/dataset-source/{1}/{2}.{3}".format(
        dekart_url,
        quote(dataset_id, safe=""),
        quote(source_id, safe=""),
        extension,
    )
    # Computed URLs are checked against the recorded template by the server.
    if url is not None:
        source_url += "?" + urlencode({"url": url})
    options = {}
    if _deadline is not None:
        timeout_seconds = min(timeout_seconds, _deadline.remaining())
        options["_deadline"] = _deadline
    try:
        body = client._download_binary(source_url, timeout_seconds=timeout_seconds, google_passthrough=True, **options)
    except (OSError, urllib.error.URLError):
        if _deadline is not None:
            _deadline.remaining()
        raise
    return save_binary_file(output_path, body), len(body)


def supports_duckdb_execution(client):
    """Discover whether run_query accepts the additive DuckDB opt-in."""
    try:
        payload = client.list_tools()
    except Exception:
        return None
    tools = payload.get("tools", []) if isinstance(payload, dict) else []
    run_tool = next(
        (tool for tool in tools if isinstance(tool, dict) and tool.get("name") == "run_query"),
        None,
    )
    return "accept_duckdb_execution" in tool_properties(run_tool)


def run_prepared_query(client, query_id, query_params_values, timeout_seconds):
    """Run a saved query and return either a connection job or DuckDB program."""
    supports_duckdb = supports_duckdb_execution(client, )
    can_execute_duckdb = sys.version_info >= (3, 9)
    arguments = {"query_id": query_id}
    if query_params_values:
        arguments["query_params_values"] = query_params_values
    if supports_duckdb and can_execute_duckdb:
        arguments["accept_duckdb_execution"] = True
    try:
        run_payload = client.call("run_query", arguments, timeout_seconds=timeout_seconds)
    except urllib.error.HTTPError as exc:
        raw_body, _parsed_body = parse_http_error_body(exc)
        if "RunDuckDBQuery" not in raw_body and "DuckDB" not in raw_body:
            raise
        if supports_duckdb is None:
            raise RuntimeError(
                "Could not discover DuckDB execution support; retry when the Dekart server is available."
            ) from exc
        if supports_duckdb and not can_execute_duckdb:
            raise RuntimeError("DuckDB query execution requires Python 3.9 or newer.") from exc
        if not supports_duckdb:
            raise RuntimeError(
                "DuckDB execution requires a newer Dekart server; connection queries remain available."
            ) from exc
        raise
    result = run_payload.get("result", {}) if isinstance(run_payload, dict) else {}
    if isinstance(result, dict) and "duckdb_execution" in result:
        query_job, execution = duckdb_execution.validate_prepared_execution(result)
        if str(query_job.get("query_id", "")).strip() != query_id:
            raise ValueError("DuckDB query_job.query_id does not match the requested query.")
        return {
            "query_id": query_id,
            "job_id": query_job["id"],
            "query_job": query_job,
            "duckdb_execution": execution,
        }
    job_id = required_nested_scalar(
        run_payload,
        ("result.query_job.id", "result.queryJob.id", "result.job_id", "result.id"),
        "job id",
    )
    return {"query_id": query_id, "job_id": job_id}


def prepared_source_jobs(client, metadata, wait, timeout_seconds, interval_seconds):
    """Poll and validate every exact warehouse source before downloads."""
    ready_jobs = {}
    pending = []
    for source in metadata["duckdb_execution"]["sources"]:
        source_job_id = str(source.get("query_job_id", "")).strip()
        if not source_job_id:
            continue
        query_job = wait_for_query_job(client, source_job_id, wait, timeout_seconds, interval_seconds)
        if not is_terminal_done_status(query_job):
            pending.append({
                "job_id": source_job_id,
                "dataset_id": source["dataset_id"],
                "job_status": query_job_status(query_job),
            })
            continue
        if str(query_job.get("dataset_id", "")).strip() != source["dataset_id"]:
            raise ValueError("DuckDB source job dataset_id does not match the prepared source.")
        result_id = str(query_job.get("job_result_id", "")).strip()
        if not result_id:
            raise ValueError("DuckDB source job_result_id is missing.")
        extension = str(query_job.get("result_extension", "")).strip().lower()
        if extension != str(source["extension"]).strip().lower():
            raise ValueError("DuckDB source result_extension does not match the prepared source.")
        ready_jobs[source_job_id] = query_job
    return ready_jobs, pending


def download_prepared_source(client, source, index, ready_jobs, directory, _deadline=None):
    """Download one validated file or warehouse revision into the work directory."""
    dataset_id = str(source["dataset_id"]).strip()
    extension = str(source["extension"]).strip().lower()
    source_id = str(source.get("file_source_id", "")).strip()
    if not source_id:
        source_job_id = str(source["query_job_id"]).strip()
        source_id = str(ready_jobs[source_job_id]["job_result_id"]).strip()
    source_path = Path(directory) / "dekart_source_{0}.{1}".format(index, extension)
    _download_dataset_source(client, dataset_id, source_id, extension, source_path, _deadline=_deadline)
    return source_path


def materialize_duckdb_query(client, metadata, out_dir, wait, timeout_seconds, interval_seconds,
                             memory_limit=None, duckdb_timeout_seconds=None):
    """Resolve prepared inputs and materialize one local DuckDB result."""
    ready_jobs, pending = prepared_source_jobs(client, metadata, wait, timeout_seconds, interval_seconds)
    if pending:
        return {
            "query_id": metadata["query_id"],
            "job_id": metadata["job_id"],
            "dataset_id": metadata["query_job"]["dataset_id"],
            "pending_source_jobs": pending,
            "terminal_status": "PENDING",
        }

    deadline = duckdb_execution._Deadline(duckdb_timeout_seconds) if duckdb_timeout_seconds is not None else None
    output_path = Path(out_dir).expanduser() / "{0}.parquet".format(metadata["job_id"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dekart-duckdb-") as directory:
        source_paths = [
            download_prepared_source(client, source, index, ready_jobs, directory, _deadline=deadline)
            for index, source in enumerate(metadata["duckdb_execution"]["sources"])
        ]
        # Keep the recorded suffix, including gzip, for DuckDB format detection.
        def download_http_source(source, index, resolved_url):
            source_path = Path(directory) / Path(source["file_name"]).name
            try:
                _download_dataset_source(client,
                    source["dataset_id"], source["http_source_id"], source["extension"],
                    source_path, timeout_seconds=timeout_seconds, url=resolved_url, _deadline=deadline,
                )
            except urllib.error.HTTPError as exc:
                message, _payload = parse_http_error_body(exc)
                raise RuntimeError(message) from exc
            return source_path

        temporary_result = Path(directory) / "dekart_result.parquet"
        execution_options = {}
        if memory_limit is not None:
            execution_options["memory_limit"] = memory_limit
        if deadline is not None:
            execution_options["_deadline"] = deadline
            deadline.remaining()
        duckdb_execution.execute_program(
            metadata["duckdb_execution"],
            metadata["query_job"],
            source_paths,
            temporary_result,
            directory,
            download_http_source,
            **execution_options,
        )
        if deadline is not None:
            deadline.remaining()
        shutil.move(str(temporary_result), str(output_path))
    return {
        "query_id": metadata["query_id"],
        "job_id": metadata["job_id"],
        "dataset_id": metadata["query_job"]["dataset_id"],
        "terminal_status": "JOB_STATUS_DONE",
        "result_extension": "parquet",
        "result_file": str(output_path),
        "path": str(output_path),
        "bytes": output_path.stat().st_size,
    }


def upload_parts_from_bytes(client, file_bytes, start_result, upload_part_endpoint, required_headers_json, max_part_size, file_id):
    """Upload inline content through the same multipart flow as CLI files."""
    if not file_bytes:
        raise ValueError("File is empty.")
    return _upload_parts_from_stream(
        client, io.BytesIO(file_bytes), len(file_bytes), start_result,
        upload_part_endpoint, required_headers_json, max_part_size, file_id,
    )


def _upload_parts_from_stream(client, source, file_size, start_result, upload_part_endpoint, required_headers_json, max_part_size, file_id):
    """Upload local file bytes using one upload session result payload."""
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
    dekart_url = client.dekart_url

    parts = []
    part_number = 1
    while True:
        chunk = source.read(resolved_max_part_size)
        if not chunk:
            break
        put_url = build_put_url(dekart_url, resolved_endpoint, part_number, len(chunk))
        part_response = client._upload_part_binary(put_url, chunk, required_headers)
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
