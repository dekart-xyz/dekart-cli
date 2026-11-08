"""Stateless HTTP access to one Dekart, using the caller's device token."""
import base64
import binascii
from contextlib import asynccontextmanager
from contextvars import ContextVar
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.error
from urllib.parse import quote, urljoin, urlsplit

import anyio
import httpx
import jsonschema
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from dekart.client import (
    DekartClient, download_query_job_result, materialize_duckdb_query,
    run_prepared_query, upload_parts_from_bytes, wait_for_query_job, parse_http_error_body,
)
from dekart.snapshot import render_local_snapshot_png

_client = ContextVar("dekart_mcp_client")
class _ToolError(ValueError):
    pass


HEAVY_TOOLS = {"run_query", "create_report_snapshot"}


def _tool(name, description, properties, required):
    return types.Tool(name=name, description=description, inputSchema={
        "type": "object", "properties": properties, "required": required,
        "additionalProperties": False,
    })


def _own_tools(settings):
    string = {"type": "string", "minLength": 1}
    return [
        _tool("run_query", "Run a query and return row count, schema and bounded rows. "
              "Use job_id to read an existing warehouse job without starting another. "
              "DuckDB queries also run in users' browsers; simplify slow SQL or set duckdb_timeout_seconds.", {
            "query_id": string, "query_params_values": {"type": "string"}, "job_id": string,
            "wait": {"type": "boolean", "default": True},
            "limit": {"type": "integer", "minimum": 0, "default": 50},
            "schema_only": {"type": "boolean", "default": False},
            "duckdb_timeout_seconds": {"type": "number", "exclusiveMinimum": 0,
                "maximum": settings["duckdb_max"], "default": settings["duckdb_timeout"]},
        }, ["query_id"]),
        _tool("create_report_snapshot", "Render a report as a PNG image. Pass zoom, lat and lon "
              "when the framing looks wrong; include_widgets includes charts.", {
            "report_id": string,
            "zoom": {"type": "number", "minimum": 0, "maximum": 24},
            "lat": {"type": "number", "minimum": -90, "maximum": 90},
            "lon": {"type": "number", "minimum": -180, "maximum": 180},
            "include_widgets": {"type": "boolean"},
            "width": {"type": "integer", "minimum": 1, "maximum": 4096},
            "height": {"type": "integer", "minimum": 1, "maximum": 4096},
            "timeout_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 120},
        }, ["report_id"]),
        _tool("upload_file", "Upload inline text or base64 content to a Dekart file. File paths are not accepted.", {
            "file_id": string, "name": string, "content": {"type": "string"},
            "content_base64": {"type": "string"}, "mime_type": string,
        }, ["file_id", "name"]),
        _tool("get_report_url", "Get the URL a user can open for a report.", {"report_id": string}, ["report_id"]),
    ]


def _settings():
    url = os.environ.get("DEKART_URL", "").rstrip("/")
    if not url:
        raise ValueError("Set DEKART_URL to the Dekart instance URL.")
    settings = {"url": url, "app_url": os.environ.get("DEKART_APP_URL", url).rstrip("/"),
                "heavy_url": os.environ.get("GEOSQL_MCP_HEAVY_SERVICE_URL", ""),
                "memory_limit": os.environ.get("GEOSQL_MCP_DUCKDB_MEMORY_LIMIT", "1GB")}
    for key, env, default in [
        ("rows", "ROW_LIMIT", 1000), ("bytes", "BYTE_LIMIT", 1048576),
        ("upload_bytes", "UPLOAD_LIMIT", 2097152), ("wait", "WAIT_SECONDS", 45),
        ("duckdb_timeout", "DUCKDB_TIMEOUT_SECONDS", 10), ("duckdb_max", "DUCKDB_MAX_TIMEOUT_SECONDS", 120),
    ]:
        settings[key] = int(os.environ.get("GEOSQL_MCP_" + env, default))
        if settings[key] <= 0:
            raise ValueError("GEOSQL_MCP_" + env + " must be positive.")
    if not settings["duckdb_timeout"] <= settings["duckdb_max"] <= 120:
        raise ValueError("DuckDB deadlines must satisfy default <= maximum <= 120 seconds.")
    for target in (settings["url"], settings["app_url"], settings["heavy_url"]):
        if target and (_origin(target) is None or urlsplit(target).username or urlsplit(target).password):
            raise ValueError("Server URLs must be HTTP(S) URLs without credentials.")
    return settings


