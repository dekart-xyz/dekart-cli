import io
import json
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from dekart import cli


class ResolveDekartUrlReferenceTest(unittest.TestCase):
    @mock.patch.object(cli, "get_dekart_url", return_value="https://example.com/dekart")
    def test_resolves_root_relative_url_against_origin(self, _get_dekart_url):
        resolved = cli.resolve_dekart_url_reference("/reports/report-1/snapshot?token=abc")

        self.assertEqual(resolved, "https://example.com/reports/report-1/snapshot?token=abc")

    @mock.patch.object(cli, "get_dekart_url", return_value="https://example.com/dekart")
    def test_resolves_path_relative_url_against_base_path(self, _get_dekart_url):
        resolved = cli.resolve_dekart_url_reference("reports/report-1/snapshot?token=abc")

        self.assertEqual(resolved, "https://example.com/dekart/reports/report-1/snapshot?token=abc")

    @mock.patch.object(cli, "get_dekart_url", return_value="https://example.com/dekart")
    def test_preserves_absolute_url(self, _get_dekart_url):
        url = "https://snapshots.example.net/report.png?signature=abc"

        self.assertEqual(cli.resolve_dekart_url_reference(url), url)

    @mock.patch.object(cli, "get_dekart_url")
    def test_preserves_empty_url_without_loading_config(self, get_dekart_url):
        self.assertEqual(cli.resolve_dekart_url_reference(""), "")
        get_dekart_url.assert_not_called()

    @mock.patch.object(cli, "get_dekart_url")
    def test_explicit_base_url_does_not_load_config(self, get_dekart_url):
        resolved = cli.resolve_dekart_url_reference("/device/authorize?device_id=device-1", "https://new.example.com")

        self.assertEqual(resolved, "https://new.example.com/device/authorize?device_id=device-1")
        get_dekart_url.assert_not_called()


class HandleSnapshotUrlTest(unittest.TestCase):
    def run_snapshot(self, snapshot_result, *, remote_only=False, raw_json=False, debug=False):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(cli, "mcp_call", return_value={"result": snapshot_result}))
            stack.enter_context(mock.patch.object(cli, "get_dekart_url", return_value="http://localhost:8080"))
            stack.enter_context(
                mock.patch.object(cli, "load_config", return_value={"local_snapshot": {"enabled": True}})
            )
            render = stack.enter_context(mock.patch.object(cli, "render_local_snapshot_png", return_value=b"local-png"))
            download = stack.enter_context(mock.patch.object(cli, "download_binary", return_value=b"remote-png"))
            stack.enter_context(mock.patch.object(cli, "save_binary_file", return_value=Path("/tmp/snapshot.png")))
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            status = cli.handle_snapshot(
                report_id="report-1",
                out="/tmp/snapshot.png",
                timeout=90,
                width=1600,
                height=900,
                remote_only=remote_only,
                raw_json=raw_json,
                debug=debug,
            )
        return status, stdout.getvalue(), stderr.getvalue(), render, download

    def test_local_snapshot_resolves_render_url_and_reports_it(self):
        status, stdout, stderr, render, download = self.run_snapshot(
            {
                "snapshot_render_url": "/reports/report-1/snapshot?snapshot_token=abc",
            },
            raw_json=True,
            debug=True,
        )

        expected_url = "http://localhost:8080/reports/report-1/snapshot?snapshot_token=abc"
        self.assertEqual(status, 0)
        render.assert_called_once_with(expected_url, width=1600, height=900, timeout_seconds=90)
        download.assert_not_called()
        self.assertIn(f"[debug] local_snapshot_render_url={expected_url}", stderr)
        self.assertEqual(json.loads(stdout)["snapshot_render_url"], expected_url)

    def test_remote_snapshot_resolves_download_url_and_reports_it(self):
        status, stdout, _stderr, render, download = self.run_snapshot(
            {
                "snapshot_url": "/reports/report-1/snapshot.png?token=abc",
            },
            remote_only=True,
            raw_json=True,
        )

        expected_url = "http://localhost:8080/reports/report-1/snapshot.png?token=abc"
        self.assertEqual(status, 0)
        render.assert_not_called()
        download.assert_called_once_with(expected_url, timeout_seconds=90)
        self.assertEqual(json.loads(stdout)["snapshot_url"], expected_url)


if __name__ == "__main__":
    unittest.main()
