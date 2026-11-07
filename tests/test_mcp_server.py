"""Exercise the HTTP boundary with real Dekart HTTP requests and isolated credentials."""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread
from unittest import mock

import pytest

pytest.importorskip("mcp")
from starlette.testclient import TestClient
from dekart import mcp_server as mcp

HEADERS = {"Authorization": "Bearer editor-token", "Accept": "application/json, text/event-stream"}
PNG = b"\x89PNG\r\n\x1a\nimage"


def rpc(client, method, params=None, token=None):
    headers = dict(HEADERS)
    if token:
        headers["Authorization"] = "Bearer " + token
    return client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def call(client, name, args, token=None):
    response = rpc(client, "tools/call", {"name": name, "arguments": args}, token)
    assert response.status_code == 200, response.text
    return response.json()["result"]


def decoded(result):
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


@pytest.fixture
def backend(monkeypatch):
    state = {"requests": [], "status": "JOB_STATUS_DONE", "snapshot_origin": None, "calls": [], "disabled": False}
    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            payload = json.loads(body) if body and self.command != "PUT" else None
            state["requests"].append((self.path, dict(self.headers), payload))
            if self.headers.get("Authorization") == "Bearer revoked":
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'{"message":"expired"}')
                return
            if self.headers.get("Authorization") == "Bearer viewer":
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b'{"message":"Only editors can use GeoSQL in Claude"}')
                return
            if self.path == "/api/v1/mcp/tools":
                tools = [{"name": name, "description": "from Dekart", "inputSchema": {"type": "object", "properties": {}}}
                         for name in ["echo", "run_query", "create_report_snapshot"]]
                tools[1]["inputSchema"]["properties"]["accept_duckdb_execution"] = {"type": "boolean"}
                if not state["disabled"]:
                    tools.append({"name": "create_connection", "inputSchema": {"type": "object"}})
                value = {"tools": tools}
            elif self.path == "/api/v1/mcp/call":
                name, args = payload["name"], payload["arguments"]
                state["calls"].append((name, args))
                if name == "run_query":
                    value = {"result": {"query_job": {"id": "job"}}}
                elif name == "check_job_status":
                    value = {"result": {"query_job": {"id": args["job_id"], "query_id": "query", "dataset_id": "dataset",
                        "job_result_id": "result", "job_status": state["status"], "result_extension": "csv"}}}
                elif name == "create_report_snapshot":
                    value = {"result": {"snapshot_render_url": (state["snapshot_origin"] or state["url"]) + "/render?snapshot_token=secret", "expires_in": 60}}
                elif name == "start_file_upload_session":
                    value = {"result": {"file_id": args["file_id"], "upload_session_id": "session", "max_part_size": 5,
                        "upload_part_endpoint": "/part/{part_number}"}}
                elif name == "complete_file_upload_session":
                    value = {"result": {"file_id": args["file_id"]}}
                else:
                    value = {"result": args}
            elif self.path.startswith("/api/v1/dataset-source/"):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"name,n\na,1\nb,2\nc,3\n")
                return
            else:
                value = {"etag": "etag", "size": length}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())
        do_GET = handle_request
        do_POST = handle_request
        do_PUT = handle_request
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["url"] = "http://127.0.0.1:%s" % server.server_port
    monkeypatch.setenv("DEKART_URL", state["url"])
    monkeypatch.delenv("DEKART_APP_URL", raising=False)
    monkeypatch.delenv("GEOSQL_MCP_HEAVY_SERVICE_URL", raising=False)
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def client(backend):
    with TestClient(mcp.create_app()) as client:
        yield client


def test_initialize_is_stateless_and_named(client):
    result = rpc(client, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})
    assert result.json()["result"]["serverInfo"]["name"] == "geosql"
    assert "mcp-session-id" not in result.headers


@pytest.mark.parametrize("enabled", [True, False])
def test_tools_preserve_upstream_schema_and_disabled_tools(client, backend, enabled):
    backend["disabled"] = not enabled
    result = rpc(client, "tools/list").json()["result"]["tools"]
    assert {tool["name"] for tool in result} == {"echo", "run_query", "create_report_snapshot", "upload_file", "get_report_url"} | ({"create_connection"} if enabled else set())
    assert next(t for t in result if t["name"] == "echo")["inputSchema"] == {"type": "object", "properties": {}}
    backend["disabled"] = True
    assert call(client, "create_connection", {})["isError"]