def _origin(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


def _clean(value, token):
    # Internal transport fields and render credentials must never reach the agent.
    if isinstance(value, dict):
        return {key: _clean(item, token) for key, item in value.items()
                if key.lower() not in {"authorization", "snapshot_token", "snapshot_render_url",
                                       "path", "result_file", "result_path", "file_path"}}
    if isinstance(value, list):
        return [_clean(item, token) for item in value]
    if isinstance(value, str):
        if token:
            value = value.replace(token, "[redacted]")
        value = re.sub(r'(?i)(snapshot_token=)[^&\s"<>]+', r'\1[redacted]', value)
        value = re.sub(r'(?i)Bearer\s+[^\s"<>]+', 'Bearer [redacted]', value)
    return value


def _safe_error(message, token):
    message = _clean(message, token)
    message = re.sub(r"(?<![\w:/])/(?:tmp|private|var|Users|home|etc|opt|root|srv|app)(?:/[^\s\"<>]+)+", "[local path]", message)
    message = re.sub(r"[A-Za-z]:[\\/][^\s\"<>]+", "[local path]", message)
    return message


def _text(value):
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(value, default=str))])


def _error(message):
    return types.CallToolResult(isError=True, content=[types.TextContent(type="text", text=message)])


def _http_error(exc, token):
    if exc.code == 401:
        return "Create a new token in Dekart"
    if exc.code == 429:
        return "Busy, retry"
    try:
        _body, payload = parse_http_error_body(exc)
        message = payload.get("message") or payload.get("error", {}).get("message")
    except (ValueError, AttributeError, TypeError):
        message = None
    return _safe_error(message, token) if isinstance(message, str) else "Dekart request failed (HTTP %s)." % exc.code


def _preview(path, extension, limit, schema_only, settings, job_id):
    import duckdb
    with duckdb.connect(":memory:") as connection:
        connection.execute("SET memory_limit = ?", [settings["memory_limit"]])
        relation = connection.read_parquet(str(path)) if extension == "parquet" else connection.read_csv(str(path))
        count = relation.count("*").fetchone()[0]
        schema = [{"name": name, "type": str(kind)} for name, kind in zip(relation.columns, relation.types)]
        result = {"job_id": job_id, "row_count": count, "schema": schema, "rows": [], "truncated": True}
        if len(json.dumps(result).encode()) + 1 > settings["bytes"]:
            raise _ToolError("Result schema exceeds the byte cap. Select fewer columns.")
        if not schema_only:
            for row in relation.limit(min(limit, settings["rows"])).fetchall():
                row = list(row)
                result["rows"].append(row)
                if len(json.dumps(result, default=str).encode()) + 1 > settings["bytes"]:
                    result["rows"].pop()
                    break
            result["truncated"] = len(result["rows"]) < count
        return result


def _run_query(client, args, settings):
    with tempfile.TemporaryDirectory(prefix="dekart-mcp-") as directory:
        if args.get("job_id"):
            meta = {"job_id": args["job_id"]}
        else:
            meta = run_prepared_query(client, args["query_id"], args.get("query_params_values"), 30)
        job_id = meta["job_id"]
        if "duckdb_execution" in meta:
            result = materialize_duckdb_query(client, meta, directory, True, settings["wait"], 2,
                memory_limit=settings["memory_limit"],
                duckdb_timeout_seconds=args.get("duckdb_timeout_seconds", settings["duckdb_timeout"]))
        else:
            try:
                job = wait_for_query_job(client, job_id, args.get("wait", True), settings["wait"], 2)
            except TimeoutError:
                return {"job_id": job_id, "message": "Still running. Use check_job_status, then run_query with this job_id."}
            if job.get("job_status") != "JOB_STATUS_DONE":
                return {"job_id": job_id, "message": "Still running. Use check_job_status, then run_query with this job_id."}
            if str(job.get("query_id", "")) != args["query_id"]:
                raise _ToolError("job_id does not belong to query_id.")
            # Dekart marks DuckDB jobs done before local execution; they cannot be resumed.
            if args.get("job_id") and not job.get("job_result_id"):
                raise _ToolError("DuckDB queries cannot be resumed by job_id. Run the query again without job_id.")
            result = download_query_job_result(client, job_id, job, Path(directory) / "result")
        if result.get("pending_source_jobs"):
            raise _ToolError("DuckDB source jobs are still running. Retry run_query without job_id.")
        preview = _preview(result["path"], result["result_extension"], args.get("limit", 50), args.get("schema_only", False), settings, job_id)
        return preview


