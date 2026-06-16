import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from dekart import cli


class HandleQueryTest(unittest.TestCase):
    def run_query(self, *, mcp_responses=None, query_job=None, download=None, sql="SELECT 1", **kwargs):
        stdout = io.StringIO()
        stderr = io.StringIO()
        calls = []
        responses = list(
            mcp_responses
            or [
                {"result": {"report": {"id": "report-1"}}},
                {"result": {"id": "dataset-1"}},
                {"result": {"query_id": "query-1"}},
                {"result": {}},
                {"result": {"query_job": {"id": "job-1"}}},
            ]
        )

        def fake_mcp_call(name, args, timeout_seconds=30, return_metadata=False):
            calls.append((name, args, timeout_seconds, return_metadata))
            return cli.normalize_mcp_call_response(name, responses.pop(0), "https://dekart")

        with tempfile.TemporaryDirectory() as directory:
            sql_file = Path(directory) / "query.sql"
            sql_file.write_text(sql, encoding="utf-8")
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(cli, "mcp_call", side_effect=fake_mcp_call))
                wait = stack.enter_context(
                    mock.patch.object(
                        cli,
                        "wait_for_query_job",
                        return_value=query_job
                        or {
                            "job_status": "JOB_STATUS_DONE",
                            "query_id": "query-1",
                            "dataset_id": "dataset-1",
                            "job_result_id": "result-1",
                            "result_extension": "parquet",
                        },
                    )
                )
                downloader = stack.enter_context(
                    mock.patch.object(
                        cli,
                        "download_query_job_result",
                        return_value=download
                        or {
                            "job_result_id": "result-1",
                            "dataset_ref": "dataset-1",
                            "result_extension": "parquet",
                            "path": str(Path(directory) / "result.parquet"),
                            "bytes": 10,
                        },
                    )
                )
                stack.enter_context(redirect_stdout(stdout))
                stack.enter_context(redirect_stderr(stderr))
                status = cli.handle_query(
                    connection_id=kwargs.get("connection_id", "connection-1"),
                    sql_file=kwargs.get("sql_file", str(sql_file)),
                    sql=kwargs.get("inline_sql"),
                    out=kwargs.get("out", str(Path(directory) / "result.parquet")),
                    wait=kwargs.get("wait", True),
                    timeout=kwargs.get("timeout", 300),
                    interval=kwargs.get("interval", 5),
                    print_rows=kwargs.get("print_rows", False),
                    raw_json=kwargs.get("raw_json", True),
                )
        return status, stdout.getvalue(), stderr.getvalue(), calls, wait, downloader

    def test_query_json_runs_control_plane_and_downloads_result(self):
        status, stdout, stderr, calls, wait, downloader = self.run_query(raw_json=True)

        self.assertEqual(status, 0, stderr)
        self.assertEqual(
            [name for name, _args, _timeout, _return_metadata in calls],
            ["create_report", "create_dataset", "create_query", "update_query", "run_query"],
        )
        self.assertEqual(calls[1][1], {"report_id": "report-1"})
        self.assertEqual(calls[2][1], {"dataset_id": "dataset-1", "connection_id": "connection-1"})
        self.assertEqual(calls[3][1], {"query_id": "query-1", "query_text": "SELECT 1"})
        self.assertEqual(calls[4][1], {"query_id": "query-1"})
        wait.assert_called_once_with("job-1", True, 300, 5)
        downloader.assert_called_once()

        payload = json.loads(stdout)
        self.assertEqual(payload["report_id"], "report-1")
        self.assertEqual(payload["dataset_id"], "dataset-1")
        self.assertEqual(payload["query_id"], "query-1")
        self.assertEqual(payload["job_id"], "job-1")
        self.assertEqual(payload["terminal_status"], "JOB_STATUS_DONE")
        self.assertEqual(payload["report_url"], "https://dekart/reports/report-1")
        self.assertEqual(payload["bytes"], 10)

    def test_query_fails_fast_on_empty_result(self):
        status, stdout, stderr, _calls, _wait, _downloader = self.run_query(
            raw_json=True,
            download={
                "job_result_id": "result-1",
                "dataset_ref": "dataset-1",
                "result_extension": "parquet",
                "path": "/tmp/empty.parquet",
                "bytes": 0,
            },
        )

        self.assertEqual(status, 1)
        self.assertEqual(stdout, "")
        self.assertIn("empty result (metadata/SHOW statement?)", stderr)

    def test_query_prints_csv_rows_without_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "result.csv"
            result_path.write_text("a,b\n1,2\n", encoding="utf-8")
            status, stdout, stderr, _calls, _wait, _downloader = self.run_query(
                raw_json=False,
                print_rows=True,
                download={
                    "job_result_id": "result-1",
                    "dataset_ref": "dataset-1",
                    "result_extension": "csv",
                    "path": str(result_path),
                    "bytes": result_path.stat().st_size,
                },
            )

        self.assertEqual(status, 0, stderr)
        self.assertEqual(stdout, "a,b\n1,2\n")

    def test_query_rejects_no_wait_when_job_is_not_done(self):
        status, stdout, stderr, _calls, _wait, downloader = self.run_query(
            raw_json=True,
            wait=False,
            query_job={
                "job_status": "JOB_STATUS_RUNNING",
                "query_id": "query-1",
                "dataset_id": "dataset-1",
                "job_result_id": "result-1",
                "result_extension": "parquet",
            },
        )

        self.assertEqual(status, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Job is not done: JOB_STATUS_RUNNING", stderr)
        downloader.assert_not_called()

    def test_query_print_parquet_requires_duckdb(self):
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "result.parquet"
            result_path.write_bytes(b"PAR1")
            with mock.patch.dict(sys.modules, {"duckdb": None}):
                status, stdout, stderr, _calls, _wait, _downloader = self.run_query(
                    raw_json=False,
                    print_rows=True,
                    download={
                        "job_result_id": "result-1",
                        "dataset_ref": "dataset-1",
                        "result_extension": "parquet",
                        "path": str(result_path),
                        "bytes": result_path.stat().st_size,
                    },
                )

        self.assertEqual(status, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Cannot print parquet rows because the duckdb Python package is not installed", stderr)

    def test_query_prints_parquet_rows_with_duckdb_python(self):
        class FakeCursor:
            description = [("a",), ("b",)]

            def __init__(self):
                self.batches = [[(1, "two")], [(None, "four")], []]
                self.last_size = None

            def fetchmany(self, size):
                self.last_size = size
                return self.batches.pop(0)

        class FakeConnection:
            def __init__(self):
                self.closed = False
                self.calls = []

            def execute(self, sql, params):
                self.calls.append((sql, params))
                self.cursor = FakeCursor()
                return self.cursor

            def close(self):
                self.closed = True

        fake_connection = FakeConnection()
        fake_duckdb = types.SimpleNamespace(connect=lambda: fake_connection)

        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "result.parquet"
            result_path.write_bytes(b"PAR1")
            with mock.patch.dict(sys.modules, {"duckdb": fake_duckdb}):
                status, stdout, stderr, _calls, _wait, _downloader = self.run_query(
                    raw_json=False,
                    print_rows=True,
                    download={
                        "job_result_id": "result-1",
                        "dataset_ref": "dataset-1",
                        "result_extension": "parquet",
                        "path": str(result_path),
                        "bytes": result_path.stat().st_size,
                    },
                )

        self.assertEqual(status, 0, stderr)
        self.assertEqual(stdout, "a\tb\n1\ttwo\n\tfour\n")
        self.assertEqual(fake_connection.cursor.last_size, 1000)
        self.assertEqual(
            fake_connection.calls,
            [("SELECT * FROM read_parquet(?)", [str(result_path)])],
        )
        self.assertTrue(fake_connection.closed)

    def test_query_rejects_print_and_json_together(self):
        status, stdout, stderr, _calls, _wait, _downloader = self.run_query(
            raw_json=True,
            print_rows=True,
        )

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("Use either --print or --json, not both.", stderr)


class WaitForQueryJobTest(unittest.TestCase):
    def test_wait_fails_fast_on_unspecified_status_without_error(self):
        with mock.patch.object(
            cli,
            "fetch_query_job",
            return_value={"job_status": "JOB_STATUS_UNSPECIFIED"},
        ) as fetch:
            with self.assertRaisesRegex(RuntimeError, "JOB_STATUS_UNSPECIFIED"):
                cli.wait_for_query_job("job-1", wait=True, timeout_seconds=300, interval_seconds=5)

        fetch.assert_called_once_with("job-1", timeout_seconds=30)

    def test_wait_fails_fast_on_numeric_unspecified_status_without_error(self):
        with mock.patch.object(
            cli,
            "fetch_query_job",
            return_value={"job_status": 0},
        ):
            with self.assertRaisesRegex(RuntimeError, "0"):
                cli.wait_for_query_job("job-1", wait=True, timeout_seconds=300, interval_seconds=5)


if __name__ == "__main__":
    unittest.main()
