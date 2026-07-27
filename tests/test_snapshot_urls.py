import io
import json
import sys
import types
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from dekart import cli


PNG = b"\x89PNG\r\n\x1a\nsnapshot"


class ResolveDekartUrlReferenceTest(unittest.TestCase):
    @mock.patch.object(cli, "get_dekart_url", return_value="https://example.com/dekart")
    def test_resolves_root_relative_url_against_origin(self, _get_dekart_url):
        resolved = cli.resolve_dekart_url_reference(
            "/reports/report-1/snapshot?token=abc"
        )

        self.assertEqual(
            resolved, "https://example.com/reports/report-1/snapshot?token=abc"
        )

    @mock.patch.object(cli, "get_dekart_url", return_value="https://example.com/dekart")
    def test_resolves_path_relative_url_against_base_path(self, _get_dekart_url):
        resolved = cli.resolve_dekart_url_reference(
            "reports/report-1/snapshot?token=abc"
        )

        self.assertEqual(
            resolved,
            "https://example.com/dekart/reports/report-1/snapshot?token=abc",
        )

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
        resolved = cli.resolve_dekart_url_reference(
            "/device/authorize?device_id=device-1",
            "https://new.example.com",
        )

        self.assertEqual(
            resolved, "https://new.example.com/device/authorize?device_id=device-1"
        )
        get_dekart_url.assert_not_called()


