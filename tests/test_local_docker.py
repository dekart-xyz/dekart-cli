import io
import json
import unittest
import urllib.error
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from unittest import mock

from dekart import cli, local_docker


def completed(code=0, stdout="", stderr=""):
    return {"code": code, "stdout": stdout, "stderr": stderr, "command": "docker"}


class DockerContractTest(unittest.TestCase):
    def test_run_command_streams_and_captures_output(self):
        process = mock.Mock()
        process.stdout = io.StringIO("Pulling image\ncontainer-id\n")
        process.wait.return_value = 0
        stdout = io.StringIO()
        with mock.patch.object(local_docker.subprocess, "Popen", return_value=process) as popen, redirect_stdout(stdout):
            result = local_docker.run_command(["docker", "run", "image"], stream_output=True)
        self.assertEqual(stdout.getvalue(), "Pulling image\ncontainer-id\n")
        self.assertEqual(result["stdout"], stdout.getvalue())
        self.assertEqual(result["code"], 0)
        popen.assert_called_once_with(
            ["docker", "run", "image"],
            stdout=local_docker.subprocess.PIPE,
            stderr=local_docker.subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

    def test_run_command_is_persistent_labeled_and_loopback_only(self):
        command = local_docker.docker_run_command(8083)
        rendered = local_docker.format_command(command)
        self.assertIn("--name dekart-local", rendered)
        self.assertIn("--label xyz.dekart.cli.managed=true", rendered)
        self.assertIn("--restart unless-stopped", rendered)
        self.assertIn("-p 127.0.0.1:8083:8080", rendered)
        self.assertIn("-v dekart-local-data:/dekart/data", rendered)
        self.assertTrue(rendered.endswith("dekartxyz/dekart:latest"))

    def test_standalone_run_command_has_no_cli_management_details(self):
        command = local_docker.standalone_docker_run_command(8084)
        self.assertEqual(command, ["docker", "run", "-p", "8084:8080", "dekartxyz/dekart"])
        self.assertNotIn("--name", command)
        self.assertNotIn("--label", command)
        self.assertNotIn("-v", command)

    def test_parser_supports_local_commands(self):
        parser = cli.build_parser()
        self.assertEqual(parser.parse_args(["local"]).local_action, "status")
        self.assertEqual(parser.parse_args(["local", "up"]).local_action, "up")
        self.assertEqual(parser.parse_args(["local", "down"]).local_action, "down")
        self.assertEqual(parser.parse_args(["local", "remove"]).local_action, "remove")
        self.assertTrue(parser.parse_args(["local", "remove", "--force"]).force)
        args = parser.parse_args(["local", "status", "--json"])
        self.assertEqual(args.local_action, "status")
        self.assertTrue(args.json)

    @mock.patch.object(local_docker.shutil, "which", return_value=None)
    def test_docker_missing(self, _which):
        self.assertEqual(local_docker.docker_availability(), "missing")

    @mock.patch.object(local_docker, "run_command", return_value=completed(1, stderr="daemon stopped"))
    @mock.patch.object(local_docker, "current_docker_host", return_value="unix:///var/run/docker.sock")
    @mock.patch.object(local_docker.shutil, "which", return_value="/usr/bin/docker")
    def test_docker_stopped(self, _which, _host, _run):
        self.assertEqual(local_docker.docker_availability(), "stopped")

    @mock.patch.object(local_docker, "run_command", return_value=completed())
    @mock.patch.object(local_docker, "current_docker_host", return_value="unix:///var/run/docker.sock")
    @mock.patch.object(local_docker.shutil, "which", return_value="/usr/bin/docker")
    def test_docker_running(self, _which, _host, _run):
        self.assertEqual(local_docker.docker_availability(), "running")

    def test_unclassified_docker_context_fails_closed(self):
        with mock.patch.object(local_docker.shutil, "which", return_value="/usr/bin/docker"), mock.patch.object(
            local_docker, "current_docker_host", return_value=""
        ), mock.patch.object(local_docker, "run_command") as run:
            self.assertEqual(local_docker.docker_availability(), "unknown")
        run.assert_not_called()

    def test_remote_docker_context_is_refused(self):
        with mock.patch.object(local_docker.shutil, "which", return_value="/usr/bin/docker"), mock.patch.object(
            local_docker, "current_docker_host", return_value="ssh://builder.example.com"
        ), mock.patch.object(local_docker, "run_command") as run:
            self.assertEqual(local_docker.docker_availability(), "remote")
        run.assert_not_called()

    def test_local_docker_hosts_are_allowed(self):
        self.assertTrue(local_docker.is_local_docker_host("unix:///var/run/docker.sock"))
        self.assertTrue(local_docker.is_local_docker_host("npipe:////./pipe/docker_engine"))
        self.assertTrue(local_docker.is_local_docker_host("tcp://127.0.0.1:2375"))
        self.assertFalse(local_docker.is_local_docker_host("tcp://10.0.0.4:2375"))
        self.assertFalse(local_docker.is_local_docker_host("ssh://builder.example.com"))

    def test_first_available_port_uses_order(self):
        with mock.patch.object(local_docker, "is_port_free", side_effect=lambda port: port == 8082):
            self.assertEqual(local_docker.first_available_port(), 8082)

    @mock.patch.object(local_docker, "is_port_free", return_value=False)
    def test_port_range_exhaustion(self, _free):
        self.assertIsNone(local_docker.first_available_port())

    def test_dekart_probe_requires_health_and_root_marker(self):
        with mock.patch.object(local_docker, "_read_url", side_effect=[(200, ""), (200, "<title>Dekart</title>")]):
            self.assertTrue(local_docker.is_dekart_endpoint("http://localhost:8080"))
        with mock.patch.object(local_docker, "_read_url", side_effect=[(200, ""), (200, "other app")]):
            self.assertFalse(local_docker.is_dekart_endpoint("http://localhost:8080"))

    @mock.patch.object(local_docker, "_read_url", side_effect=urllib.error.URLError("no"))
    def test_dekart_probe_handles_connection_failure(self, _read):
        self.assertFalse(local_docker.is_dekart_endpoint("http://localhost:8080"))


class InspectContainerTest(unittest.TestCase):
    def inspect(self, *, label="true", running=True, port="8084"):
        payload = [{
            "Config": {"Labels": {local_docker.MANAGED_LABEL: label}},
            "State": {"Running": running},
            "HostConfig": {"PortBindings": {"8080/tcp": [{"HostPort": port}]}},
        }]
        with mock.patch.object(local_docker, "run_command", return_value=completed(stdout=json.dumps(payload))):
            return local_docker.inspect_container()

    def test_managed_running_container(self):
        self.assertEqual(self.inspect(), {"invalid": False, "managed": True, "running": True, "port": 8084, "volumes": []})

    def test_unlabeled_container_is_foreign(self):
        self.assertFalse(self.inspect(label="")["managed"])

    def test_stopped_container(self):
        self.assertFalse(self.inspect(running=False)["running"])

    @mock.patch.object(local_docker, "run_command", return_value=completed(1))
    def test_missing_container(self, _run):
        self.assertIsNone(local_docker.inspect_container())

    def test_removal_inspection_requires_real_not_found_error(self):
        with mock.patch.object(
            local_docker, "run_command", return_value=completed(1, stderr="Error: No such object: dekart-local")
        ):
            self.assertEqual(local_docker.inspect_container_for_removal()["state"], "absent")
        with mock.patch.object(local_docker, "run_command", return_value=completed(1, stderr="permission denied")):
            self.assertEqual(local_docker.inspect_container_for_removal()["state"], "error")

    def test_removal_inspection_reads_container_volume_attachment(self):
        payload = [{
            "Config": {"Labels": {local_docker.MANAGED_LABEL: local_docker.MANAGED_LABEL_VALUE}},
            "Mounts": [{"Type": "volume", "Name": local_docker.VOLUME_NAME}],
        }]
        with mock.patch.object(local_docker, "run_command", return_value=completed(stdout=json.dumps(payload))):
            inspected = local_docker.inspect_container_for_removal()
        self.assertTrue(inspected["managed"])
        self.assertEqual(inspected["volumes"], [local_docker.VOLUME_NAME])


class StatusTest(unittest.TestCase):
    def test_managed_healthy_status(self):
        container = {"managed": True, "running": True, "port": 8082}
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(local_docker, "docker_availability", return_value="running"))
            stack.enter_context(mock.patch.object(local_docker, "inspect_container", return_value=container))
            stack.enter_context(mock.patch.object(local_docker, "is_dekart_endpoint", return_value=True))
            status = local_docker.get_status()
        self.assertEqual(status["status"], "running")
        self.assertEqual(status["url"], "http://localhost:8082")
        self.assertTrue(status["managed"])

    def test_managed_unhealthy_status(self):
        container = {"managed": True, "running": True, "port": 8082}
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(local_docker, "docker_availability", return_value="running"))
            stack.enter_context(mock.patch.object(local_docker, "inspect_container", return_value=container))
            stack.enter_context(mock.patch.object(local_docker, "is_dekart_endpoint", return_value=False))
            self.assertEqual(local_docker.get_status()["status"], "unhealthy")

    def test_external_dekart_is_reused(self):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(local_docker, "docker_availability", return_value="running"))
            stack.enter_context(mock.patch.object(local_docker, "inspect_container", return_value=None))
            stack.enter_context(mock.patch.object(local_docker, "is_port_free", side_effect=lambda port: port != 8080))
            stack.enter_context(
                mock.patch.object(
                    local_docker,
                    "is_dekart_endpoint",
                    side_effect=lambda url, **_kwargs: url.endswith(":8080"),
                )
            )
            status = local_docker.get_status()
        self.assertEqual(status["status"], "external")
        self.assertFalse(status["managed"])

    def test_busy_non_dekart_8080_selects_8081(self):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(local_docker, "docker_availability", return_value="running"))
            stack.enter_context(mock.patch.object(local_docker, "inspect_container", return_value=None))
            stack.enter_context(mock.patch.object(local_docker, "is_dekart_endpoint", return_value=False))
            stack.enter_context(mock.patch.object(local_docker, "is_port_free", side_effect=lambda port: port == 8081))
            status = local_docker.get_status()
        self.assertEqual(status["status"], "absent")
        self.assertEqual(status["port"], 8081)

    def test_unlabeled_same_name_reports_conflict(self):
        container = {"managed": False, "running": False, "port": 8080}
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(local_docker, "docker_availability", return_value="running"))
            stack.enter_context(mock.patch.object(local_docker, "inspect_container", return_value=container))
            status = local_docker.get_status()
        self.assertEqual(status["status"], "ownership_conflict")

    def test_json_status_is_machine_clean(self):
        status = local_docker._base_status("running", port=8080, managed=True, healthy=True)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            local_docker.print_status(status, raw_json=True)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "running")

    def test_human_status_does_not_claim_to_execute_docker(self):
        status = local_docker._base_status("running", port=8080, managed=True, healthy=True)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            local_docker.print_status(status)
        self.assertNotIn("Equivalent Docker command", stdout.getvalue())
        self.assertNotIn("Executing:", stdout.getvalue())


