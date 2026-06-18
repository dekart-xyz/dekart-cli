import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
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


class ReportURLTest(unittest.TestCase):
    def test_report_url_helper_uses_configured_base_url(self):
        self.assertEqual(
            cli.report_url_for_report_id("report-1", dekart_url="http://localhost:8080"),
            "http://localhost:8080/reports/report-1",
        )

    def test_report_url_helper_escapes_report_id(self):
        self.assertEqual(
            cli.report_url_for_report_id("space/report", dekart_url="https://dekart.example"),
            "https://dekart.example/reports/space%2Freport",
        )

    @mock.patch.object(cli, "get_dekart_url", return_value="http://localhost:8080")
    def test_handle_report_url_prints_plain_url(self, _get_dekart_url):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            status = cli.handle_report_url("report-1", raw_json=False)

        self.assertEqual(status, 0)
        self.assertEqual(stdout.getvalue(), "http://localhost:8080/reports/report-1\n")

    @mock.patch.object(cli, "get_dekart_url", return_value="http://localhost:8080")
    def test_handle_report_url_prints_json(self, _get_dekart_url):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            status = cli.handle_report_url("report-1", raw_json=True)

        self.assertEqual(status, 0)
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {"report_id": "report-1", "report_url": "http://localhost:8080/reports/report-1"},
        )

    def test_handle_report_url_rejects_empty_report_id(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = cli.handle_report_url("", raw_json=True)

        self.assertEqual(status, 2)
        self.assertIn("Invalid --report-id", stderr.getvalue())


class MCPCallURLTest(unittest.TestCase):
    @mock.patch.object(cli, "get_auth_headers", return_value={})
    @mock.patch.object(cli, "get_dekart_url", return_value="http://localhost:8080")
    @mock.patch.object(
        cli.urllib.request,
        "urlopen",
        return_value=FakeResponse({"result": {"report": {"id": "report-1"}}}),
    )
    def test_get_report_properties_returns_raw_payload(self, _urlopen, _get_dekart_url, _get_auth_headers):
        payload = cli.mcp_call("get_report_properties", {"report_id": "report-1"})

        self.assertEqual(payload, {"result": {"report": {"id": "report-1"}}})

    @mock.patch.object(cli, "get_auth_headers", return_value={})
    @mock.patch.object(cli, "get_dekart_url", return_value="http://localhost:8080")
    @mock.patch.object(
        cli.urllib.request,
        "urlopen",
        return_value=FakeResponse({"result": {"report_path": "/reports/report-1"}}),
    )
    def test_create_report_metadata_return_keeps_raw_payload(self, _urlopen, _get_dekart_url, _get_auth_headers):
        payload, metadata = cli.mcp_call("create_report", {}, return_metadata=True)

        self.assertEqual(payload, {"result": {"report_path": "/reports/report-1"}})
        self.assertEqual(metadata["endpoint"], "http://localhost:8080/api/v1/mcp/call")


if __name__ == "__main__":
    unittest.main()
