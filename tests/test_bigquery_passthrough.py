import io
import json
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from dekart import cli


class FakeResponse:
    def __init__(self, payload=b"{}"):
        self.payload = payload
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback):
        return False

    def read(self):
        return self.payload


def http_error(code, payload):
    return urllib.error.HTTPError(
        "https://dekart.example/api/v1/mcp/call",
        code,
        "failure",
        {},
        io.BytesIO(json.dumps(payload).encode("utf-8")),
    )


class BigQueryPassthroughParserTest(unittest.TestCase):
    def test_init_mode_defaults_to_ask_and_accepts_enable_disable(self):
        parser = cli.build_parser()
        self.assertEqual(parser.parse_args(["init"]).bigquery_passthrough, "ask")
        self.assertEqual(
            parser.parse_args(["init", "--bigquery-passthrough", "enable"]).bigquery_passthrough,
            "enable",
        )
        self.assertEqual(
            parser.parse_args(["init", "--bigquery-passthrough", "disable"]).bigquery_passthrough,
            "disable",
        )


class BigQueryPassthroughConfigTest(unittest.TestCase):
    def setUp(self):
        cli._google_access_token_cache.clear()

    def test_enable_saves_only_non_secret_binding_and_preserves_other_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text('{"other": "kept"}\n', encoding="utf-8")
            process_results = [mock.Mock(returncode=0, stdout="(unset)\n", stderr="")]
            with mock.patch.object(cli, "get_config_path", return_value=config_path), mock.patch.object(
                cli.shutil, "which", return_value="/usr/bin/gcloud"
            ), mock.patch.object(cli.subprocess, "run", side_effect=process_results), mock.patch.object(
                cli, "mint_google_access_token", return_value="secret-token"
            ):
                self.assertTrue(
                    cli.configure_google_bigquery_passthrough(
                        "enable", "https://dekart.example", "USER@example.com", False
                    )
                )

            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["other"], "kept")
            self.assertEqual(
                saved["google_bigquery_passthrough"],
                {"dekart_url": "https://dekart.example", "gcloud_account": "user@example.com"},
            )
            self.assertNotIn("secret", config_path.read_text(encoding="utf-8"))

    def test_disable_removes_only_passthrough_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                json.dumps({"other": "kept", "google_bigquery_passthrough": {"gcloud_account": "a"}}),
                encoding="utf-8",
            )
            with mock.patch.object(cli, "get_config_path", return_value=config_path):
                self.assertTrue(
                    cli.configure_google_bigquery_passthrough(
                        "disable", "https://dekart.example", "user@example.com", False
                    )
                )
            self.assertEqual(json.loads(config_path.read_text(encoding="utf-8")), {"other": "kept"})

    def test_missing_matching_login_fails_with_actionable_message(self):
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            cli, "get_config_path", return_value=Path(directory) / "config.json"
        ), mock.patch.object(cli.shutil, "which", return_value="/usr/bin/gcloud"), mock.patch.object(
            cli.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="(unset)\n", stderr="")
        ), mock.patch.object(
            cli,
            "mint_google_access_token",
            side_effect=RuntimeError(
                "gcloud could not provide an access token. "
                "Run `gcloud auth login --account user@example.com`, then retry."
            ),
        ), redirect_stderr(stderr):
            self.assertFalse(
                cli.configure_google_bigquery_passthrough(
                    "enable", "https://dekart.example", "user@example.com", False
                )
            )
        self.assertIn("could not provide an access token", stderr.getvalue())
        self.assertIn("gcloud auth login --account user@example.com", stderr.getvalue())

    def test_signed_in_dekart_account_is_passed_directly_to_gcloud(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            cli, "get_config_path", return_value=Path(directory) / "config.json"
        ), mock.patch.object(cli.shutil, "which", return_value="/usr/bin/gcloud"), mock.patch.object(
            cli.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="(unset)\n", stderr="")
        ), mock.patch.object(cli, "mint_google_access_token", return_value="secret-token") as mint:
            self.assertTrue(
                cli.configure_google_bigquery_passthrough(
                    "enable", "https://dekart.example", "user@example.com", False
                )
            )
            saved = json.loads((Path(directory) / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["google_bigquery_passthrough"]["gcloud_account"], "user@example.com")
        mint.assert_called_once_with("user@example.com", force_refresh=True)

    def test_anonymous_self_hosted_init_prompts_for_authenticated_user_account(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            cli, "get_config_path", return_value=Path(directory) / "config.json"
        ), mock.patch.object(cli.shutil, "which", return_value="/usr/bin/gcloud"), mock.patch.object(
            cli.subprocess,
            "run",
            side_effect=[
                mock.Mock(returncode=0, stdout="runner@example.iam.gserviceaccount.com\n", stderr=""),
                mock.Mock(
                    returncode=0,
                    stdout="first@example.com\nrunner@example.iam.gserviceaccount.com\nsecond@example.com\n",
                    stderr="",
                ),
                mock.Mock(returncode=0, stdout="(unset)\n", stderr=""),
            ],
        ), mock.patch.object(
            cli, "select_menu_option", return_value=1
        ), mock.patch.object(cli, "mint_google_access_token", return_value="secret-token"):
            self.assertTrue(
                cli.configure_google_bigquery_passthrough(
                    "enable", "http://host.docker.internal:8080", "UNKNOWN_EMAIL", True
                )
            )
            saved = json.loads((Path(directory) / "config.json").read_text(encoding="utf-8"))

        self.assertEqual(saved["google_bigquery_passthrough"]["gcloud_account"], "second@example.com")

    def test_impersonation_lookup_failure_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            cli, "get_config_path", return_value=Path(directory) / "config.json"
        ), mock.patch.object(cli.shutil, "which", return_value="/usr/bin/gcloud"), mock.patch.object(
            cli.subprocess,
            "run",
            return_value=mock.Mock(returncode=1, stdout="", stderr="failure"),
        ), mock.patch.object(cli, "mint_google_access_token") as mint, redirect_stderr(io.StringIO()):
            self.assertFalse(
                cli.configure_google_bigquery_passthrough(
                    "enable", "https://dekart.example", "user@example.com", False
                )
            )
        mint.assert_not_called()

    def test_binding_requires_same_endpoint_and_device_email(self):
        config = {
            "google_bigquery_passthrough": {
                "dekart_url": "https://dekart.example",
                "gcloud_account": "user@example.com",
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "token.json"
            token_path.write_text(
                json.dumps({"dekart_url": "https://dekart.example", "email": "USER@example.com"}),
                encoding="utf-8",
            )
            with mock.patch.object(cli, "load_config", return_value=config), mock.patch.object(
                cli, "get_token_path", return_value=token_path
            ), mock.patch.object(cli, "get_dekart_url", return_value="https://dekart.example"):
                self.assertEqual(cli.get_google_bigquery_passthrough_binding()["gcloud_account"], "user@example.com")
            token_path.write_text(
                json.dumps({"dekart_url": "https://dekart.example", "email": "other@example.com"}),
                encoding="utf-8",
            )
            with mock.patch.object(cli, "load_config", return_value=config), mock.patch.object(
                cli, "get_token_path", return_value=token_path
            ), mock.patch.object(cli, "get_dekart_url", return_value="https://dekart.example"):
                self.assertIsNone(cli.get_google_bigquery_passthrough_binding())

    def test_binding_accepts_selected_gcloud_user_for_anonymous_self_hosted_identity(self):
        config = {
            "google_bigquery_passthrough": {
                "dekart_url": "http://host.docker.internal:8080",
                "gcloud_account": "user@example.com",
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "token.json"
            token_path.write_text(
                json.dumps(
                    {
                        "dekart_url": "http://host.docker.internal:8080",
                        "email": "UNKNOWN_EMAIL",
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(cli, "load_config", return_value=config), mock.patch.object(
                cli, "get_token_path", return_value=token_path
            ), mock.patch.object(
                cli, "get_dekart_url", return_value="http://host.docker.internal:8080"
            ):
                self.assertEqual(
                    cli.get_google_bigquery_passthrough_binding()["gcloud_account"],
                    "user@example.com",
                )


class BigQueryPassthroughRequestTest(unittest.TestCase):
    def setUp(self):
        cli._google_access_token_cache.clear()
        cli._google_token_warning_shown = False

    def test_google_header_url_allowlist_is_exact(self):
        base = "https://dekart.example/root"
        allowed = [
            "https://dekart.example/root/api/v1/mcp/call",
            "https://dekart.example/root/api/v1/dataset-source/dataset/result.parquet",
            "http://localhost:8080/api/v1/mcp/call",
            "http://host.docker.internal:8080/api/v1/mcp/call",
        ]
        self.assertTrue(cli.is_google_header_url_allowed(allowed[0], base))
        self.assertTrue(cli.is_google_header_url_allowed(allowed[1], base))
        self.assertTrue(cli.is_google_header_url_allowed(allowed[2], "http://localhost:8080"))
        self.assertTrue(
            cli.is_google_header_url_allowed(
                allowed[3], "http://host.docker.internal:8080"
            )
        )
        denied = [
            "http://dekart.example/root/api/v1/mcp/call",
            "https://evil.dekart.example/root/api/v1/mcp/call",
            "https://dekart.example:444/root/api/v1/mcp/call",
            "https://dekart.example/root/api/v1/mcp/tools",
            "https://dekart.example/root/api/v1/mcp/call/extra",
            "https://dekart.example/root/api/v1/dataset-source/a/b.parquet?next=1",
            "https://user@dekart.example/root/api/v1/mcp/call",
        ]
        for url in denied:
            with self.subTest(url=url):
                self.assertFalse(cli.is_google_header_url_allowed(url, base))

    def test_mint_is_cached_in_process(self):
        result = mock.Mock(returncode=0, stdout="token-value\n", stderr="ignored secret")
        with mock.patch.object(cli.subprocess, "run", return_value=result) as run:
            self.assertEqual(cli.mint_google_access_token("user@example.com"), "token-value")
            self.assertEqual(cli.mint_google_access_token("USER@example.com"), "token-value")
        run.assert_called_once()

    def test_failed_force_refresh_removes_rejected_cached_token(self):
        cli._google_access_token_cache["user@example.com"] = "rejected-token"
        result = mock.Mock(returncode=1, stdout="", stderr="failure")
        with mock.patch.object(cli.subprocess, "run", return_value=result):
            with self.assertRaises(RuntimeError):
                cli.mint_google_access_token("user@example.com", force_refresh=True)
        self.assertNotIn("user@example.com", cli._google_access_token_cache)

    def test_mcp_uses_no_redirect_opener_and_retries_invalid_token_once(self):
        first = http_error(401, {"error": "google_access_token_invalid"})
        second = FakeResponse(json.dumps({"result": {"ok": True}}).encode("utf-8"))
        with mock.patch.object(cli, "get_dekart_url", return_value="https://dekart.example"), mock.patch.object(
            cli, "get_auth_headers", return_value={"Authorization": "Bearer device"}
        ), mock.patch.object(
            cli,
            "get_optional_google_passthrough_headers",
            side_effect=[
                {cli.GOOGLE_ACCESS_TOKEN_HEADER: "Bearer google-1"},
                {cli.GOOGLE_ACCESS_TOKEN_HEADER: "Bearer google-2"},
            ],
        ) as google_headers, mock.patch.object(
            cli, "urlopen_without_redirects", side_effect=[first, second]
        ) as opener, mock.patch.object(cli.urllib.request, "urlopen") as ordinary:
            payload = cli.mcp_call("run_query", {"query_id": "query-1"})

        self.assertEqual(payload, {"result": {"ok": True}})
        self.assertEqual(opener.call_count, 2)
        ordinary.assert_not_called()
        self.assertEqual(google_headers.call_args_list[0].kwargs["force_refresh"], False)
        self.assertEqual(google_headers.call_args_list[1].kwargs["force_refresh"], True)
        second_request = opener.call_args_list[1].args[0]
        self.assertEqual(second_request.get_header("Authorization"), "Bearer device")
        self.assertEqual(
            {key.casefold(): value for key, value in second_request.header_items()}[
                cli.GOOGLE_ACCESS_TOKEN_HEADER.casefold()
            ],
            "Bearer google-2",
        )

    def test_mint_failure_warns_once_and_omits_header(self):
        stderr = io.StringIO()
        with mock.patch.object(cli, "get_google_passthrough_headers", side_effect=RuntimeError("login required")), redirect_stderr(stderr):
            self.assertEqual(cli.get_optional_google_passthrough_headers("https://dekart.example"), {})
            self.assertEqual(cli.get_optional_google_passthrough_headers("https://dekart.example"), {})
        self.assertEqual(stderr.getvalue().count("login required"), 1)

    def test_dataset_download_uses_both_credentials_and_refreshes_once(self):
        first = http_error(401, {"error": "google_access_token_invalid"})
        second = FakeResponse(b"city,longitude,latitude\nBerlin,13.405,52.52\n")
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            cli, "get_dekart_url", return_value="https://dekart.example"
        ), mock.patch.object(
            cli, "get_auth_headers", return_value={"Authorization": "Bearer device"}
        ), mock.patch.object(
            cli,
            "get_optional_google_passthrough_headers",
            side_effect=[
                {cli.GOOGLE_ACCESS_TOKEN_HEADER: "Bearer google-1"},
                {cli.GOOGLE_ACCESS_TOKEN_HEADER: "Bearer google-2"},
            ],
        ) as google_headers, mock.patch.object(
            cli, "urlopen_without_redirects", side_effect=[first, second]
        ) as opener, mock.patch.object(cli.urllib.request, "urlopen") as ordinary:
            saved, size = cli.download_dataset_source(
                "dataset-1", "result-1", "csv", Path(directory) / "result.csv"
            )
            saved_body = saved.read_bytes()

        self.assertEqual(saved_body, second.payload)
        self.assertEqual(size, len(second.payload))
        self.assertEqual(opener.call_count, 2)
        ordinary.assert_not_called()
        self.assertEqual(google_headers.call_args_list[1].kwargs["force_refresh"], True)
        headers = {key.casefold(): value for key, value in opener.call_args_list[1].args[0].header_items()}
        self.assertEqual(headers["authorization"], "Bearer device")
        self.assertEqual(headers[cli.GOOGLE_ACCESS_TOKEN_HEADER.casefold()], "Bearer google-2")

    def test_off_origin_download_never_mints_or_forwards_google_token(self):
        response = FakeResponse(b"payload")
        binding = {"dekart_url": "https://dekart.example", "gcloud_account": "user@example.com"}
        with mock.patch.object(cli, "get_google_bigquery_passthrough_binding", return_value=binding), mock.patch.object(
            cli, "mint_google_access_token"
        ) as mint, mock.patch.object(cli, "get_auth_headers", return_value={}), mock.patch.object(
            cli.urllib.request, "urlopen", return_value=response
        ) as ordinary, mock.patch.object(cli, "urlopen_without_redirects") as sensitive:
            self.assertEqual(
                cli.download_binary("https://downloads.example/result.csv", google_passthrough=True),
                b"payload",
            )
        mint.assert_not_called()
        sensitive.assert_not_called()
        self.assertIsNone(ordinary.call_args.args[0].get_header(cli.GOOGLE_ACCESS_TOKEN_HEADER))


if __name__ == "__main__":
    unittest.main()