class LifecycleTest(unittest.TestCase):
    def test_prepare_data_volume_creates_it_with_management_label(self):
        executed = []
        with mock.patch.object(
            local_docker,
            "inspect_volume_for_removal",
            side_effect=[{"state": "absent"}, {"state": "present", "managed": True}],
        ), mock.patch.object(local_docker, "run_command", return_value=completed()) as run:
            result = local_docker.prepare_data_volume(on_execute=executed.append)
        command = [
            "docker", "volume", "create", "--label",
            "xyz.dekart.cli.managed=true", "dekart-local-data",
        ]
        self.assertEqual(result["code"], 0)
        run.assert_called_once_with(command)
        self.assertEqual(executed, ["docker volume create --label xyz.dekart.cli.managed=true dekart-local-data"])

    def test_prepare_data_volume_rejects_unmanaged_volume_reused_during_create(self):
        with mock.patch.object(
            local_docker,
            "inspect_volume_for_removal",
            side_effect=[{"state": "absent"}, {"state": "present", "managed": False}],
        ), mock.patch.object(local_docker, "run_command", return_value=completed()) as run:
            result = local_docker.prepare_data_volume()
        self.assertEqual(result["code"], 2)
        self.assertIn("refusing to use it", result["message"])
        run.assert_called_once()

    def test_prepare_data_volume_refuses_unowned_existing_volume(self):
        with mock.patch.object(
            local_docker, "inspect_volume_for_removal", return_value={"state": "present", "managed": False}
        ), mock.patch.object(local_docker, "run_command") as run:
            result = local_docker.prepare_data_volume()
        self.assertEqual(result["code"], 2)
        run.assert_not_called()

    def test_remove_deletes_managed_container_and_data_volume(self):
        running = local_docker._base_status("running", port=8080, managed=True, healthy=True)
        absent = local_docker._base_status("absent", port=8080)
        container = {"state": "present", "managed": True, "volumes": ["dekart-local-data"]}
        volume = {"state": "present", "managed": False}
        executed = []
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(local_docker, "get_status", side_effect=[running, absent]))
            stack.enter_context(mock.patch.object(local_docker, "inspect_container_for_removal", return_value=container))
            stack.enter_context(mock.patch.object(local_docker, "inspect_volume_for_removal", return_value=volume))
            run = stack.enter_context(mock.patch.object(local_docker, "run_command", side_effect=[completed(), completed()]))
            result = local_docker.remove(on_execute=executed.append)
        self.assertEqual(result["code"], 0)
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [["docker", "rm", "-f", "dekart-local"], ["docker", "volume", "rm", "dekart-local-data"]],
        )
        self.assertEqual(executed, ["docker rm -f dekart-local", "docker volume rm dekart-local-data"])
        self.assertIn("permanently deleted", result["message"])

    def test_remove_deletes_leftover_data_volume_without_container(self):
        absent = local_docker._base_status("absent", port=8080)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(local_docker, "get_status", side_effect=[absent, absent]))
            stack.enter_context(mock.patch.object(local_docker, "inspect_container_for_removal", return_value={"state": "absent"}))
            stack.enter_context(mock.patch.object(local_docker, "inspect_volume_for_removal", return_value={"state": "present", "managed": True}))
            run = stack.enter_context(mock.patch.object(local_docker, "run_command", return_value=completed()))
            result = local_docker.remove()
        self.assertEqual(result["code"], 0)
        run.assert_called_once_with(["docker", "volume", "rm", "dekart-local-data"])

    def test_remove_refuses_unowned_leftover_volume(self):
        absent = local_docker._base_status("absent", port=8080)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(local_docker, "get_status", return_value=absent))
            stack.enter_context(mock.patch.object(local_docker, "inspect_container_for_removal", return_value={"state": "absent"}))
            stack.enter_context(mock.patch.object(local_docker, "inspect_volume_for_removal", return_value={"state": "present", "managed": False}))
            run = stack.enter_context(mock.patch.object(local_docker, "run_command"))
            result = local_docker.remove()
        self.assertEqual(result["code"], 2)
        self.assertIn("cannot be proven", result["message"])
        run.assert_not_called()

    def test_remove_rechecks_container_ownership_before_mutation(self):
        stopped = local_docker._base_status("stopped", port=8080, managed=True)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(local_docker, "get_status", return_value=stopped))
            stack.enter_context(mock.patch.object(local_docker, "inspect_container_for_removal", return_value={"state": "present", "managed": False, "volumes": []}))
            run = stack.enter_context(mock.patch.object(local_docker, "run_command"))
            result = local_docker.remove()
        self.assertEqual(result["code"], 2)
        run.assert_not_called()

    def test_remove_stops_when_volume_inspection_fails(self):
        stopped = local_docker._base_status("stopped", port=8080, managed=True)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(local_docker, "get_status", return_value=stopped))
            stack.enter_context(mock.patch.object(local_docker, "inspect_container_for_removal", return_value={"state": "present", "managed": True, "volumes": ["dekart-local-data"]}))
            stack.enter_context(mock.patch.object(local_docker, "inspect_volume_for_removal", return_value={"state": "error", "message": "permission denied"}))
            run = stack.enter_context(mock.patch.object(local_docker, "run_command"))
            result = local_docker.remove()
        self.assertEqual(result["code"], 1)
        self.assertIn("Could not verify data volume ownership", result["message"])
        run.assert_not_called()

    def test_remove_reports_volume_only_failure_without_claiming_container_removal(self):
        absent = local_docker._base_status("absent", port=8080)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(local_docker, "get_status", side_effect=[absent, absent]))
            stack.enter_context(mock.patch.object(local_docker, "inspect_container_for_removal", return_value={"state": "absent"}))
            stack.enter_context(mock.patch.object(local_docker, "inspect_volume_for_removal", return_value={"state": "present", "managed": True}))
            stack.enter_context(mock.patch.object(local_docker, "run_command", return_value=completed(1, stderr="in use")))
            result = local_docker.remove()
        self.assertEqual(result["code"], 1)
        self.assertNotIn("container was removed", result["message"])

    def test_remove_can_retry_labeled_volume_after_partial_failure(self):
        running = local_docker._base_status("running", port=8080, managed=True, healthy=True)
        absent = local_docker._base_status("absent", port=8080)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(local_docker, "get_status", side_effect=[running, absent, absent, absent]))
            stack.enter_context(mock.patch.object(
                local_docker,
                "inspect_container_for_removal",
                side_effect=[
                    {"state": "present", "managed": True, "volumes": ["dekart-local-data"]},
                    {"state": "absent"},
                ],
            ))
            stack.enter_context(mock.patch.object(
                local_docker,
                "inspect_volume_for_removal",
                return_value={"state": "present", "managed": True},
            ))
            stack.enter_context(mock.patch.object(
                local_docker,
                "run_command",
                side_effect=[completed(), completed(1, stderr="busy"), completed()],
            ))
            first = local_docker.remove()
            second = local_docker.remove()
        self.assertEqual(first["code"], 1)
        self.assertEqual(second["code"], 0)

    def test_remove_refuses_external_dekart_without_mutation(self):
        external = local_docker._base_status("external", port=8080, managed=False, healthy=True)
        with mock.patch.object(local_docker, "get_status", return_value=external), mock.patch.object(
            local_docker, "run_command"
        ) as run:
            result = local_docker.remove()
        self.assertEqual(result["code"], 2)
        run.assert_not_called()

    def test_remove_confirmation_defaults_to_cancel(self):
        stdout = io.StringIO()
        with mock.patch("builtins.input", return_value=""), mock.patch.object(local_docker, "remove") as remove, redirect_stdout(stdout):
            code = local_docker.handle_local("remove")
        self.assertEqual(code, 0)
        remove.assert_not_called()
        self.assertIn("No container or data was deleted", stdout.getvalue())

    def test_remove_force_skips_confirmation(self):
        absent = local_docker._base_status("absent", port=8080)
        result = {"code": 0, "message": "removed", "status": absent}
        with mock.patch("builtins.input") as prompt, mock.patch.object(
            local_docker, "remove", return_value=result
        ) as remove, redirect_stdout(io.StringIO()):
            code = local_docker.handle_local("remove", force=True)
        self.assertEqual(code, 0)
        prompt.assert_not_called()
        self.assertIn("on_execute", remove.call_args.kwargs)

    def test_up_creates_and_waits_for_healthy_dekart(self):
        absent = local_docker._base_status("absent", port=8081)
        running = local_docker._base_status("running", port=8081, managed=True, healthy=True)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(local_docker, "get_status", side_effect=[absent, running]))
            stack.enter_context(mock.patch.object(local_docker, "inspect_container", return_value=None))
            stack.enter_context(mock.patch.object(local_docker, "prepare_data_volume", return_value={"code": 0}))
            run = stack.enter_context(mock.patch.object(local_docker, "run_command", return_value=completed()))
            stack.enter_context(mock.patch.object(local_docker, "wait_for_dekart", return_value=True))
            result = local_docker.up()
        self.assertEqual(result["code"], 0)
        self.assertEqual(run.call_args.args[0], local_docker.docker_run_command(8081))
        self.assertTrue(run.call_args.kwargs["stream_output"])

    def test_up_starts_stopped_managed_container(self):
        stopped = local_docker._base_status("stopped", port=8080, managed=True)
        running = local_docker._base_status("running", port=8080, managed=True, healthy=True)
        container = {"managed": True, "running": False, "port": 8080}
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(local_docker, "get_status", side_effect=[stopped, running]))
            stack.enter_context(mock.patch.object(local_docker, "inspect_container", return_value=container))
            stack.enter_context(mock.patch.object(local_docker, "is_port_free", return_value=True))
            run = stack.enter_context(mock.patch.object(local_docker, "run_command", return_value=completed()))
            stack.enter_context(mock.patch.object(local_docker, "wait_for_dekart", return_value=True))
            executed = []
            result = local_docker.up(on_execute=executed.append)
        self.assertEqual(result["code"], 0)
        run.assert_called_once_with(["docker", "start", "dekart-local"])
        self.assertEqual(executed, ["docker start dekart-local"])

    def test_up_recreates_stopped_managed_container_on_new_port(self):
        stopped = local_docker._base_status("stopped", port=8080, managed=True)
        running = local_docker._base_status("running", port=8081, managed=True, healthy=True)
        container = {"managed": True, "running": False, "port": 8080}
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(local_docker, "get_status", side_effect=[stopped, running]))
            stack.enter_context(mock.patch.object(local_docker, "inspect_container", return_value=container))
            stack.enter_context(mock.patch.object(local_docker, "is_port_free", return_value=False))
            stack.enter_context(mock.patch.object(local_docker, "first_available_port", return_value=8081))
            stack.enter_context(mock.patch.object(local_docker, "prepare_data_volume", return_value={"code": 0}))
            run = stack.enter_context(mock.patch.object(local_docker, "run_command", return_value=completed()))
            stack.enter_context(mock.patch.object(local_docker, "wait_for_dekart", return_value=True))
            result = local_docker.up()
        self.assertEqual(result["code"], 0)
        self.assertEqual(run.call_args_list[0].args[0], ["docker", "rm", "dekart-local"])
        self.assertIn("dekart-local-data:/dekart/data", run.call_args_list[1].args[0])
        self.assertTrue(run.call_args_list[1].kwargs["stream_output"])

    def test_up_refuses_foreign_container(self):
        conflict = local_docker._base_status("ownership_conflict", port=8080)
        conflict["message"] = "foreign"
        with mock.patch.object(local_docker, "get_status", return_value=conflict), mock.patch.object(local_docker, "run_command") as run:
            result = local_docker.up()
        self.assertEqual(result["code"], 2)
        run.assert_not_called()

    def test_up_timeout_prints_logs_and_recovery(self):
        absent = local_docker._base_status("absent", port=8080)
        unhealthy = local_docker._base_status("unhealthy", port=8080, managed=True)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(local_docker, "get_status", side_effect=[absent, unhealthy]))
            stack.enter_context(mock.patch.object(local_docker, "inspect_container", return_value=None))
            stack.enter_context(mock.patch.object(local_docker, "prepare_data_volume", return_value={"code": 0}))
            stack.enter_context(mock.patch.object(local_docker, "run_command", side_effect=[completed(), completed(stdout="boot failed")]))
            stack.enter_context(mock.patch.object(local_docker, "wait_for_dekart", return_value=False))
            result = local_docker.up(wait_timeout=1)
        self.assertEqual(result["code"], 1)
        self.assertIn("boot failed", result["logs"])
        self.assertIn("docker logs dekart-local", result["recovery_commands"])

    def test_up_rewaits_for_running_unhealthy_container_without_mutation(self):
        unhealthy = local_docker._base_status("unhealthy", port=8080, managed=True)
        running = local_docker._base_status("running", port=8080, managed=True, healthy=True)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(local_docker, "get_status", side_effect=[unhealthy, running]))
            wait = stack.enter_context(mock.patch.object(local_docker, "wait_for_dekart", return_value=True))
            run = stack.enter_context(mock.patch.object(local_docker, "run_command"))
            result = local_docker.up(wait_timeout=2)
        self.assertEqual(result["code"], 0)
        wait.assert_called_once_with("http://localhost:8080", timeout_seconds=2)
        run.assert_not_called()

    def test_down_stops_but_never_deletes(self):
        running = local_docker._base_status("running", port=8080, managed=True, healthy=True)
        stopped = local_docker._base_status("stopped", port=8080, managed=True)
        with mock.patch.object(local_docker, "get_status", side_effect=[running, stopped]), mock.patch.object(local_docker, "run_command", return_value=completed()) as run:
            executed = []
            result = local_docker.down(on_execute=executed.append)
        self.assertEqual(result["code"], 0)
        run.assert_called_once_with(["docker", "stop", "dekart-local"])
        self.assertEqual(executed, ["docker stop dekart-local"])
        self.assertIn("Data remains", result["message"])

    def test_down_refuses_external_dekart(self):
        external = local_docker._base_status("external", port=8080, healthy=True)
        with mock.patch.object(local_docker, "get_status", return_value=external), mock.patch.object(local_docker, "run_command") as run:
            result = local_docker.down()
        self.assertEqual(result["code"], 2)
        run.assert_not_called()

    def test_successful_up_saves_selected_url(self):
        running = local_docker._base_status("running", port=8084, managed=True, healthy=True)
        result = {"code": 0, "message": "running", "status": running}
        save_url = mock.Mock()
        with mock.patch.object(local_docker, "up", return_value=result), mock.patch.object(local_docker, "print_result"), redirect_stdout(io.StringIO()):
            code = local_docker.handle_local("up", save_url=save_url)
        self.assertEqual(code, 0)
        save_url.assert_called_once_with("http://localhost:8084")

    def test_local_up_prints_executing_only_for_actual_mutation(self):
        running = local_docker._base_status("running", port=8080, managed=True, healthy=True)
        result = {"code": 0, "message": "Dekart is running at http://localhost:8080.", "status": running}

        def fake_up(**kwargs):
            kwargs["on_execute"]("docker start dekart-local")
            return result

        stdout = io.StringIO()
        with mock.patch.object(local_docker, "up", side_effect=fake_up), redirect_stdout(stdout):
            code = local_docker.handle_local("up")
        self.assertEqual(code, 0)
        self.assertIn("Executing: docker start dekart-local", stdout.getvalue())
        self.assertNotIn("Equivalent Docker command", stdout.getvalue())

    def test_local_up_does_not_offer_stop_for_external_dekart(self):
        external = local_docker._base_status("external", port=8080, managed=False, healthy=True)
        result = {"code": 0, "message": "Using running Dekart at http://localhost:8080.", "status": external}
        stdout = io.StringIO()
        with mock.patch.object(local_docker, "up", return_value=result), redirect_stdout(stdout):
            code = local_docker.handle_local("up")
        self.assertEqual(code, 0)
        self.assertNotIn("dekart local down", stdout.getvalue())