def test_auth_and_browser_origin(client):
    assert client.post("/mcp", json={}).status_code == 401
    assert client.post("/mcp", json={}, headers={**HEADERS, "Origin": "https://example.com"}).status_code == 403
    assert "Create a new token in Dekart" in json.dumps(call(client, "echo", {}, "revoked"))
    assert "Only editors" in json.dumps(call(client, "echo", {}, "viewer"))
    assert "Create a new token in Dekart" in rpc(client, "tools/list", token="revoked").text


def test_two_tokens_and_restart(backend):
    for _ in range(2):
        with TestClient(mcp.create_app()) as client:
            with ThreadPoolExecutor(2) as executor:
                results = list(executor.map(lambda token: decoded(call(client, "echo", {"value": token[-1]}, token)), ["token-a", "token-b"]))
            assert results == [{"result": {"value": "a"}}, {"result": {"value": "b"}}]
    for path, headers, payload in backend["requests"]:
        assert headers["User-Agent"].startswith("dekart-cli/")
        if payload and payload["name"] == "echo":
            assert headers["Authorization"] == "Bearer token-" + payload["arguments"]["value"]


def test_warehouse_rows_and_resume(client, backend):
    result = decoded(call(client, "run_query", {"query_id": "query", "limit": 2}))
    assert result["row_count"] == 3
    assert result["schema"] == [{"name": "name", "type": "VARCHAR"}, {"name": "n", "type": "BIGINT"}]
    assert result["rows"] == [["a", 1], ["b", 2]]
    assert result["truncated"]
    backend["calls"].clear()
    result = decoded(call(client, "run_query", {"query_id": "query", "job_id": "job", "schema_only": True}))
    assert result["rows"] == [] and result["row_count"] == 3
    assert all(name != "run_query" for name, _ in backend["calls"])


def test_pending_warehouse_returns_job_id(client, backend):
    backend["status"] = "JOB_STATUS_RUNNING"
    result = decoded(call(client, "run_query", {"query_id": "query", "wait": False}))
    assert result["job_id"] == "job"
    assert "check_job_status" in result["message"]
    with mock.patch.object(mcp, "wait_for_query_job", side_effect=TimeoutError):
        assert decoded(call(client, "run_query", {"query_id": "query"}))["job_id"] == "job"


@pytest.mark.parametrize("origin", [None, "https://app.example", "https://evil.example"])
def test_snapshot_origin_png_and_viewport(backend, monkeypatch, origin):
    monkeypatch.setenv("DEKART_APP_URL", "https://app.example")
    backend["snapshot_origin"] = origin
    with TestClient(mcp.create_app()) as client, mock.patch.object(mcp, "render_local_snapshot_png", return_value=PNG) as render:
        result = call(client, "create_report_snapshot", {"report_id": "report", "zoom": 12, "lat": 52, "lon": 13, "include_widgets": True})
        if origin == "https://evil.example":
            assert result["isError"] and "origin" in result["content"][0]["text"]
            render.assert_not_called()
        else:
            assert result["content"] == [{"type": "image", "data": base64.b64encode(PNG).decode(), "mimeType": "image/png"}]
            assert render.call_args.args[1:] == (1600, 900, 90)
            assert render.call_args.kwargs["expires_at"] > 0
            assert backend["calls"][-1][1] == {"report_id": "report", "zoom": 12, "lat": 52, "lon": 13, "include_widgets": True}
        assert "snapshot_token" not in json.dumps(result)


def test_upload_inline_and_caps(client, backend, monkeypatch):
    result = decoded(call(client, "upload_file", {"file_id": "file", "name": "a.csv", "content": "a,b\n1,2\n"}))
    assert result == {"result": {"file_id": "file"}}
    start = next(args for name, args in backend["calls"] if name == "start_file_upload_session")
    assert start["mime_type"] == "text/csv" and start["total_size"] == 8
    assert sum(path.startswith("/part/") for path, _, _ in backend["requests"]) == 2
    for args in [{"path": "/tmp/a.csv"}, {"content": "x", "content_base64": "eA=="}, {"content_base64": "%%%"}]:
        assert call(client, "upload_file", {"file_id": "file", "name": "a.csv", **args})["isError"]
    monkeypatch.setenv("GEOSQL_MCP_UPLOAD_LIMIT", "3")
    with TestClient(mcp.create_app()) as capped:
        assert call(capped, "upload_file", {"file_id": "file", "name": "a.csv", "content": "four"})["isError"]
        assert decoded(call(capped, "upload_file", {"file_id": "file", "name": "a.csv", "content_base64": "eA=="}))


def test_report_url(client):
    assert decoded(call(client, "get_report_url", {"report_id": "a/b"}))["url"].endswith("/reports/a%2Fb")


