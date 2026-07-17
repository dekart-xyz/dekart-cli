"""Manage the optional localhost Dekart Docker container."""

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


CONTAINER_NAME = "dekart-local"
VOLUME_NAME = "dekart-local-data"
IMAGE = "dekartxyz/dekart:latest"
MANAGED_LABEL = "xyz.dekart.cli.managed"
MANAGED_LABEL_VALUE = "true"
PORTS = range(8080, 8100)
STARTUP_TIMEOUT_SECONDS = 60


def local_url(port):
    return "http://localhost:{0}".format(port)


def run_command(command, stream_output=False):
    if stream_output:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        output = []
        try:
            if process.stdout is not None:
                for chunk in iter(process.stdout.readline, ""):
                    output.append(chunk)
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
            code = process.wait()
        except KeyboardInterrupt:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
        return {
            "code": code,
            "stdout": "".join(output),
            "stderr": "",
            "command": " ".join(command),
        }
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    return {
        "code": result.returncode,
        "stdout": result.stdout or "",
        "stderr": result.stderr or "",
        "command": " ".join(command),
    }


def docker_availability():
    if shutil.which("docker") is None:
        return "missing"
    host = current_docker_host()
    if not host:
        return "unknown"
    if host and not is_local_docker_host(host):
        return "remote"
    result = run_command(["docker", "info"])
    if result["code"] != 0:
        return "stopped"
    return "running"


def current_docker_host():
    configured_host = os.environ.get("DOCKER_HOST", "").strip()
    if configured_host:
        return configured_host
    result = run_command(["docker", "context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"])
    if result["code"] != 0:
        return ""
    value = result["stdout"].strip()
    try:
        decoded = json.loads(value)
        return str(decoded or "").strip()
    except (TypeError, ValueError):
        return value.strip('"')


def is_local_docker_host(host):
    normalized = str(host or "").strip().lower()
    return (
        normalized.startswith("unix://")
        or normalized.startswith("npipe://")
        or normalized.startswith("tcp://127.0.0.1:")
        or normalized.startswith("tcp://localhost:")
        or normalized.startswith("tcp://[::1]:")
        or normalized.startswith("http://127.0.0.1:")
        or normalized.startswith("http://localhost:")
    )


def docker_guidance(availability):
    system = platform.system().lower()
    if availability == "remote":
        return "Switch to a local Docker context before running Local Dekart (for example: docker context use default)."
    if availability == "unknown":
        return "Could not verify that Docker uses a local daemon. Check docker context inspect, then select a local Docker context."
    if availability == "missing":
        if system in {"darwin", "windows"}:
            return "Install Docker Desktop, start it, then run dekart init again: https://docs.docker.com/get-docker/"
        return "Install Docker Engine, start it, then run dekart init again: https://docs.docker.com/engine/install/"
    if system == "darwin":
        return "Start Docker Desktop (Applications > Docker), wait until it is ready, then run dekart init again."
    if system == "windows":
        return "Start Docker Desktop, wait until it is ready, then run dekart init again."
    return "Start Docker Engine (usually: sudo systemctl start docker), then run dekart init again."


def _read_url(url, timeout_seconds=1):
    request = urllib.request.Request(url=url, method="GET")
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return response.status, response.read(1024 * 1024).decode("utf-8", errors="replace")


def is_dekart_endpoint(url, timeout_seconds=1):
    """Require both Dekart's health endpoint and a Dekart-marked app root."""
    try:
        health_status, _ = _read_url(url.rstrip("/") + "/health", timeout_seconds=timeout_seconds)
        root_status, root_body = _read_url(url.rstrip("/") + "/", timeout_seconds=timeout_seconds)
    except (OSError, TimeoutError, ValueError, urllib.error.URLError):
        return False
    return health_status == 200 and root_status == 200 and "dekart" in root_body.lower()


