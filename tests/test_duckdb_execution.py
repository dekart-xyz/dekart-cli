import io
import json
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from dekart import cli
from dekart import duckdb_execution


def prepared_result():
    return {
        "query_job": {
            "id": "root-job",
            "query_id": "query-1",
            "dataset_id": "result-dataset",
            "query_params_hash": "hash",
            "job_status": "JOB_STATUS_DONE",
        },
        "duckdb_execution": {
            "duckdb_version": "1.4.3",
            "sources": [
                {
                    "dataset_id": "source-dataset",
                    "file_source_id": "file-1",
                    "extension": "csv",
                }
            ],
            "statements": [{"sql": "SELECT 1"}],
        },
    }


class ParamsAndCapabilityTest(unittest.TestCase):
    def test_params_json_encodes_qp_scalars(self):
        self.assertEqual(
            cli.encode_query_params_values('{"limit":2,"enabled":true,"empty":null}'),
            "qp_limit=2&qp_enabled=true&qp_empty=",
        )

    def test_params_json_rejects_nested_values(self):
        with self.assertRaisesRegex(ValueError, "values must be scalars"):
            cli.encode_query_params_values('{"nested":{"value":1}}')

    def test_run_query_uses_discovered_opt_in_and_params(self):
        response = {"result": {"query_job": {"id": "job-1"}}}
        with mock.patch.object(cli, "supports_duckdb_execution", return_value=True), mock.patch.object(
            cli, "mcp_call", return_value=response
        ) as call:
            result = cli.run_prepared_query("query-1", "qp_limit=2", 30)

        self.assertEqual(result["job_id"], "job-1")
        call.assert_called_once_with(
            "run_query",
            {
                "query_id": "query-1",
                "query_params_values": "qp_limit=2",
                "accept_duckdb_execution": True,
            },
            timeout_seconds=30,
        )

    def test_old_server_connection_call_omits_opt_in(self):
        response = {"result": {"query_job": {"id": "job-1"}}}
        with mock.patch.object(cli, "supports_duckdb_execution", return_value=False), mock.patch.object(
            cli, "mcp_call", return_value=response
        ) as call:
            cli.run_prepared_query("query-1", "", 30)

        call.assert_called_once_with("run_query", {"query_id": "query-1"}, timeout_seconds=30)

    def test_old_server_duckdb_error_has_upgrade_guidance(self):
        error = urllib.error.HTTPError("http://dekart", 400, "Bad Request", {}, io.BytesIO(b""))
        with mock.patch.object(cli, "supports_duckdb_execution", return_value=False), mock.patch.object(
            cli, "mcp_call", side_effect=error
        ), mock.patch.object(cli, "parse_http_error_body", return_value=("DuckDB queries must use RunDuckDBQuery", None)):
            with self.assertRaisesRegex(RuntimeError, "newer Dekart server"):
                cli.run_prepared_query("query-1", "", 30)
        error.close()

    def test_python_38_does_not_opt_in_before_rejecting_duckdb(self):
        error = urllib.error.HTTPError("http://dekart", 400, "Bad Request", {}, io.BytesIO(b""))
        with mock.patch.object(cli, "supports_duckdb_execution", return_value=True), mock.patch.object(
            cli.sys, "version_info", (3, 8)
        ), mock.patch.object(cli, "mcp_call", side_effect=error) as call, mock.patch.object(
            cli, "parse_http_error_body", return_value=("DuckDB queries must use RunDuckDBQuery", None)
        ):
            with self.assertRaisesRegex(RuntimeError, "Python 3.9"):
                cli.run_prepared_query("query-1", "", 30)
        self.assertNotIn("accept_duckdb_execution", call.call_args.args[1])
        error.close()

    def test_failed_capability_discovery_is_not_mislabeled_as_old_server(self):
        error = urllib.error.HTTPError("http://dekart", 400, "Bad Request", {}, io.BytesIO(b""))
        with mock.patch.object(cli, "supports_duckdb_execution", return_value=None), mock.patch.object(
            cli, "mcp_call", side_effect=error
        ), mock.patch.object(
            cli, "parse_http_error_body", return_value=("DuckDB queries must use RunDuckDBQuery", None)
        ):
            with self.assertRaisesRegex(RuntimeError, "Could not discover"):
                cli.run_prepared_query("query-1", "", 30)
        error.close()

    def test_http_error_body_can_be_read_twice(self):
        body = b'{"message":"invalid query"}'
        error = urllib.error.HTTPError("http://dekart", 400, "Bad Request", {}, io.BytesIO(body))
        first = cli.parse_http_error_body(error)
        second = cli.parse_http_error_body(error)
        self.assertEqual(first, second)
        self.assertEqual(second[1]["message"], "invalid query")
        error.close()