def test_sensitive_fields_and_exception_paths_never_escape(client, caplog, capsys):
    result = call(client, "echo", {"snapshot_token": "secret", "Authorization": "Bearer editor-token", "path": "/tmp/private", "message": "https://a/?snapshot_token=secret"})
    output = json.dumps(result) + caplog.text + str(capsys.readouterr())
    assert "secret" not in output and "editor-token" not in output and "/tmp/private" not in output
    with mock.patch.object(mcp, "_snapshot", side_effect=RuntimeError("/tmp/private snapshot_token=secret")):
        assert "/tmp/private" not in json.dumps(call(client, "create_report_snapshot", {"report_id": "report"}))


def test_duckdb_timeout_memory_and_preview(client):
    with mock.patch.object(mcp, "run_prepared_query", return_value={"job_id": "job", "duckdb_execution": {}}), mock.patch.object(mcp, "materialize_duckdb_query", side_effect=TimeoutError) as materialize:
        result = call(client, "run_query", {"query_id": "query"})
        assert "browsers" in result["content"][0]["text"] and "duckdb_timeout_seconds" in result["content"][0]["text"]
        assert materialize.call_args.kwargs == {"memory_limit": "1GB", "duckdb_timeout_seconds": 10}
        def write_result(*args, **kwargs):
            import duckdb
            path = Path(args[2]) / "result.parquet"
            with duckdb.connect() as connection:
                connection.execute("COPY (SELECT 1 n UNION ALL SELECT 2) TO ? (FORMAT PARQUET)", [str(path)])
            return {"path": str(path), "result_extension": "parquet"}
        materialize.side_effect = write_result
        result = decoded(call(client, "run_query", {"query_id": "query", "duckdb_timeout_seconds": 60, "limit": 1}))
        assert result["row_count"] == 2 and result["rows"] == [[1]]
        assert materialize.call_args.kwargs["duckdb_timeout_seconds"] == 60
        assert not Path(materialize.call_args.args[2]).exists()
    assert call(client, "run_query", {"query_id": "query", "duckdb_timeout_seconds": 121})["isError"]


def test_preview_byte_and_row_caps(backend, monkeypatch):
    monkeypatch.setenv("GEOSQL_MCP_ROW_LIMIT", "1")
    monkeypatch.setenv("GEOSQL_MCP_BYTE_LIMIT", "200")
    with TestClient(mcp.create_app()) as client:
        result = call(client, "run_query", {"query_id": "query", "limit": 100})
        data = decoded(result)
        assert len(data["rows"]) <= 1
        assert len(json.dumps(data).encode()) <= 200


@pytest.mark.parametrize("status", [200, 401, 429])
def test_heavy_forward(backend, monkeypatch, status):
    import httpx
    seen = []
    class Heavy(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, self.headers["Authorization"], body))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "heavy"}]}}).encode() if status == 200 else b'"upstream error"')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Heavy)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("GEOSQL_MCP_HEAVY_SERVICE_URL", "http://127.0.0.1:%s" % server.server_port)
    try:
        with TestClient(mcp.create_app()) as client:
            with ThreadPoolExecutor(2) as executor:
                results = list(executor.map(lambda pair: rpc(client, "tools/call", {"name": pair[0], "arguments": {}}, pair[1]), [("run_query", "one"), ("create_report_snapshot", "two")]))
            for response in results:
                if status == 401:
                    assert response.status_code == 401 and response.content == b'"upstream error"'
                elif status == 429:
                    assert response.status_code == 200 and "Busy, retry" in response.text
                else:
                    assert response.json()["result"]["content"][0]["text"] == "heavy"
            assert {(auth, payload["params"]["name"]) for _, auth, payload in seen} == {("Bearer one", "run_query"), ("Bearer two", "create_report_snapshot")}
            assert all(path == "/mcp" for path, _, _ in seen)
            assert decoded(call(client, "get_report_url", {"report_id": "report"}))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_extension_route(client, tmp_path):
    bundle = tmp_path / "geosql.mcpb"
    bundle.write_bytes(b"test bundle")
    with mock.patch.object(mcp, "__file__", str(tmp_path / "mcp_server.py")):
        response = client.get("/geosql.mcpb")
        assert response.status_code == 200 and response.content == b"test bundle"
        assert "geosql.mcpb" in response.headers["content-disposition"]


