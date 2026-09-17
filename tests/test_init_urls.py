import io
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from unittest import mock

from dekart import cli


class HandleInitUrlTest(unittest.TestCase):
    def run_init(self, auth_url, *, token_result=None, sleep_side_effect=None, no_browser=False, local_snapshot_mode="skip"):
        stdout = io.StringIO()
        stderr = io.StringIO()
        selected_url = "https://new.example.com/dekart"
        start_payload = {
            "device_id": "device-1",
            "auth_url": auth_url,
            "expires_in": 60,
            "interval": 1,
        }
        token_payload = token_result if token_result is not None else {
            "status": "authorized",
            "token": "token-1",
            "email": "user@example.com",
        }

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(cli, "is_interactive_terminal", return_value=True))
            stack.enter_context(mock.patch.object(cli, "print_init_banner"))
            stack.enter_context(mock.patch.object(cli, "prompt_init_dekart_url", return_value=selected_url))
            stack.enter_context(mock.patch.object(cli, "get_dekart_url", return_value="https://old.example.com"))
            post_json = stack.enter_context(mock.patch.object(cli, "post_json", side_effect=[start_payload, token_payload]))
            browser_open = stack.enter_context(mock.patch.object(cli.webbrowser, "open", return_value=True))
            sleep = stack.enter_context(mock.patch.object(cli.time, "sleep", side_effect=sleep_side_effect))
            stack.enter_context(mock.patch("builtins.input", side_effect=AssertionError("unexpected browser prompt")))
            save_dekart_url = stack.enter_context(mock.patch.object(cli, "save_dekart_url"))
            save_token = stack.enter_context(mock.patch.object(cli, "save_token"))
            stack.enter_context(mock.patch.object(cli, "configure_google_bigquery_passthrough", return_value=True))
            stack.enter_context(mock.patch.object(cli, "install_local_snapshot_capability", return_value={"ok": True}))
            stack.enter_context(mock.patch.object(cli, "get_token_path"))
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            status = cli.handle_init(no_browser=no_browser, local_snapshot_mode=local_snapshot_mode)

        return (
            status,
            stdout.getvalue(),
            stderr.getvalue(),
            selected_url,
            post_json,
            browser_open,
            sleep,
            save_dekart_url,
            save_token,
        )

    def test_relative_auth_url_uses_selected_unsaved_endpoint(self):
        status, stdout, _stderr, selected_url, post_json, browser_open, _sleep, _save_url, _save_token = self.run_init(
            "/device/authorize?device_id=device-1"
        )

        expected_url = "https://new.example.com/device/authorize?device_id=device-1"
        self.assertEqual(status, 0)
        self.assertIn(f"  {expected_url}", stdout)
        browser_open.assert_called_once_with(expected_url, new=2, autoraise=True)
        self.assertEqual(post_json.call_args_list[0].args[0], f"{selected_url}/api/v1/device")

    def test_setup_finishes_before_device_registration_and_browser_open(self):
        events = []
        start_payload = {
            "device_id": "device-1",
            "auth_url": "/device/authorize?device_id=device-1",
            "expires_in": 60,
            "interval": 1,
        }
        token_payload = {
            "status": "authorized",
            "token": "token-1",
            "email": "user@example.com",
        }

        def post_json(_url, _payload, **_kwargs):
            events.append("register" if not events or "register" not in events else "poll")
            return start_payload if events[-1] == "register" else token_payload

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(cli, "is_interactive_terminal", return_value=True))
            stack.enter_context(mock.patch.object(cli, "print_init_banner"))
            stack.enter_context(mock.patch.object(cli, "prompt_init_dekart_url", return_value="https://dekart.example"))
            stack.enter_context(mock.patch.object(cli, "save_dekart_url", side_effect=lambda _url: events.append("save_endpoint")))
            stack.enter_context(mock.patch.object(cli, "configure_google_bigquery_passthrough", side_effect=lambda *_args: events.append("bigquery") or True))
            stack.enter_context(mock.patch.object(cli, "install_local_snapshot_capability", side_effect=lambda **_kwargs: events.append("snapshot") or {"ok": True}))
            stack.enter_context(mock.patch.object(cli, "post_json", side_effect=post_json))
            stack.enter_context(mock.patch.object(cli.webbrowser, "open", side_effect=lambda *_args, **_kwargs: events.append("browser") or True))
            stack.enter_context(mock.patch.object(cli, "save_token"))
            stack.enter_context(mock.patch.object(cli, "get_token_path"))
            stack.enter_context(mock.patch.object(cli, "get_google_bigquery_passthrough_binding", return_value=None))
            stack.enter_context(mock.patch.object(cli, "get_local_snapshot_settings", return_value={"enabled": True}))
            stack.enter_context(mock.patch.object(cli, "load_config", return_value={}))
            stack.enter_context(mock.patch("builtins.input", return_value="n"))
            stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(redirect_stderr(io.StringIO()))
            status = cli.handle_init(no_browser=False, local_snapshot_mode="ask")

        self.assertEqual(status, 0)
        self.assertEqual(events, ["save_endpoint", "bigquery", "snapshot", "register", "browser", "poll"])

    def test_optional_setup_failures_do_not_block_successful_authorization(self):
        stderr = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(cli, "is_interactive_terminal", return_value=True))
            stack.enter_context(mock.patch.object(cli, "print_init_banner"))
            stack.enter_context(mock.patch.object(cli, "prompt_init_dekart_url", return_value="https://dekart.example"))
            stack.enter_context(mock.patch.object(cli, "save_dekart_url"))
            stack.enter_context(mock.patch.object(cli, "configure_google_bigquery_passthrough", return_value=False))
            stack.enter_context(mock.patch.object(cli, "install_local_snapshot_capability", return_value={"ok": False, "message": "install failed"}))
            stack.enter_context(mock.patch.object(cli, "post_json", side_effect=[
                {"device_id": "device-1", "auth_url": "/authorize", "expires_in": 60, "interval": 1},
                {"status": "authorized", "token": "token-1", "email": "user@example.com"},
            ]))
            stack.enter_context(mock.patch.object(cli.webbrowser, "open", return_value=True))
            stack.enter_context(mock.patch.object(cli, "save_token"))
            stack.enter_context(mock.patch.object(cli, "get_token_path"))
            stack.enter_context(mock.patch.object(cli, "get_google_bigquery_passthrough_binding", return_value=None))
            stack.enter_context(mock.patch.object(cli, "get_local_snapshot_settings", return_value={"enabled": False}))
            stack.enter_context(mock.patch.object(cli, "load_config", return_value={}))
            stack.enter_context(mock.patch("builtins.input", return_value="y"))
            stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(redirect_stderr(stderr))
            status = cli.handle_init(no_browser=False, local_snapshot_mode="ask")

        self.assertEqual(status, 0)
        self.assertIn("install failed", stderr.getvalue())

    def test_no_browser_and_snapshot_skip_are_preserved(self):
        result = self.run_init(
            "/device/authorize?device_id=device-1",
            no_browser=True,
            local_snapshot_mode="skip",
        )
        status, stdout, _stderr, _selected_url, _post_json, browser_open, _sleep, _save_url, _save_token = result

        self.assertEqual(status, 0)
        browser_open.assert_not_called()
        self.assertIn("Browser auto-open is disabled (--no-browser).", stdout)
        self.assertIn("Skipped local snapshot install (--local-snapshot skip).", stdout)

    def test_noninteractive_init_opens_browser_by_default(self):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(cli, "is_interactive_terminal", return_value=False))
            stack.enter_context(mock.patch.object(cli, "get_dekart_url", return_value="https://dekart.example"))
            stack.enter_context(mock.patch.object(cli, "save_dekart_url"))
            stack.enter_context(mock.patch.object(cli, "configure_google_bigquery_passthrough", return_value=True))
            stack.enter_context(mock.patch.object(cli, "install_local_snapshot_capability", return_value={"ok": True}))
            stack.enter_context(mock.patch.object(cli, "post_json", side_effect=[
                {"device_id": "device-1", "auth_url": "/authorize", "expires_in": 60, "interval": 1},
                {"status": "authorized", "token": "token-1", "email": "user@example.com"},
            ]))
            browser_open = stack.enter_context(mock.patch.object(cli.webbrowser, "open", return_value=True))
            stack.enter_context(mock.patch.object(cli, "save_token"))
            stack.enter_context(mock.patch.object(cli, "get_token_path"))
            stack.enter_context(mock.patch.object(cli, "get_google_bigquery_passthrough_binding", return_value=None))
            stack.enter_context(mock.patch.object(cli, "get_local_snapshot_settings", return_value={"enabled": True}))
            stack.enter_context(mock.patch.object(cli, "load_config", return_value={}))
            stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(redirect_stderr(io.StringIO()))
            status = cli.handle_init(no_browser=False, local_snapshot_mode="ask")

        self.assertEqual(status, 0)
        browser_open.assert_called_once_with("https://dekart.example/authorize", new=2, autoraise=True)

    def test_absolute_auth_url_is_preserved(self):
        auth_url = "https://auth.example.net/device/authorize?device_id=device-1"

        status, stdout, _stderr, _selected_url, _post_json, browser_open, _sleep, _save_url, _save_token = self.run_init(
            auth_url
        )

        self.assertEqual(status, 0)
        self.assertIn(f"  {auth_url}", stdout)
        browser_open.assert_called_once_with(auth_url, new=2, autoraise=True)

    def test_interrupt_during_token_polling_is_friendly(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        start_payload = {
            "device_id": "device-1",
            "auth_url": "/device/authorize?device_id=device-1",
            "expires_in": 60,
            "interval": 1,
        }

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(cli, "is_interactive_terminal", return_value=False))
            stack.enter_context(mock.patch.object(cli, "get_dekart_url", return_value="https://example.com"))
            stack.enter_context(mock.patch.object(cli, "post_json", side_effect=[start_payload, KeyboardInterrupt()]))
            save_dekart_url = stack.enter_context(mock.patch.object(cli, "save_dekart_url"))
            save_token = stack.enter_context(mock.patch.object(cli, "save_token"))
            stack.enter_context(mock.patch.object(cli, "get_token_path"))
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            status = cli.handle_init(no_browser=True, local_snapshot_mode="skip")

        self.assertEqual(status, 130)
        self.assertIn("Authorization cancelled.", stderr.getvalue())
        save_dekart_url.assert_called_once_with("https://example.com")
        save_token.assert_not_called()

    def test_interrupt_during_pending_sleep_is_friendly(self):
        result = self.run_init(
            "/device/authorize?device_id=device-1",
            token_result={"status": "pending"},
            sleep_side_effect=KeyboardInterrupt(),
        )
        status, _stdout, stderr, _selected_url, _post_json, _browser_open, sleep, save_dekart_url, save_token = result

        self.assertEqual(status, 130)
        self.assertIn("Authorization cancelled.", stderr)
        sleep.assert_called_once_with(1)
        save_dekart_url.assert_called_once_with("https://new.example.com/dekart")
        save_token.assert_not_called()

    def test_interrupt_during_token_save_is_not_mislabeled_as_authorization_cancellation(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        start_payload = {
            "device_id": "device-1",
            "auth_url": "/device/authorize?device_id=device-1",
            "expires_in": 60,
            "interval": 1,
        }
        token_payload = {
            "status": "authorized",
            "token": "token-1",
            "email": "user@example.com",
        }

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(cli, "is_interactive_terminal", return_value=False))
            stack.enter_context(mock.patch.object(cli, "get_dekart_url", return_value="https://example.com"))
            stack.enter_context(mock.patch.object(cli, "post_json", side_effect=[start_payload, token_payload]))
            stack.enter_context(mock.patch.object(cli, "save_dekart_url"))
            stack.enter_context(mock.patch.object(cli, "save_token", side_effect=KeyboardInterrupt()))
            stack.enter_context(mock.patch.object(cli, "get_token_path"))
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            with self.assertRaises(KeyboardInterrupt):
                cli.handle_init(no_browser=True, local_snapshot_mode="skip")

        self.assertNotIn("Authorization cancelled.", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
