import json
import unittest
from unittest import mock

from dekart import cli


class FakeResponse:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback):
        return False

    def read(self):
        return self.body


class NormalizeMCPCallResponseTest(unittest.TestCase):
    def test_adds_report_url_from_report_path(self):
        payload = {"result": {"report_path": "/reports/report-1"}}

        normalized = cli.normalize_mcp_call_response("create_report", payload, "http://localhost:8080")

        self.assertEqual(normalized["result"]["report_url"], "http://localhost:8080/reports/report-1")

    def test_preserves_server_report_url(self):
        payload = {
            "result": {
                "report_path": "/reports/report-1",
                "report_url": "https://maps.example.com/reports/report-1",
            }
        }

        normalized = cli.normalize_mcp_call_response("create_report", payload, "https://api.example.com")

        self.assertEqual(normalized["result"]["report_url"], "https://maps.example.com/reports/report-1")

    def test_adds_report_url_from_nested_report_id(self):
        payload = {"result": {"report": {"id": "report-1"}}}

        normalized = cli.normalize_mcp_call_response("create_report", payload, "http://localhost:8080")

        self.assertEqual(normalized["result"]["report_url"], "http://localhost:8080/reports/report-1")

    def test_adds_report_url_for_get_report_properties(self):
        payload = {"result": {"datasets": [], "queries": [], "report": {"id": "report-1"}}}

        normalized = cli.normalize_mcp_call_response("get_report_properties", payload, "http://localhost:8080")

        self.assertEqual(normalized["result"]["report_url"], "http://localhost:8080/reports/report-1")

    def test_nested_report_path_wins_over_id_fallback(self):
        payload = {"result": {"report": {"id": "report-1", "report_path": "/reports/canonical"}}}

        normalized = cli.normalize_mcp_call_response("get_report_properties", payload, "http://localhost:8080")

        self.assertEqual(normalized["result"]["report_url"], "http://localhost:8080/reports/canonical")

    def test_does_not_add_report_url_without_report_identity(self):
        payload = {"result": {"queries": []}}

        normalized = cli.normalize_mcp_call_response("get_query", payload, "http://localhost:8080")

        self.assertNotIn("report_url", normalized["result"])

    def test_adds_report_url_for_discovered_report_tool_name(self):
        payload = {"result": {"report_path": "/reports/report-1"}}

        normalized = cli.normalize_mcp_call_response("create_map_report", payload, "http://localhost:8080")

        self.assertEqual(normalized["result"]["report_url"], "http://localhost:8080/reports/report-1")

    def test_passes_through_other_response_shapes(self):
        payloads = [
            ("create_report", {}),
            ("create_report", {"result": "not-an-object"}),
        ]

        for name, payload in payloads:
            with self.subTest(name=name, payload=payload):
                self.assertIs(cli.normalize_mcp_call_response(name, payload, "http://localhost:8080"), payload)


class MCPCallURLTest(unittest.TestCase):
    @mock.patch.object(cli, "get_auth_headers", return_value={})
    @mock.patch.object(cli, "get_dekart_url", return_value="http://localhost:8080")
    @mock.patch.object(
        cli.urllib.request,
        "urlopen",
        return_value=FakeResponse({"result": {"report": {"id": "report-1"}}}),
    )
    def test_get_report_properties_returns_absolute_url(self, _urlopen, _get_dekart_url, _get_auth_headers):
        payload = cli.mcp_call("get_report_properties", {"report_id": "report-1"})

        self.assertEqual(payload["result"]["report_url"], "http://localhost:8080/reports/report-1")

    @mock.patch.object(cli, "get_auth_headers", return_value={})
    @mock.patch.object(cli, "get_dekart_url", return_value="http://localhost:8080")
    @mock.patch.object(
        cli.urllib.request,
        "urlopen",
        return_value=FakeResponse({"result": {"report_path": "/reports/report-1"}}),
    )
    def test_create_report_metadata_return_includes_absolute_url(self, _urlopen, _get_dekart_url, _get_auth_headers):
        payload, metadata = cli.mcp_call("create_report", {}, return_metadata=True)

        self.assertEqual(payload["result"]["report_url"], "http://localhost:8080/reports/report-1")
        self.assertEqual(metadata["endpoint"], "http://localhost:8080/api/v1/mcp/call")


if __name__ == "__main__":
    unittest.main()