class HandleSnapshotUrlTest(unittest.TestCase):
    def run_snapshot(
        self,
        snapshot_result,
        *,
        remote_only=False,
        raw_json=False,
        debug=False,
        zoom=None,
        lat=None,
        lon=None,
        render_result=PNG,
        render_side_effect=None,
        local_enabled=True,
        clock_value=100.0,
    ):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with ExitStack() as stack:
            mcp_call = stack.enter_context(
                mock.patch.object(
                    cli,
                    "mcp_call",
                    return_value={"result": snapshot_result},
                )
            )
            stack.enter_context(
                mock.patch.object(
                    cli,
                    "get_dekart_url",
                    return_value="http://localhost:8080",
                )
            )
            stack.enter_context(
                mock.patch.object(
                    cli,
                    "load_config",
                    return_value={"local_snapshot": {"enabled": local_enabled}},
                )
            )
            render = stack.enter_context(
                mock.patch.object(
                    cli,
                    "render_local_snapshot_png",
                    return_value=render_result,
                    side_effect=render_side_effect,
                )
            )
            download = stack.enter_context(
                mock.patch.object(cli, "download_binary", return_value=PNG)
            )
            save = stack.enter_context(
                mock.patch.object(
                    cli,
                    "save_binary_file",
                    return_value=Path("snapshot.png"),
                )
            )
            stack.enter_context(
                mock.patch.object(cli.time, "monotonic", return_value=clock_value)
            )
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            status = cli.handle_snapshot(
                report_id="report-1",
                out="snapshot.png",
                timeout=90,
                width=1600,
                height=900,
                remote_only=remote_only,
                raw_json=raw_json,
                debug=debug,
                zoom=zoom,
                lat=lat,
                lon=lon,
            )
        return {
            "status": status,
            "stdout": stdout.getvalue(),
            "stderr": stderr.getvalue(),
            "render": render,
            "download": download,
            "save": save,
            "mcp": mcp_call,
        }

    def test_local_snapshot_resolves_render_url_and_deadline(self):
        result = self.run_snapshot(
            {
                "snapshot_render_url": (
                    "/reports/report-1/snapshot?snapshot_token=abc"
                ),
                "expires_in": 30,
            },
            raw_json=True,
            debug=True,
        )

        expected_url = (
            "http://localhost:8080/reports/report-1/snapshot?snapshot_token=abc"
        )
        self.assertEqual(result["status"], 0)
        result["render"].assert_called_once_with(
            expected_url,
            width=1600,
            height=900,
            timeout_seconds=90,
            expires_at=130.0,
            debug=True,
        )
        result["download"].assert_not_called()
        self.assertIn("query_keys=snapshot_token", result["stderr"])
        self.assertNotIn("snapshot_token=abc", result["stderr"])
        self.assertEqual(
            json.loads(result["stdout"])["snapshot_render_url"],
            expected_url,
        )

    def test_remote_snapshot_resolves_download_url_and_reports_it(self):
        result = self.run_snapshot(
            {"snapshot_url": "/reports/report-1/snapshot.png?token=abc"},
            remote_only=True,
            raw_json=True,
        )

        expected_url = (
            "http://localhost:8080/reports/report-1/snapshot.png?token=abc"
        )
        self.assertEqual(result["status"], 0)
        result["render"].assert_not_called()
        result["download"].assert_called_once_with(
            expected_url,
            timeout_seconds=90,
        )
        self.assertEqual(
            json.loads(result["stdout"])["snapshot_url"],
            expected_url,
        )

    def test_snapshot_viewport_params_are_sent_to_mcp(self):
        result = self.run_snapshot(
            {
                "snapshot_render_url": (
                    "/reports/report-1/snapshot?"
                    "snapshot_token=abc&zoom=12&lat=52.52&lon=13.405"
                ),
            },
            raw_json=True,
            zoom=12,
            lat=52.52,
            lon=13.405,
        )

        self.assertEqual(result["status"], 0)
        result["mcp"].assert_called_once_with(
            "create_report_snapshot",
            {
                "report_id": "report-1",
                "zoom": 12.0,
                "lat": 52.52,
                "lon": 13.405,
            },
            timeout_seconds=90,
        )
        self.assertEqual(
            json.loads(result["stdout"])["snapshot_viewport"],
            {"zoom": 12.0, "lat": 52.52, "lon": 13.405},
        )

    def test_snapshot_rejects_lat_without_lon(self):
        result = self.run_snapshot(
            {"snapshot_url": "/reports/report-1/snapshot.png?token=abc"},
            remote_only=True,
            lat=52.52,
        )

        self.assertEqual(result["status"], 2)
        self.assertIn(
            "Invalid --lat/--lon: pass both latitude and longitude.",
            result["stderr"],
        )
        result["mcp"].assert_not_called()

    def test_snapshot_rejects_out_of_range_viewport_values(self):
        cases = (
            {"zoom": -1, "message": "Invalid --zoom: must be between 0 and 24."},
            {
                "zoom": float("nan"),
                "message": "Invalid --zoom: must be between 0 and 24.",
            },
            {
                "lat": -91,
                "lon": 13.405,
                "message": "Invalid --lat: must be between -90 and 90.",
            },
            {
                "lat": float("nan"),
                "lon": 13.405,
                "message": "Invalid --lat: must be between -90 and 90.",
            },
            {
                "lat": 52.52,
                "lon": 181,
                "message": "Invalid --lon: must be between -180 and 180.",
            },
            {
                "lat": 52.52,
                "lon": float("nan"),
                "message": "Invalid --lon: must be between -180 and 180.",
            },
        )
        for case in cases:
            with self.subTest(case=case):
                result = self.run_snapshot(
                    {"snapshot_url": "/reports/report-1/snapshot.png?token=abc"},
                    remote_only=True,
                    zoom=case.get("zoom"),
                    lat=case.get("lat"),
                    lon=case.get("lon"),
                )

                self.assertEqual(result["status"], 2)
                self.assertIn(case["message"], result["stderr"])
                result["mcp"].assert_not_called()

    def test_invalid_local_png_fails_without_remote_fallback_or_file(self):
        result = self.run_snapshot(
            {
                "snapshot_render_url": "/render?snapshot_token=secret",
                "snapshot_url": "/snapshot.png?signature=secret",
            },
            render_result=b"",
        )

        self.assertEqual(result["status"], 1)
        self.assertIn("invalid PNG data", result["stderr"])
        result["download"].assert_not_called()
        result["save"].assert_not_called()

    def test_target_closed_failure_offers_remote_only_without_token_leak(self):
        error = cli.LocalSnapshotRenderError(
            RuntimeError(
                "Target page, context or browser has been closed at "
                "https://example.test/render?snapshot_token=render-secret"
            ),
            stage="ready",
            retryable=True,
        )
        error.attempts = 2

        result = self.run_snapshot(
            {
                "snapshot_render_url": "/render?snapshot_token=render-secret",
                "snapshot_url": "/snapshot.png?signature=download-secret",
            },
            render_side_effect=error,
        )

        self.assertEqual(result["status"], 1)
        self.assertIn("add `--remote-only`", result["stderr"])
        self.assertNotIn("render-secret", result["stderr"])
        self.assertNotIn("download-secret", result["stderr"])
        result["download"].assert_not_called()


