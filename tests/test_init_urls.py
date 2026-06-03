import io
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from unittest import mock

from dekart import cli


class HandleInitUrlTest(unittest.TestCase):
    def run_init(self, auth_url, *, token_result=None, sleep_side_effect=None):
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
            stack.enter_context(mock.patch("builtins.input", return_value="y"))
            save_dekart_url = stack.enter_context(mock.patch.object(cli, "save_dekart_url"))
            save_token = stack.enter_context(mock.patch.object(cli, "save_token"))
            stack.enter_context(mock.patch.object(cli, "get_token_path"))
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            status = cli.handle_init(no_browser=False, local_snapshot_mode="skip")

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
        save_dekart_url.assert_not_called()
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
        save_dekart_url.assert_not_called()
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
