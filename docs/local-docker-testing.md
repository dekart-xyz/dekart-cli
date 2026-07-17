# Local Docker manual testing plan

This plan validates the user-visible localhost setup and lifecycle behavior in disposable environments. Run it before releasing changes to `dekart local`.

The running Docker-in-Docker shell is privileged and is suitable only for trusted code. Its containers and volumes disappear when the shell exits; only this mounted repository persists. Host ports `8080-8099` are forwarded into the running environment.

## Environments

```bash
make shell-no-docker-claude
make shell-docker-stopped-claude
make shell-docker-claude
```

In each shell, the current repository is installed editable. Run `python -m unittest discover -s tests -v` first.

## Required scenarios

Record command output and pass/fail for every case in the release handoff or pull request.

1. No Docker command: in `shell-no-docker-claude`, run `dekart local status` and interactively select Local in `dekart init`. Confirm status exits 1, Local says Docker is required, installation guidance is shown, and backend selection resumes.
2. Docker daemon stopped: repeat in `shell-docker-stopped-claude`. Confirm status exits 1, Local says to start Docker, platform guidance is shown, and backend selection resumes.
3. Free default port: in `shell-docker-claude`, run `dekart local status`, then `dekart local up`. Confirm the URL is `http://localhost:8080`.
4. CLI-managed start: run `dekart local down`, select Local in `dekart init`, then choose the displayed start option. Confirm its copy contains the detected port, output says `Executing: docker ...`, management commands are shown, and device authorization begins only after Dekart is healthy.
5. Manual start: remove only the labeled disposable test container with `docker rm -f dekart-local`, select Local, then choose `I will start or connect to Dekart myself`. Confirm it prints the standalone command `docker run -p <detected-port>:8080 dekartxyz/dekart`, with no CLI-managed name, label, or volume. Run it in another terminal, enter its URL, and confirm authorization continues. Confirm no output says `Equivalent Docker command`.
6. External Dekart: remove the managed container, run `docker run -d --name external-dekart -p 127.0.0.1:8080:8080 dekartxyz/dekart:latest`, then select Local in `dekart init`. Confirm the selector offers both a new managed container on 8081 and connection to the running Dekart on 8080. Connect to 8080 and confirm no Docker mutation. Also confirm `dekart local down` refuses without stopping it.
7. Non-Dekart port conflict: occupy 8080 with `docker run -d --name port-busy -p 127.0.0.1:8080:80 nginx:alpine`, then run `dekart local up`. Confirm Dekart uses 8081.
8. Multiple busy ports: also occupy 8081 and 8082 with non-Dekart containers, then run `dekart local up`. Confirm Dekart uses 8083, the first free port.
9. Status formats: run `dekart local`, `dekart local status`, and `dekart local status --json`. Confirm the first two include status, URL, and ownership without claiming a command was executed; confirm JSON stdout parses and contains all documented fields.
10. Persistent data across stop/start: create a marker in the volume (`docker exec dekart-local sh -c 'echo keep >/dekart/data/cli-test-marker'`), run `dekart local down` then `dekart local up`, and confirm the marker remains.
11. Persistent data across port migration: stop Dekart, occupy its old host port, run `dekart local up`, confirm it recreates only the labeled container on the next port, and confirm `/dekart/data/cli-test-marker` remains.
12. Foreign same-name container: create an unlabeled stopped container named `dekart-local`, record `docker inspect`, then run `dekart local up` and `dekart local down`. Confirm both exit 2 and a second inspect is unchanged.
13. Remove cancellation and cleanup: confirm `docker volume inspect dekart-local-data` contains `xyz.dekart.cli.managed=true`, create data in the volume, run `dekart local remove`, answer `n`, and confirm the container and volume remain. Run it again, answer `y`, and confirm output prints both `docker rm -f dekart-local` and `docker volume rm dekart-local-data`, then confirm both the container and volume are gone. Repeat with `dekart local remove --force` and confirm no prompt. Confirm remove refuses an unlabeled same-name container, externally managed Dekart, and an unattached/unlabeled same-name volume.

Clean up named scenario containers between cases. Never delete `dekart-local-data` during persistence cases.

## Release evidence

```text
Date / commit:
Host Docker version:
Unit tests:
1 no Docker command: PASS/FAIL
2 daemon stopped: PASS/FAIL
3 port 8080 free: PASS/FAIL
4 CLI-managed init: PASS/FAIL
5 manual printed command: PASS/FAIL
6 external Dekart: PASS/FAIL
7 8080 non-Dekart conflict: PASS/FAIL
8 multiple busy ports: PASS/FAIL
9 human + JSON status: PASS/FAIL
10 down/up persistence: PASS/FAIL
11 migration persistence: PASS/FAIL
12 unlabeled conflict refusal: PASS/FAIL
Notes:
```
