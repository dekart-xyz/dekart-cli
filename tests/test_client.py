import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from dekart import cli, client, duckdb_execution


class Response:
    headers = {}

    def __init__(self, body=b'{}'):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return self.body


class ClientTest(unittest.TestCase):
    def test_explicit_clients_do_not_read_config_and_keep_credentials_separate(self):
        requests = []

        def open_request(request, timeout):
            requests.append(request)
            return Response(b'{"tools": []}')

        with mock.patch.object(cli, 'load_config', side_effect=AssertionError('config read')), mock.patch.object(
            cli, 'load_token', side_effect=AssertionError('token read')
        ), mock.patch.object(cli, 'mint_google_access_token', side_effect=AssertionError('gcloud run')), mock.patch.object(
            client.urllib.request, 'urlopen', side_effect=open_request
        ):
            clients = [client.DekartClient('https://one.example/root/', 'one'),
                       client.DekartClient('https://two.example', 'two')]
            with ThreadPoolExecutor(2) as pool:
                list(pool.map(lambda c: (c.list_tools(), c.call('test', {})), clients))
        self.assertEqual(len(requests), 4)
        for request in requests:
            expected = 'one' if request.full_url.startswith('https://one.example/root/') else 'two'
            self.assertEqual(request.get_header('Authorization'), 'Bearer ' + expected)
            self.assertTrue(request.get_header('User-agent').startswith('dekart-cli/'))
            self.assertIsNone(request.get_header('X-dekart-google-access-token'))

    def test_import_does_not_access_configuration_or_start_work(self):
        script = '''
import pathlib, subprocess, threading, urllib.request
from unittest import mock
with mock.patch.object(pathlib.Path, 'read_text', side_effect=AssertionError('read file')), \\
     mock.patch.object(pathlib.Path, 'write_text', side_effect=AssertionError('write file')), \\
     mock.patch.object(subprocess, 'run', side_effect=AssertionError('run')), \\
     mock.patch.object(threading.Thread, 'start', side_effect=AssertionError('thread')), \\
     mock.patch.object(urllib.request, 'urlopen', side_effect=AssertionError('network')):
    import dekart.client
assert 'mcp' not in __import__('sys').modules
assert 'dekart.cli' not in __import__('sys').modules
'''
        subprocess.run([sys.executable, '-c', script], check=True, capture_output=True)

    def test_old_python_serve_reports_version_before_telemetry(self):
        stderr = io.StringIO()
        with mock.patch.object(sys, 'argv', ['dekart', 'mcp', 'serve']), mock.patch.object(
            sys, 'version_info', (3, 8)
        ), mock.patch.object(cli, 'send_version_ping') as ping, mock.patch('sys.stderr', stderr):
            with self.assertRaises(SystemExit) as result:
                cli.main()
        self.assertEqual(result.exception.code, 2)
        self.assertIn('needs Python 3.10 or newer', stderr.getvalue())
        ping.assert_not_called()

    def test_mcp_extra_is_excluded_on_python_38(self):
        import re
        config = (Path(__file__).parents[1] / 'pyproject.toml').read_text()
        extra = config.split('[project.optional-dependencies]')[1].split('[project.scripts]')[0]
        self.assertRegex(extra, r"mcp = \[.*mcp.*python_version >= '3\.10'")

    def test_inline_upload_uses_client_for_every_part(self):
        c = client.DekartClient('https://dekart.example', 'token')
        with mock.patch.object(c, '_upload_part_binary', return_value={'etag': 'ok'}) as put:
            result = client.upload_parts_from_bytes(c, b'abcdef', {
                'upload_session_id': 'session', 'file_id': 'file', 'max_part_size': 4,
                'upload_part_endpoint': '/parts/{part_number}',
            }, '', '', None, '')
        self.assertEqual([call.args[1] for call in put.call_args_list], [b'abcd', b'ef'])
        self.assertEqual(result['complete_args']['total_size'], 6)


class DuckDBLimitsTest(unittest.TestCase):
    def metadata(self, sql):
        return {
            'query_id': 'query', 'job_id': 'root',
            'query_job': {'id': 'root', 'dataset_id': 'dataset'},
            'duckdb_execution': {
                'duckdb_version': '1.4.3', 'extensions': [], 'sources': [],
                'statements': [{'sql': 'CREATE SCHEMA dekart_internal'}, {
                    'sql': 'CREATE TABLE dekart_internal.job_root AS ' + sql,
                }],
            },
        }

    def test_real_query_receives_memory_limit_before_lock(self):
        import duckdb
        c = client.DekartClient('https://dekart.example', 'token')
        with tempfile.TemporaryDirectory() as directory:
            result = client.materialize_duckdb_query(c, self.metadata("SELECT current_setting('memory_limit') AS memory"),
                directory, True, 30, 1, memory_limit='64MB', duckdb_timeout_seconds=5)
            connection = duckdb.connect()
            try:
                value = connection.execute('SELECT memory FROM read_parquet(?)', [result['path']]).fetchone()[0]
            finally:
                connection.close()
            self.assertIn(value, ('61.0 MiB', '64.0 MB'))

    def test_real_query_is_interrupted_and_temporary_files_removed(self):
        c = client.DekartClient('https://dekart.example', 'token')
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(TimeoutError, "users' browsers.*duckdb_timeout_seconds"):
                client.materialize_duckdb_query(c, self.metadata('SELECT sum(sin(i)) FROM range(100000000000) t(i)'),
                    directory, True, 30, 1, duckdb_timeout_seconds=0.1)
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertLess(time.monotonic() - started, 3)

    def test_downloads_share_execution_deadline(self):
        metadata = self.metadata('SELECT 1')
        metadata['duckdb_execution']['sources'] = [
            {'dataset_id': 'data', 'file_source_id': 'file', 'extension': 'csv'},
        ]
        c = client.DekartClient('https://dekart.example', 'token')

        def download(*args, **kwargs):
            self.assertLessEqual(kwargs['timeout_seconds'], 0.05)
            time.sleep(0.06)
            return b'a\n1\n'

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            c, '_download_binary', side_effect=download
        ), mock.patch.object(duckdb_execution, 'execute_program') as execute:
            with self.assertRaisesRegex(TimeoutError, 'duckdb_timeout_seconds'):
                client.materialize_duckdb_query(c, metadata, directory, True, 30, 1, duckdb_timeout_seconds=0.05)
            execute.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