def is_port_free(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def first_available_port():
    for port in PORTS:
        if is_port_free(port):
            return port
    return None


def docker_run_command(port):
    return [
        "docker", "run", "-d",
        "--name", CONTAINER_NAME,
        "--label", "{0}={1}".format(MANAGED_LABEL, MANAGED_LABEL_VALUE),
        "--restart", "unless-stopped",
        "-p", "127.0.0.1:{0}:8080".format(port),
        "-v", "{0}:/dekart/data".format(VOLUME_NAME),
        IMAGE,
    ]


def standalone_docker_run_command(port):
    """Return the simple, non-CLI-managed command shown for manual setup."""
    return [
        "docker", "run",
        "-p", "{0}:8080".format(port),
        IMAGE.split(":", 1)[0],
    ]


def format_command(command):
    return " ".join(command)


def inspect_container():
    result = run_command(["docker", "inspect", CONTAINER_NAME])
    if result["code"] != 0:
        return None
    try:
        payload = json.loads(result["stdout"])
        details = payload[0]
    except (KeyError, IndexError, TypeError, ValueError):
        return {"invalid": True, "managed": False, "running": False, "port": None}
    labels = details.get("Config", {}).get("Labels") or {}
    state = details.get("State", {}) or {}
    volumes = [
        mount.get("Name")
        for mount in details.get("Mounts", [])
        if mount.get("Type") == "volume" and mount.get("Name")
    ]
    bindings = (details.get("HostConfig", {}).get("PortBindings") or {}).get("8080/tcp") or []
    port = None
    if bindings:
        try:
            port = int(bindings[0].get("HostPort", ""))
        except (TypeError, ValueError):
            port = None
    return {
        "invalid": False,
        "managed": labels.get(MANAGED_LABEL) == MANAGED_LABEL_VALUE,
        "running": bool(state.get("Running")),
        "port": port,
        "volumes": volumes,
    }


def _is_missing_docker_object(stderr):
    message = str(stderr or "").lower()
    return "no such container" in message or "no such object" in message or "no such volume" in message


def inspect_container_for_removal():
    """Inspect container ownership and mounts without treating Docker errors as absence."""
    result = run_command(["docker", "inspect", CONTAINER_NAME])
    if result["code"] != 0:
        if _is_missing_docker_object(result["stderr"]):
            return {"state": "absent"}
        return {"state": "error", "message": result["stderr"].strip() or "Docker container inspection failed."}
    try:
        details = json.loads(result["stdout"])[0]
        labels = details.get("Config", {}).get("Labels") or {}
        volumes = [
            mount.get("Name")
            for mount in details.get("Mounts", [])
            if mount.get("Type") == "volume" and mount.get("Name")
        ]
    except (KeyError, IndexError, TypeError, ValueError):
        return {"state": "error", "message": "Docker returned invalid container inspection data."}
    return {
        "state": "present",
        "managed": labels.get(MANAGED_LABEL) == MANAGED_LABEL_VALUE,
        "volumes": volumes,
    }


def inspect_volume_for_removal():
    """Inspect volume labels without treating Docker errors as absence."""
    result = run_command(["docker", "volume", "inspect", VOLUME_NAME])
    if result["code"] != 0:
        if _is_missing_docker_object(result["stderr"]):
            return {"state": "absent"}
        return {"state": "error", "message": result["stderr"].strip() or "Docker volume inspection failed."}
    try:
        details = json.loads(result["stdout"])[0]
        labels = details.get("Labels") or {}
    except (KeyError, IndexError, TypeError, ValueError):
        return {"state": "error", "message": "Docker returned invalid volume inspection data."}
    return {
        "state": "present",
        "managed": labels.get(MANAGED_LABEL) == MANAGED_LABEL_VALUE,
    }


def prepare_data_volume(allow_legacy=False, on_execute=None):
    """Create a labeled data volume or verify an existing volume is safe to use."""
    volume = inspect_volume_for_removal()
    if volume["state"] == "error":
        return {"code": 1, "message": "Could not inspect Dekart data volume: {0}".format(volume["message"])}
    if volume["state"] == "present":
        if volume.get("managed") or allow_legacy:
            return {"code": 0}
        return {
            "code": 2,
            "message": "The {0} volume is not managed by this CLI; refusing to use it.".format(VOLUME_NAME),
        }
    command = [
        "docker", "volume", "create",
        "--label", "{0}={1}".format(MANAGED_LABEL, MANAGED_LABEL_VALUE),
        VOLUME_NAME,
    ]
    result = _run_mutation(command, on_execute=on_execute)
    if result["code"] != 0:
        return {"code": 1, "message": "Docker could not create Dekart data volume: {0}".format(result["stderr"].strip())}
    verified = inspect_volume_for_removal()
    if verified["state"] == "error":
        return {"code": 1, "message": "Could not verify the created Dekart data volume: {0}".format(verified["message"])}
    if verified["state"] != "present" or not verified.get("managed"):
        return {
            "code": 2,
            "message": "Docker did not create a verifiably CLI-managed {0} volume; refusing to use it.".format(VOLUME_NAME),
        }
    return {"code": 0}


def wait_for_dekart(url, timeout_seconds=STARTUP_TIMEOUT_SECONDS):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if is_dekart_endpoint(url):
            return True
        time.sleep(1)
    return False


def _base_status(status, port=None, managed=False, healthy=False, command=None):
    return {
        "status": status,
        "url": local_url(port) if port else None,
        "port": port,
        "managed": managed,
        "container_name": CONTAINER_NAME,
        "volume_name": VOLUME_NAME,
        "image": IMAGE,
        "healthy": healthy,
        "docker_command": format_command(command or docker_run_command(port or 8080)),
    }


def get_status():
    availability = docker_availability()
    if availability != "running":
        if is_dekart_endpoint(local_url(8080)):
            return _base_status("external", port=8080, managed=False, healthy=True)
        status = _base_status("docker_unavailable")
        status["docker_availability"] = availability
        status["guidance"] = docker_guidance(availability)
        return status

    container = inspect_container()
    if container is not None:
        if not container.get("managed"):
            status = _base_status("ownership_conflict", port=container.get("port"), managed=False)
            status["message"] = "Container dekart-local exists without the Dekart CLI management label."
            return status
        port = container.get("port")
        if not container.get("running"):
            return _base_status("stopped", port=port, managed=True, command=["docker", "start", CONTAINER_NAME])
        healthy = bool(port and is_dekart_endpoint(local_url(port)))
        return _base_status("running" if healthy else "unhealthy", port=port, managed=True, healthy=healthy)

    for port in PORTS:
        if not is_port_free(port) and is_dekart_endpoint(local_url(port), timeout_seconds=0.25):
            return _base_status("external", port=port, managed=False, healthy=True)
    return _base_status("absent", port=first_available_port())


def _failure_with_logs(message, port=None):
    logs_command = ["docker", "logs", CONTAINER_NAME]
    logs = run_command(logs_command)
    return {
        "code": 1,
        "message": message,
        "status": get_status(),
        "logs": (logs["stdout"] + logs["stderr"]).strip(),
        "recovery_commands": [
            format_command(logs_command),
            format_command(["docker", "stop", CONTAINER_NAME]),
            format_command(["docker", "start", CONTAINER_NAME]),
        ],
        "port": port,
    }


def _run_mutation(command, on_execute=None, stream_output=False):
    if on_execute:
        on_execute(format_command(command))
    if stream_output:
        return run_command(command, stream_output=True)
    return run_command(command)


def up(wait_timeout=STARTUP_TIMEOUT_SECONDS, preferred_port=None, reuse_external=True, on_execute=None):
    status = get_status()
    if status["status"] == "docker_unavailable":
        return {"code": 1, "message": status["guidance"], "status": status}
    if status["status"] == "ownership_conflict":
        return {"code": 2, "message": status["message"] + " Refusing to modify it.", "status": status}
    if status["status"] == "external":
        if reuse_external:
            return {"code": 0, "message": "Using running Dekart at {0}; the CLI will not manage it.".format(status["url"]), "status": status}
        port = preferred_port or first_available_port()
        if port is None:
            return {"code": 1, "message": "No free local port found in 8080-8099.", "status": status}
        status = _base_status("absent", port=port)
    if status["status"] == "running":
        return {"code": 0, "message": "Dekart is already running at {0}.".format(status["url"]), "status": status}
    if status["status"] == "unhealthy":
        url = status.get("url")
        if url and wait_for_dekart(url, timeout_seconds=wait_timeout):
            return {"code": 0, "message": "Dekart is running at {0}.".format(url), "status": get_status(), "command": format_command(["docker", "start", CONTAINER_NAME])}
        return _failure_with_logs("The running Dekart container did not become healthy within {0} seconds.".format(wait_timeout), port=status.get("port"))

    container = inspect_container()
    if container and container.get("managed"):
        port = container.get("port")
        if port and is_port_free(port):
            command = ["docker", "start", CONTAINER_NAME]
            result = _run_mutation(command, on_execute=on_execute)
        else:
            port = first_available_port()
            if port is None:
                return {"code": 1, "message": "No free local port found in 8080-8099.", "status": status}
            prepared = prepare_data_volume(
                allow_legacy=VOLUME_NAME in container.get("volumes", []),
                on_execute=on_execute,
            )
            if prepared["code"] != 0:
                return {"code": prepared["code"], "message": prepared["message"], "status": status}
            remove = _run_mutation(["docker", "rm", CONTAINER_NAME], on_execute=on_execute)
            if remove["code"] != 0:
                return {"code": 1, "message": "Could not recreate managed container: {0}".format(remove["stderr"].strip()), "status": status}
            command = docker_run_command(port)
            result = _run_mutation(command, on_execute=on_execute, stream_output=True)
    else:
        port = status.get("port") or first_available_port()
        if port is None:
            return {"code": 1, "message": "No free local port found in 8080-8099.", "status": status}
        prepared = prepare_data_volume(on_execute=on_execute)
        if prepared["code"] != 0:
            return {"code": prepared["code"], "message": prepared["message"], "status": status}
        command = docker_run_command(port)
        result = _run_mutation(command, on_execute=on_execute, stream_output=True)

    if result["code"] != 0:
        detail = (result["stderr"] or result["stdout"]).strip()
        return _failure_with_logs("Docker could not start Dekart: {0}".format(detail), port=port)
    url = local_url(port)
    if not wait_for_dekart(url, timeout_seconds=wait_timeout):
        return _failure_with_logs("Dekart did not become healthy within {0} seconds.".format(wait_timeout), port=port)
    return {"code": 0, "message": "Dekart is running at {0}.".format(url), "status": get_status(), "command": format_command(command)}


def down(on_execute=None):
    status = get_status()
    command = ["docker", "stop", CONTAINER_NAME]
    if status["status"] == "docker_unavailable":
        return {"code": 1, "message": status["guidance"], "status": status, "command": format_command(command)}
    if status["status"] in {"ownership_conflict", "external"}:
        return {"code": 2, "message": "Dekart is not CLI-managed; refusing to stop it.", "status": status, "command": format_command(command)}
    if status["status"] == "absent":
        return {"code": 1, "message": "No CLI-managed local Dekart container exists.", "status": status, "command": format_command(command)}
    if status["status"] == "stopped":
        return {"code": 0, "message": "Dekart is already stopped. Data remains in {0}.".format(VOLUME_NAME), "status": status, "command": format_command(command)}
    result = _run_mutation(command, on_execute=on_execute)
    if result["code"] != 0:
        return {"code": 1, "message": "Docker could not stop Dekart: {0}".format(result["stderr"].strip()), "status": status, "command": format_command(command)}
    return {"code": 0, "message": "Dekart stopped. Data remains in {0}.".format(VOLUME_NAME), "status": get_status(), "command": format_command(command)}


def remove(on_execute=None):
    """Remove the CLI-managed container and permanently delete its data volume."""
    status = get_status()
    if status["status"] == "docker_unavailable":
        return {"code": 1, "message": status["guidance"], "status": status}
    if status["status"] in {"ownership_conflict", "external"}:
        return {
            "code": 2,
            "message": "Dekart is not CLI-managed; refusing to remove its container or data.",
            "status": status,
        }

    container = inspect_container_for_removal()
    if container["state"] == "error":
        return {"code": 1, "message": "Could not verify container ownership: {0}".format(container["message"]), "status": status}
    if container["state"] == "present" and not container.get("managed"):
        return {"code": 2, "message": "The dekart-local container is not managed by this CLI; refusing to remove it or any data.", "status": status}

    volume = inspect_volume_for_removal()
    if volume["state"] == "error":
        return {"code": 1, "message": "Could not verify data volume ownership: {0}".format(volume["message"]), "status": status}
    has_container = container["state"] == "present"
    has_volume = volume["state"] == "present"
    volume_is_owned = volume.get("managed") or (has_container and VOLUME_NAME in container.get("volumes", []))
    if has_volume and not volume_is_owned:
        return {
            "code": 2,
            "message": "The {0} volume cannot be proven to belong to this CLI; refusing to delete it.".format(VOLUME_NAME),
            "status": status,
        }
    if not has_container and not has_volume:
        return {"code": 0, "message": "No CLI-managed local Dekart container or data exists.", "status": status}

    removed_container = False
    if has_container:
        result = _run_mutation(["docker", "rm", "-f", CONTAINER_NAME], on_execute=on_execute)
        if result["code"] != 0:
            return {
                "code": 1,
                "message": "Docker could not remove Dekart: {0}".format(result["stderr"].strip()),
                "status": status,
            }
        removed_container = True

    if has_volume:
        result = _run_mutation(["docker", "volume", "rm", VOLUME_NAME], on_execute=on_execute)
        if result["code"] != 0:
            return {
                "code": 1,
                "message": "{0}Docker could not delete Dekart data: {1}".format(
                    "Dekart container was removed, but " if removed_container else "",
                    result["stderr"].strip(),
                ),
                "status": get_status(),
            }

    return {
        "code": 0,
        "message": "Local Dekart and all data in {0} were permanently deleted.".format(VOLUME_NAME),
        "status": get_status(),
    }


def print_result(result):
    print(result.get("message", ""))
    logs = result.get("logs")
    if logs:
        print("Container logs:")
        print(logs)
    for command in result.get("recovery_commands", []):
        print("  {0}".format(command))


def print_status(status, raw_json=False):
    if raw_json:
        print(json.dumps(status, indent=2, sort_keys=True))
        return
    print("Status: {0}".format(status["status"]))
    if status.get("url"):
        print("URL: {0}".format(status["url"]))
    if status.get("guidance"):
        print(status["guidance"])
    if status.get("message"):
        print(status["message"])
    print("Managed by Dekart CLI: {0}".format("yes" if status.get("managed") else "no"))


def print_executing(command):
    print("Executing: {0}".format(command))


def print_management_commands():
    print()
    print("Manage local Dekart:")
    print("  dekart local status    show status and URL")
    print("  dekart local down      stop it (data stays local)")
    print("  dekart local up        start it again")
    print("  dekart local remove    remove it and permanently delete data")


def handle_local(action, raw_json=False, save_url=None, force=False):
    if action == "status":
        status = get_status()
        print_status(status, raw_json=raw_json)
        return 0 if status["status"] in {"running", "external"} else (2 if status["status"] == "ownership_conflict" else 1)
    if action == "remove":
        if not force:
            try:
                confirmed = input(
                    "Remove local Dekart and permanently delete all data in {0}? [y/N]: ".format(VOLUME_NAME)
                ).strip().lower()
            except EOFError:
                confirmed = ""
            if confirmed not in {"y", "yes"}:
                print("Remove cancelled. No container or data was deleted.")
                return 0
        result = remove(on_execute=print_executing)
    else:
        result = up(on_execute=print_executing) if action == "up" else down(on_execute=print_executing)
    status = result.get("status") or {}
    if action == "up" and result["code"] == 0 and status.get("url") and save_url:
        save_url(status["url"])
    print_result(result)
    if action == "up" and result["code"] == 0 and status.get("managed"):
        print_management_commands()
    return result["code"]