def test_real_duckdb_execution_and_memory(client):
    import duckdb
    metadata = {
        "query_id": "query", "job_id": "job",
        "query_job": {"id": "job", "query_id": "query", "dataset_id": "dataset", "job_status": "JOB_STATUS_DONE"},
        "duckdb_execution": {"duckdb_version": duckdb.__version__, "extensions": [], "sources": [], "statements": [
            {"sql": "CREATE SCHEMA dekart_internal"},
            {"sql": "CREATE TABLE dekart_internal.job_job AS SELECT current_setting('memory_limit') memory, i FROM range(3) t(i)"},
        ]},
    }
    with mock.patch.object(mcp, "run_prepared_query", return_value=metadata):
        result = decoded(call(client, "run_query", {"query_id": "query", "limit": 1}))
    assert result["row_count"] == 3 and len(result["rows"]) == 1
    assert result["rows"][0][0] in ("953.6 MiB", "1.0 GB")


def test_complete_preview_respects_exact_byte_cap(client, monkeypatch):
    args = {"query_id": "query", "limit": 1}
    baseline = decoded(call(client, "run_query", args))
    monkeypatch.setenv("GEOSQL_MCP_BYTE_LIMIT", str(len(json.dumps(baseline).encode()) - 1))
    with TestClient(mcp.create_app()) as capped:
        result = decoded(call(capped, "run_query", args))
    assert result["rows"] == []
    assert len(json.dumps(result).encode()) < len(json.dumps(baseline).encode())


def test_full_runtime_h3_offline(client):
    import os
    if not os.environ.get("GEOSQL_MCP_TEST_FULL_RUNTIME"):
        pytest.skip("Run in the full image with GEOSQL_MCP_TEST_FULL_RUNTIME=1")
    import duckdb
    from dekart import duckdb_execution
    metadata = {
        "query_id": "query", "job_id": "job",
        "query_job": {"id": "job", "query_id": "query", "dataset_id": "dataset", "job_status": "JOB_STATUS_DONE"},
        "duckdb_execution": {"duckdb_version": duckdb.__version__, "sources": [],
            "extensions": [{"name": "spatial", "repository": "core"}, {"name": "h3", "repository": "community"}],
            "statements": [{"sql": "CREATE SCHEMA dekart_internal"}, {"sql": "CREATE TABLE dekart_internal.job_job AS SELECT h3_latlng_to_cell(52.5, 13.4, 7) cell"}]},
    }
    original = duckdb_execution.load_extensions
    class OfflineConnection:
        def __init__(self, connection):
            self.connection = connection
        def execute(self, sql, *args):
            assert not sql.startswith("INSTALL"), "Extensions must be preinstalled in the full image"
            return self.connection.execute(sql, *args)
    def load(connection, extensions):
        return original(OfflineConnection(connection), extensions)
    with mock.patch.object(mcp, "run_prepared_query", return_value=metadata), mock.patch.object(duckdb_execution, "load_extensions", side_effect=load):
        result = decoded(call(client, "run_query", {"query_id": "query"}))
    assert result["row_count"] == 1 and len(result["rows"]) == 1


@pytest.mark.parametrize("stage", ["wait_for_query_job", "materialize_duckdb_query"])
def test_query_errors_are_actionable_without_local_paths_or_credentials(client, stage):
    meta = {"job_id": "job"}
    if stage == "materialize_duckdb_query":
        meta["duckdb_execution"] = {}
    error = "Binder Error: missing column at /tmp/private/result.parquet with Bearer editor-token"
    with mock.patch.object(mcp, "run_prepared_query", return_value=meta), mock.patch.object(mcp, stage, side_effect=RuntimeError(error)):
        result = call(client, "run_query", {"query_id": "query"})
    assert result["isError"] and "Binder Error: missing column" in result["content"][0]["text"]
    assert "/tmp/private" not in json.dumps(result) and "editor-token" not in json.dumps(result)


def test_sdk_debug_logging_never_records_upload_content(client, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    marker = "private-inline-upload-42"
    decoded(call(client, "upload_file", {"file_id": "file", "name": "a.txt", "content": marker}))
    assert marker not in caplog.text and "editor-token" not in caplog.text


def test_cli_serve_dispatch_skips_cli_config_and_telemetry(monkeypatch):
    from dekart import cli
    monkeypatch.setattr("sys.argv", ["dekart", "mcp", "serve", "--host", "0.0.0.0", "--port", "8123"])
    with mock.patch.object(mcp, "serve") as serve, mock.patch.object(cli, "send_version_ping", side_effect=AssertionError("telemetry")), mock.patch.object(cli, "load_config", side_effect=AssertionError("config")):
        cli.main()
    serve.assert_called_once_with("0.0.0.0", 8123)