class LocalSnapshotRetryTest(unittest.TestCase):
    class FakePlaywrightTimeoutError(Exception):
        pass

    def playwright_modules(self):
        sync_api = types.ModuleType("playwright.sync_api")
        sync_api.TimeoutError = self.FakePlaywrightTimeoutError
        sync_api.sync_playwright = mock.Mock(name="sync_playwright")
        playwright = types.ModuleType("playwright")
        playwright.sync_api = sync_api
        return {
            "playwright": playwright,
            "playwright.sync_api": sync_api,
        }

    def test_target_close_retries_once_and_returns_second_png(self):
        first_error = cli.LocalSnapshotRenderError(
            RuntimeError("Target page, context or browser has been closed"),
            stage="ready",
            retryable=True,
            page_closed=True,
        )
        attempt = mock.Mock(
            side_effect=[
                first_error,
                (PNG, {"elapsed_seconds": 0.1, "cleanup_errors": []}),
            ]
        )

        stderr = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.dict(sys.modules, self.playwright_modules())
            )
            stack.enter_context(
                mock.patch.object(cli, "_render_local_snapshot_attempt", attempt)
            )
            stack.enter_context(redirect_stderr(stderr))
            result = cli.render_local_snapshot_png(
                "https://example.test/render?snapshot_token=secret",
                width=1600,
                height=900,
                timeout_seconds=90,
                expires_at=200.0,
                monotonic=mock.Mock(return_value=100.0),
            )

        self.assertEqual(result, PNG)
        self.assertEqual(attempt.call_count, 2)
        self.assertIn("retrying once with a fresh browser", stderr.getvalue())

    def test_attempt_classifies_target_termination_but_not_timeout(self):
        cases = (
            (
                "exact message",
                RuntimeError("Target page, context or browser has been closed"),
                {},
                True,
                False,
            ),
            (
                "page closed state",
                RuntimeError("readiness failed"),
                {"page_closed": True},
                True,
                False,
            ),
            (
                "browser disconnected state",
                RuntimeError("readiness failed"),
                {"browser_connected": False},
                True,
                False,
            ),
            (
                "page crash event",
                RuntimeError("readiness failed"),
                {"page_crashed": True},
                True,
                False,
            ),
            (
                "timeout precedence",
                self.FakePlaywrightTimeoutError(
                    "Target page, context or browser has been closed"
                ),
                {},
                False,
                True,
            ),
            (
                "lookalike wording",
                RuntimeError("browser target closed unexpectedly"),
                {},
                False,
                False,
            ),
        )
        for name, error, state, retryable, timed_out in cases:
            with self.subTest(name=name):
                manager = mock.Mock()
                playwright = manager.start.return_value
                browser = playwright.chromium.launch.return_value
                context = browser.new_context.return_value
                page = context.new_page.return_value
                callbacks = {}
                page.on.side_effect = lambda event, callback: callbacks.setdefault(
                    event, callback
                )
                browser.on.side_effect = lambda event, callback: callbacks.setdefault(
                    event, callback
                )
                page.is_closed.return_value = state.get("page_closed", False)
                browser.is_connected.return_value = state.get(
                    "browser_connected",
                    True,
                )

                def fail_navigation(*_args, **_kwargs):
                    if state.get("page_crashed"):
                        callbacks["crash"]()
                    raise error

                page.goto.side_effect = fail_navigation

                with self.assertRaises(cli.LocalSnapshotRenderError) as raised:
                    cli._render_local_snapshot_attempt(
                        "https://example.test/render?snapshot_token=secret",
                        1600,
                        900,
                        90,
                        sync_playwright_factory=mock.Mock(return_value=manager),
                        playwright_timeout_error=self.FakePlaywrightTimeoutError,
                        monotonic=mock.Mock(side_effect=(0.0, 0.1)),
                    )

                self.assertIs(raised.exception.retryable, retryable)
                self.assertIs(raised.exception.timed_out, timed_out)
                context.close.assert_called_once_with()
                browser.close.assert_called_once_with()
                playwright.stop.assert_called_once_with()

    def test_playwright_timeout_is_not_retried(self):
        timeout = self.FakePlaywrightTimeoutError("Timeout 90000ms exceeded")
        error = cli.LocalSnapshotRenderError(
            timeout,
            stage="ready",
            timed_out=True,
        )
        attempt = mock.Mock(side_effect=error)

        with mock.patch.dict(sys.modules, self.playwright_modules()), mock.patch.object(
            cli,
            "_render_local_snapshot_attempt",
            attempt,
        ):
            with self.assertRaises(cli.LocalSnapshotRenderError):
                cli.render_local_snapshot_png(
                    "https://example.test/render?snapshot_token=secret",
                    width=1600,
                    height=900,
                    timeout_seconds=90,
                    expires_at=200.0,
                    monotonic=mock.Mock(return_value=100.0),
                )

        self.assertEqual(attempt.call_count, 1)

    def test_target_close_without_verified_lifetime_is_not_retried(self):
        error = cli.LocalSnapshotRenderError(
            RuntimeError("Target page, context or browser has been closed"),
            stage="ready",
            retryable=True,
            page_closed=True,
        )
        attempt = mock.Mock(side_effect=error)

        with mock.patch.dict(sys.modules, self.playwright_modules()), mock.patch.object(
            cli,
            "_render_local_snapshot_attempt",
            attempt,
        ):
            with self.assertRaises(cli.LocalSnapshotRenderError) as raised:
                cli.render_local_snapshot_png(
                    "https://example.test/render?snapshot_token=secret",
                    width=1600,
                    height=900,
                    timeout_seconds=90,
                    expires_at=None,
                )

        self.assertEqual(attempt.call_count, 1)
        self.assertEqual(
            raised.exception.retry_skipped_reason,
            "unverified_lifetime",
        )