def _snapshot(client, args, settings):
    if ("lat" in args) != ("lon" in args):
        raise _ToolError("Pass both lat and lon.")
    options = {key: value for key, value in args.items() if key in {"report_id", "zoom", "lat", "lon", "include_widgets"}}
    result = client.call("create_report_snapshot", options, args.get("timeout_seconds", 90))["result"]
    url = urljoin(settings["url"] + "/", result.get("snapshot_render_url", ""))
    if not result.get("snapshot_render_url") or _origin(url) not in {_origin(settings["url"]), _origin(settings["app_url"])}:
        raise _ToolError("Snapshot render URL must use the DEKART_URL or DEKART_APP_URL origin.")
    expires = result.get("expires_in")
    png = render_local_snapshot_png(url, args.get("width", 1600), args.get("height", 900),
        args.get("timeout_seconds", 90), expires_at=time.monotonic() + float(expires) if expires else None)
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise _ToolError("Snapshot renderer did not return a PNG image.")
    return types.CallToolResult(content=[types.ImageContent(type="image", mimeType="image/png", data=base64.b64encode(png).decode())])


def _upload(client, args, settings):
    if ("content" in args) == ("content_base64" in args):
        raise _ToolError("Pass exactly one of content or content_base64; file paths are not accepted.")
    if "content_base64" in args:
        if len(args["content_base64"]) > 4 * ((settings["upload_bytes"] + 2) // 3):
            raise _ToolError("Upload exceeds the inline byte cap.")
        try:
            data = base64.b64decode(args["content_base64"], validate=True)
        except (ValueError, binascii.Error):
            raise _ToolError("content_base64 must be valid base64.") from None
    else:
        data = args["content"].encode("utf-8")
    if len(data) > settings["upload_bytes"]:
        raise _ToolError("Upload exceeds the inline byte cap.")
    if not data:
        raise _ToolError("File is empty.")
    start = client.call("start_file_upload_session", {
        "file_id": args["file_id"], "name": args["name"], "total_size": len(data),
        "mime_type": args.get("mime_type") or mimetypes.guess_type(args["name"])[0] or "application/octet-stream",
    })["result"]
    uploaded = upload_parts_from_bytes(client, data, start, "", "", None, args["file_id"])
    return client.call("complete_file_upload_session", uploaded["complete_args"])


def create_app():
    # SDK debug records include whole requests; transport tracebacks can include validation input.
    for name in ("mcp.server.lowlevel.server", "mcp.server.streamable_http", "mcp.server.streamable_http_manager"):
        logging.getLogger(name).disabled = True
    settings = _settings()
    own = {tool.name: tool for tool in _own_tools(settings)}
    server = Server("geosql")

    # Register directly: SDK decorators cache tools across callers, while Dekart roles can change per request.
    async def list_tools(request):
        client, token = _client.get()
        try:
            payload = await anyio.to_thread.run_sync(client.list_tools)
            tools = [types.Tool.model_validate(tool) for tool in payload.get("tools", []) if tool["name"] not in own]
            return types.ServerResult(types.ListToolsResult(tools=tools + list(own.values())))
        except urllib.error.HTTPError as exc:
            raise ValueError(_http_error(exc, token)) from None
        except Exception:
            raise ValueError("Could not list Dekart tools. Check DEKART_URL and your token.") from None

    def call(name, args, client, token):
        try:
            tools = {tool["name"]: tool for tool in client.list_tools().get("tools", [])}
            definition = own.get(name)
            if definition:
                schema = definition.inputSchema
            elif name in tools:
                schema = tools[name]["inputSchema"]
            else:
                return _error("Unknown or disabled tool.")
            try:
                jsonschema.validate(args, schema)
            except jsonschema.ValidationError:
                return _error("Invalid arguments. Follow the tool's input schema; local file paths are not accepted.")
            if name == "create_report_snapshot":
                return _snapshot(client, args, settings)
            if name == "run_query":
                value = _run_query(client, args, settings)
            elif name == "upload_file":
                value = _upload(client, args, settings)
            elif name == "get_report_url":
                value = {"url": settings["app_url"] + "/reports/" + quote(args["report_id"], safe="")}
            else:
                value = client.call(name, args)
            return _text(_clean(value, token))
        except urllib.error.HTTPError as exc:
            return _error(_http_error(exc, token))
        except TimeoutError:
            return _error("Query timed out. DuckDB queries also run in users' browsers; simplify the query or pass duckdb_timeout_seconds (up to %s)." % settings["duckdb_max"])
        except _ToolError as exc:
            return _error(_safe_error(str(exc), token))
        except RuntimeError as exc:
            if name == "run_query":
                return _error(_safe_error(str(exc), token))
            return _error("Tool execution failed. Check the Dekart server and MCP runtime, then retry.")
        except Exception:
            return _error("Tool execution failed. Check the Dekart server and MCP runtime, then retry.")

    async def call_tool(request):
        client, token = _client.get()
        result = await anyio.to_thread.run_sync(call, request.params.name, request.params.arguments or {}, client, token)
        return types.ServerResult(result)

    server.request_handlers[types.ListToolsRequest] = list_tools
    server.request_handlers[types.CallToolRequest] = call_tool
    manager = StreamableHTTPSessionManager(server, stateless=True, json_response=True,
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        max_request_body_size=max(4194304, settings["upload_bytes"] * 2 + 65536))

    @asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield

    async def mcp_endpoint(request: Request):
        if "origin" in request.headers:
            return Response("Browser Origin requests are not allowed.", status_code=403)
        authorization = request.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return Response("Create a new token in Dekart", status_code=401)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > max(4194304, settings["upload_bytes"] * 2 + 65536):
                return Response("Request exceeds the byte cap.", status_code=413)
        try:
            payload = json.loads(body) if body else {}
        except (ValueError, UnicodeDecodeError):
            return Response("Invalid JSON.", status_code=400)
        if not isinstance(payload, dict):
            return Response("Expected one MCP request.", status_code=400)
        if (settings["heavy_url"] and payload.get("method") == "tools/call"
                and isinstance(payload.get("params"), dict) and payload["params"].get("name") in HEAVY_TOOLS):
            target = settings["heavy_url"].rstrip("/")
            if not target.endswith("/mcp"):
                target += "/mcp"
            headers = {key: request.headers[key] for key in ("content-type", "accept", "mcp-protocol-version") if key in request.headers}
            headers["authorization"] = authorization
            try:
                async with httpx.AsyncClient(timeout=240, follow_redirects=False) as transport:
                    upstream = await transport.post(target, content=bytes(body), headers=headers)
                if upstream.status_code == 429:
                    return JSONResponse({"jsonrpc": "2.0", "id": payload.get("id"), "result": _error("Busy, retry").model_dump(by_alias=True, exclude_none=True)})
                return Response(upstream.content, status_code=upstream.status_code,
                                media_type=upstream.headers.get("content-type", "application/json"))
            except httpx.HTTPError:
                return Response("Heavy service unavailable. Retry.", status_code=502)
        context = _client.set((DekartClient(settings["url"], token), token))
        try:
            async def receive():
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            await manager.handle_request(request.scope, receive, request._send)
            return None
        finally:
            _client.reset(context)

    # Use an ASGI endpoint so the SDK writes its own response.
    class MCPEndpoint:
        async def __call__(self, scope, receive, send):
            response = await mcp_endpoint(Request(scope, receive, send))
            if response is not None:
                await response(scope, receive, send)

    async def extension(request):
        path = Path(__file__).parent / "geosql.mcpb"
        if not path.is_file():
            return Response("Desktop extension is not installed in this build.", status_code=404)
        return FileResponse(path, filename="geosql.mcpb", media_type="application/octet-stream")

    # Stateless JSON requests need no server-to-client stream or session deletion.
    return Starlette(routes=[Route("/mcp", MCPEndpoint(), methods=["POST"]),
                             Route("/geosql.mcpb", extension)], lifespan=lifespan)


def serve(host, port):
    import uvicorn
    uvicorn.run(create_app(), host=host, port=port, access_log=False)