class InitIntegrationTest(unittest.TestCase):
    def test_absent_manual_choice_prints_run_command_and_accepts_remote_url(self):
        stdout = io.StringIO()
        absent = local_docker._base_status("absent", port=8082)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(local_docker, "get_status", return_value=absent))
            stack.enter_context(mock.patch.object(cli, "select_menu_option", return_value=1))
            stack.enter_context(mock.patch("builtins.input", return_value="https://remote.example.com"))
            stack.enter_context(redirect_stdout(stdout))
            selected = cli.prompt_local_dekart_url()
        self.assertEqual(selected, "https://remote.example.com")
        self.assertIn("docker run -p 8082:8080 dekartxyz/dekart", stdout.getvalue())
        self.assertNotIn("dekart-local", stdout.getvalue())
        self.assertIn("Using Dekart endpoint", stdout.getvalue())
        self.assertNotIn("Connected to Dekart", stdout.getvalue())
        self.assertNotIn("Equivalent Docker command", stdout.getvalue())

    def test_start_choice_uses_detected_port_and_prints_management_commands(self):
        stdout = io.StringIO()
        absent = local_docker._base_status("absent", port=8083)
        running = local_docker._base_status("running", port=8083, managed=True, healthy=True)
        result = {"code": 0, "message": "running", "status": running}
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(local_docker, "get_status", return_value=absent))
            menu = stack.enter_context(mock.patch.object(cli, "select_menu_option", return_value=0))
            up = stack.enter_context(mock.patch.object(local_docker, "up", return_value=result))
            stack.enter_context(redirect_stdout(stdout))
            selected = cli.prompt_local_dekart_url()
        self.assertEqual(selected, "http://localhost:8083")
        self.assertEqual(up.call_args.kwargs["preferred_port"], 8083)
        self.assertTrue(up.call_args.kwargs["reuse_external"])
        self.assertIn("Start new local Dekart Docker now on port 8083", menu.call_args.kwargs["options"])
        self.assertIn("dekart local status", stdout.getvalue())
        self.assertIn("dekart local down", stdout.getvalue())
        self.assertIn("dekart local up", stdout.getvalue())

    def test_external_state_offers_connect_without_docker_mutation(self):
        stdout = io.StringIO()
        external = local_docker._base_status("external", port=8080, healthy=True)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(local_docker, "get_status", return_value=external))
            stack.enter_context(mock.patch.object(local_docker, "first_available_port", return_value=8081))
            stack.enter_context(mock.patch.object(cli, "select_menu_option", return_value=1))
            up = stack.enter_context(mock.patch.object(local_docker, "up"))
            stack.enter_context(redirect_stdout(stdout))
            selected = cli.prompt_local_dekart_url()
        self.assertEqual(selected, "http://localhost:8080")
        up.assert_not_called()

    def test_external_state_can_start_new_managed_dekart_on_free_port(self):
        stdout = io.StringIO()
        external = local_docker._base_status("external", port=8080, healthy=True)
        running = local_docker._base_status("running", port=8081, managed=True, healthy=True)
        result = {"code": 0, "message": "running", "status": running}
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(local_docker, "get_status", return_value=external))
            stack.enter_context(mock.patch.object(local_docker, "first_available_port", return_value=8081))
            stack.enter_context(mock.patch.object(cli, "select_menu_option", return_value=0))
            up = stack.enter_context(mock.patch.object(local_docker, "up", return_value=result))
            stack.enter_context(redirect_stdout(stdout))
            selected = cli.prompt_local_dekart_url()
        self.assertEqual(selected, "http://localhost:8081")
        self.assertEqual(up.call_args.kwargs["preferred_port"], 8081)
        self.assertFalse(up.call_args.kwargs["reuse_external"])

    def test_external_manual_command_and_default_url_use_same_port(self):
        stdout = io.StringIO()
        external = local_docker._base_status("external", port=8080, healthy=True)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(local_docker, "get_status", return_value=external))
            stack.enter_context(mock.patch.object(local_docker, "first_available_port", return_value=8081))
            stack.enter_context(mock.patch.object(cli, "select_menu_option", return_value=2))
            stack.enter_context(mock.patch("builtins.input", return_value=""))
            stack.enter_context(mock.patch.object(local_docker, "is_dekart_endpoint", return_value=True))
            stack.enter_context(redirect_stdout(stdout))
            selected = cli.prompt_local_dekart_url()
        self.assertEqual(selected, "http://localhost:8081")
        self.assertIn("docker run -p 8081:8080 dekartxyz/dekart", stdout.getvalue())

    def test_stopped_manual_choice_prints_standalone_docker_run(self):
        stdout = io.StringIO()
        stopped = local_docker._base_status("stopped", port=8080, managed=True)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(local_docker, "get_status", return_value=stopped))
            stack.enter_context(mock.patch.object(cli, "select_menu_option", return_value=1))
            stack.enter_context(mock.patch("builtins.input", return_value=""))
            stack.enter_context(mock.patch.object(local_docker, "is_dekart_endpoint", return_value=False))
            stack.enter_context(redirect_stdout(stdout))
            selected = cli.prompt_local_dekart_url()
        self.assertIsNone(selected)
        self.assertIn("docker run -p 8080:8080 dekartxyz/dekart", stdout.getvalue())
        self.assertNotIn("dekart-local", stdout.getvalue())
        self.assertNotIn("xyz.dekart.cli.managed", stdout.getvalue())

    def test_manual_url_retries_invalid_input(self):
        stdout = io.StringIO()
        absent = local_docker._base_status("absent", port=8080)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(local_docker, "get_status", return_value=absent))
            stack.enter_context(mock.patch.object(cli, "select_menu_option", return_value=1))
            stack.enter_context(mock.patch("builtins.input", side_effect=["bad", "https://remote.example.com"]))
            stack.enter_context(redirect_stdout(stdout))
            selected = cli.prompt_local_dekart_url()
        self.assertEqual(selected, "https://remote.example.com")
        self.assertIn("Invalid URL", stdout.getvalue())

    def test_init_local_path_uses_action_selector(self):
        stdout = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(cli, "get_dekart_url", return_value=cli.DEFAULT_DEKART_URL))
            stack.enter_context(mock.patch.object(cli, "select_menu_option", return_value=0))
            stack.enter_context(mock.patch.object(local_docker, "docker_availability", return_value="running"))
            local_prompt = stack.enter_context(mock.patch.object(cli, "prompt_local_dekart_url", return_value="http://localhost:8084"))
            stack.enter_context(redirect_stdout(stdout))
            selected = cli.prompt_init_dekart_url()
        self.assertEqual(selected, "http://localhost:8084")
        local_prompt.assert_called_once_with(docker_state="running")

    def test_local_manual_url_remains_available_without_docker(self):
        stdout = io.StringIO()
        unavailable = local_docker._base_status("docker_unavailable")
        unavailable["guidance"] = "Install Docker first."
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(cli, "get_dekart_url", return_value=cli.DEFAULT_DEKART_URL))
            stack.enter_context(mock.patch.object(cli, "select_menu_option", side_effect=[0, 0]))
            stack.enter_context(mock.patch.object(local_docker, "docker_availability", return_value="missing"))
            stack.enter_context(mock.patch.object(local_docker, "docker_guidance", return_value="Install Docker first."))
            stack.enter_context(mock.patch.object(local_docker, "get_status", return_value=unavailable))
            stack.enter_context(mock.patch.object(local_docker, "first_available_port", return_value=8080))
            stack.enter_context(mock.patch("builtins.input", return_value="https://remote.example.com"))
            stack.enter_context(redirect_stdout(stdout))
            selected = cli.prompt_init_dekart_url()
        self.assertEqual(selected, "https://remote.example.com")
        self.assertIn("Install Docker first.", stdout.getvalue())
        self.assertIn("docker run -p 8080:8080 dekartxyz/dekart", stdout.getvalue())

    def test_docker_missing_guides_then_returns_to_backend_menu(self):
        stdout = io.StringIO()
        unavailable = local_docker._base_status("docker_unavailable")
        unavailable["guidance"] = "Install Docker first."
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(cli, "get_dekart_url", return_value=cli.DEFAULT_DEKART_URL))
            stack.enter_context(mock.patch.object(cli, "select_menu_option", side_effect=[0, 1, 1]))
            stack.enter_context(mock.patch.object(local_docker, "docker_availability", return_value="missing"))
            stack.enter_context(mock.patch.object(local_docker, "docker_guidance", return_value="Install Docker first."))
            stack.enter_context(mock.patch.object(local_docker, "get_status", return_value=unavailable))
            stack.enter_context(mock.patch.object(local_docker, "first_available_port", return_value=8080))
            stack.enter_context(redirect_stdout(stdout))
            selected = cli.prompt_init_dekart_url()
        self.assertEqual(selected, cli.DEFAULT_DEKART_URL)
        self.assertIn("Install Docker first.", stdout.getvalue())

    def test_healthy_local_selection_is_saved_before_device_authorization(self):
        stdout = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(cli, "is_interactive_terminal", return_value=True))
            stack.enter_context(mock.patch.object(cli, "print_init_banner"))
            stack.enter_context(mock.patch.object(cli, "prompt_init_dekart_url", return_value="http://localhost:8085"))
            save_url = stack.enter_context(mock.patch.object(cli, "save_dekart_url"))
            stack.enter_context(mock.patch.object(cli, "post_json", side_effect=RuntimeError("stop after selection")))
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(io.StringIO()))
            code = cli.handle_init(no_browser=True, local_snapshot_mode="skip")
        self.assertEqual(code, 1)
        save_url.assert_called_once_with("http://localhost:8085")

    def test_menu_interrupt_is_friendly(self):
        stdout = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(cli, "is_interactive_terminal", return_value=True))
            stack.enter_context(mock.patch.object(cli, "print_init_banner"))
            stack.enter_context(mock.patch.object(cli, "prompt_init_dekart_url", side_effect=KeyboardInterrupt()))
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(io.StringIO()))
            code = cli.handle_init(no_browser=True, local_snapshot_mode="skip")
        self.assertEqual(code, 130)


if __name__ == "__main__":
    unittest.main()