class SnapshotSafetyTest(unittest.TestCase):
    def test_sanitizes_query_values_and_url_userinfo(self):
        diagnostic = (
            "failed at https://alice:basic-secret@example.test/render"
            "?snapshot_token=url-secret&zoom=12 "
            'with {"token":"json-secret"}'
        )

        sanitized = cli.sanitize_snapshot_diagnostic(diagnostic)
        summary = cli.snapshot_url_debug_summary(
            "https://alice:basic-secret@example.test/render"
            "?snapshot_token=url-secret"
        )

        for output in (sanitized, summary):
            self.assertNotIn("alice", output)
            self.assertNotIn("basic-secret", output)
            self.assertNotIn("url-secret", output)
        self.assertNotIn("json-secret", sanitized)

    def test_expiry_deadline_rejects_unbounded_values(self):
        self.assertEqual(cli.snapshot_expiry_deadline("30", 100.0), 130.0)
        for value in (None, 0, -1, "invalid", "9" * 5000, 10**400):
            with self.subTest(value_type=type(value).__name__):
                self.assertIsNone(cli.snapshot_expiry_deadline(value, 100.0))


class SnapshotParserTest(unittest.TestCase):
    def test_snapshot_parses_viewport_arguments(self):
        parser = cli.build_parser()
        args = parser.parse_args(
            [
                "snapshot",
                "--report-id",
                "report-1",
                "--zoom",
                "12",
                "--lat",
                "52.52",
                "--lon",
                "13.405",
            ]
        )

        self.assertEqual(args.command, "snapshot")
        self.assertEqual(args.zoom, 12.0)
        self.assertEqual(args.lat, 52.52)
        self.assertEqual(args.lon, 13.405)


if __name__ == "__main__":
    unittest.main()