class PreparedExecutionValidationTest(unittest.TestCase):
    def test_valid_bundle(self):
        query_job, execution = duckdb_execution.validate_prepared_execution(prepared_result())
        self.assertEqual(query_job["id"], "root-job")
        self.assertEqual(execution["sources"][0]["file_source_id"], "file-1")

    def test_empty_sources_are_valid(self):
        result = prepared_result()
        result["duckdb_execution"]["sources"] = []
        duckdb_execution.validate_prepared_execution(result)

    def test_invalid_bundles(self):
        cases = []
        missing_root = prepared_result()
        missing_root["query_job"].pop("id")
        cases.append(missing_root)
        root_error = prepared_result()
        root_error["query_job"]["job_error"] = "failed"
        cases.append(root_error)
        duplicate_revision = prepared_result()
        duplicate_revision["duckdb_execution"]["sources"][0]["query_job_id"] = "job-1"
        cases.append(duplicate_revision)
        unsupported_source = prepared_result()
        unsupported_source["duckdb_execution"]["sources"][0]["extension"] = "xlsx"
        cases.append(unsupported_source)
        empty_statement = prepared_result()
        empty_statement["duckdb_execution"]["statements"] = [{"sql": ""}]
        cases.append(empty_statement)

        for result in cases:
            with self.subTest(result=result):
                with self.assertRaises(ValueError):
                    duckdb_execution.validate_prepared_execution(result)

    def test_requested_query_must_match_root(self):
        result = prepared_result()
        with mock.patch.object(cli, "supports_duckdb_execution", return_value=True), mock.patch.object(
            cli, "mcp_call", return_value={"result": result}
        ):
            with self.assertRaisesRegex(ValueError, "does not match"):
                cli.run_prepared_query("another-query", "", 30)


class SourceResolutionTest(unittest.TestCase):
    def metadata(self):
        result = prepared_result()
        result["duckdb_execution"]["sources"] = [
            {
                "dataset_id": "warehouse-dataset",
                "query_job_id": "source-job",
                "extension": "parquet",
            }
        ]
        return {
            "query_id": "query-1",
            "job_id": "root-job",
            "query_job": result["query_job"],
            "duckdb_execution": result["duckdb_execution"],
        }

    def test_no_wait_returns_pending_without_download_or_execution(self):
        metadata = self.metadata()
        pending_job = {"job_status": "JOB_STATUS_RUNNING", "dataset_id": "warehouse-dataset"}
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            cli, "wait_for_query_job", return_value=pending_job
        ), mock.patch.object(cli, "download_binary") as download, mock.patch.object(
            duckdb_execution, "execute_program"
        ) as execute:
            payload = cli.materialize_duckdb_query(metadata, directory, False, 30, 1)

        self.assertEqual(payload["terminal_status"], "PENDING")
        download.assert_not_called()
        execute.assert_not_called()

    def test_source_job_requires_matching_dataset_and_extension(self):
        metadata = self.metadata()
        bad_jobs = [
            {
                "job_status": "JOB_STATUS_DONE",
                "dataset_id": "wrong",
                "job_result_id": "result-1",
                "result_extension": "parquet",
            },
            {
                "job_status": "JOB_STATUS_DONE",
                "dataset_id": "warehouse-dataset",
                "job_result_id": "result-1",
                "result_extension": "csv",
            },
            {
                "job_status": "JOB_STATUS_DONE",
                "dataset_id": "warehouse-dataset",
                "job_result_id": "",
                "result_extension": "parquet",
            },
        ]
        for query_job in bad_jobs:
            with self.subTest(query_job=query_job), mock.patch.object(
                cli, "wait_for_query_job", return_value=query_job
            ):
                with self.assertRaises(ValueError):
                    cli.prepared_source_jobs(metadata, True, 30, 1)

    def test_file_happy_path_downloads_then_executes(self):
        result = prepared_result()
        metadata = {
            "query_id": "query-1",
            "job_id": "root-job",
            "query_job": result["query_job"],
            "duckdb_execution": result["duckdb_execution"],
        }

        def fake_execute(_execution, _job, source_paths, result_path, _work_dir):
            self.assertTrue(Path(source_paths[0]).exists())
            Path(result_path).write_bytes(b"PAR1")

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            cli, "get_dekart_url", return_value="https://dekart"
        ), mock.patch.object(cli, "download_binary", return_value=b"a\n1\n") as download, mock.patch.object(
            duckdb_execution, "execute_program", side_effect=fake_execute
        ) as execute:
            payload = cli.materialize_duckdb_query(metadata, directory, True, 30, 1)
            self.assertTrue(Path(payload["path"]).exists())

        self.assertEqual(payload["bytes"], 4)
        self.assertIn("/source-dataset/file-1.csv", download.call_args.args[0])
        execute.assert_called_once()


class PublicationTest(unittest.TestCase):
    class Connection:
        def execute(self, _sql):
            return self

        def fetchall(self):
            return [
                ('geo"metry', "GEOMETRY"),
                ("large", "UHUGEINT"),
                ("price", "DECIMAL(10,2)"),
                ("small", "INTEGER"),
            ]

    def test_publication_matches_browser_casts(self):
        query = duckdb_execution.publication_query(self.Connection(), 'dekart_internal."job_root"')
        self.assertIn('ST_AsWKB("geo""metry") AS "geo""metry"', query)
        self.assertIn('CAST("large" AS DOUBLE) AS "large"', query)
        self.assertIn('CAST("price" AS DOUBLE) AS "price"', query)
        self.assertIn('"small"', query)

    def test_version_mismatch_warns_only_to_stderr(self):
        stderr = io.StringIO()
        stdout = io.StringIO()
        with redirect_stderr(stderr), mock.patch("sys.stdout", stdout):
            duckdb_execution.warn_on_version_mismatch("1.4.3", "1.5.2")
            print(json.dumps({"ok": True}))
        self.assertIn("Continuing best effort", stderr.getvalue())
        self.assertEqual(json.loads(stdout.getvalue()), {"ok": True})


if __name__ == "__main__":
    unittest.main()
