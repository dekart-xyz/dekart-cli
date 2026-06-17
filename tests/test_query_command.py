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
                {"result": {"report": {"id": "report-1"}, "datasets": [{"id": "dataset-1"}], "queries": []}},
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
                    report_id=kwargs.get("report_id", "report-1"),
                    dataset_id=kwargs.get("dataset_id", "dataset-1"),
                    sql_file=kwargs.get("sql_file", str(sql_file)),
                    sql=kwargs.get("inline_sql"),
                    out_dir=kwargs.get("out_dir", directory),
                    deprecated_out=kwargs.get("deprecated_out"),
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
            ["get_report_properties", "create_query", "update_query", "run_query"],
        )
        self.assertEqual(calls[0][1], {"report_id": "report-1"})
        self.assertEqual(calls[1][1], {"dataset_id": "dataset-1", "connection_id": "connection-1"})
        self.assertEqual(calls[2][1], {"query_id": "query-1", "query_text": "SELECT 1"})
        self.assertEqual(calls[3][1], {"query_id": "query-1"})
        wait.assert_called_once_with("job-1", True, 300, 5)
        downloader.assert_called_once()
        self.assertEqual(Path(downloader.call_args.args[2]).name, "result-1.parquet")

        payload = json.loads(stdout)
        self.assertEqual(payload["report_id"], "report-1")
        self.assertEqual(payload["dataset_id"], "dataset-1")
        self.assertEqual(payload["query_id"], "query-1")
        self.assertEqual(payload["job_id"], "job-1")
        self.assertEqual(payload["terminal_status"], "JOB_STATUS_DONE")
        self.assertEqual(payload["report_url"], "https://dekart/reports/report-1")
        self.assertEqual(payload["bytes"], 10)
        self.assertEqual(payload["result_file"], payload["path"])

    def test_query_reuses_existing_dataset_query(self):
        status, stdout, stderr, calls, _wait, _downloader = self.run_query(
            raw_json=True,
            mcp_responses=[
                {
                    "result": {
                        "report": {"id": "report-1"},
                        "datasets": [{"id": "dataset-1", "query_id": "query-1"}],
                        "queries": [{"id": "query-1"}],
                    }
                },
                {"result": {}},
                {"result": {"query_job": {"id": "job-1"}}},
            ],
        )

        self.assertEqual(status, 0, stderr)
        self.assertEqual(
            [name for name, _args, _timeout, _return_metadata in calls],
            ["get_report_properties", "update_query", "run_query"],
        )
        self.assertEqual(calls[1][1], {"query_id": "query-1", "query_text": "SELECT 1"})
        self.assertEqual(json.loads(stdout)["query_id"], "query-1")

    def test_query_rejects_dataset_missing_from_report(self):
        status, stdout, stderr, calls, _wait, downloader = self.run_query(
            raw_json=True,
            mcp_responses=[
                {
                    "result": {
                        "report": {"id": "report-1"},
                        "datasets": [{"id": "other-dataset"}],
                        "queries": [],
                    }
                },
            ],
        )

        self.assertEqual(status, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Dataset dataset-1 was not found in report properties", stderr)
        self.assertEqual([name for name, _args, _timeout, _return_metadata in calls], ["get_report_properties"])
        downloader.assert_not_called()

    def test_query_rejects_deprecated_out(self):
        status, stdout, stderr, calls, _wait, _downloader = self.run_query(
            raw_json=True,
            deprecated_out="/tmp/result.parquet",
        )

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("--out was removed; use --out-dir", stderr)
        self.assertEqual(calls, [])

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


class PreviewCommandTest(unittest.TestCase):
    def test_preview_rejects_missing_file(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = cli.handle_preview("/tmp/does-not-exist.parquet", limit=20, schema=False)

        self.assertEqual(status, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("File does not exist", stderr.getvalue())

    def test_preview_prints_rows_with_duckdb(self):
        class FakeCursor:
            description = [("a",), ("b",)]

            def __init__(self):
                self.batches = [[(1, "two")], []]

            def fetchmany(self, _size):
                return self.batches.pop(0)

        class FakeConnection:
            def __init__(self):
                self.calls = []
                self.closed = False

            def execute(self, sql, params):
                self.calls.append((sql, params))
                return FakeCursor()

            def close(self):
                self.closed = True

        fake_connection = FakeConnection()
        fake_duckdb = types.SimpleNamespace(connect=lambda: fake_connection)

        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "result.parquet"
            result_path.write_bytes(b"PAR1")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch.dict(sys.modules, {"duckdb": fake_duckdb}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    status = cli.handle_preview(str(result_path), limit=20, schema=False)

        self.assertEqual(status, 0, stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "a\tb\n1\ttwo\n")
        self.assertEqual(
            fake_connection.calls,
            [("SELECT * FROM read_parquet(?)", [str(result_path)])],
        )
        self.assertTrue(fake_connection.closed)

    def test_preview_prints_schema_with_duckdb(self):
        class FakeCursor:
            def fetchall(self):
                return [("a", "INTEGER"), ("b", "VARCHAR")]

        class FakeConnection:
            def __init__(self):
                self.calls = []

            def execute(self, sql, params):
                self.calls.append((sql, params))
                return FakeCursor()

            def close(self):
                pass

        fake_connection = FakeConnection()
        fake_duckdb = types.SimpleNamespace(connect=lambda: fake_connection)

        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "result.csv"
            result_path.write_text("a,b\n1,two\n", encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch.dict(sys.modules, {"duckdb": fake_duckdb}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    status = cli.handle_preview(str(result_path), limit=20, schema=True)

        self.assertEqual(status, 0, stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "a\tINTEGER\nb\tVARCHAR\n")
        self.assertEqual(
            fake_connection.calls,
            [("DESCRIBE SELECT * FROM read_csv_auto(?)", [str(result_path)])],
        )


class ParserTest(unittest.TestCase):
    def test_fetch_job_is_not_a_subcommand(self):
        parser = cli.build_parser()
        help_text = parser.format_help()
        self.assertNotIn("fetch-job", help_text)

    def test_old_query_out_flag_reaches_deprecation_handler(self):
        parser = cli.build_parser()
        args = parser.parse_args(["query", "--connection-id", "connection-1", "--sql", "SELECT 1", "--out", "/tmp/result.parquet"])
        self.assertEqual(args.command, "query")
        self.assertEqual(args.deprecated_out, "/tmp/result.parquet")


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
