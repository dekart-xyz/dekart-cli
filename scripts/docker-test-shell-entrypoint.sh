#!/usr/bin/env bash
set -euo pipefail

mode="${DEKART_TEST_DOCKER_MODE:-running}"
if [ "$mode" = "missing" ]; then
    mv /usr/bin/docker /usr/bin/docker.hidden
elif [ "$mode" = "running" ]; then
    dockerd > /tmp/dockerd.log 2>&1 &
    for _attempt in $(seq 1 60); do
        docker info >/dev/null 2>&1 && break
        sleep 0.5
    done
    if ! docker info >/dev/null 2>&1; then
        cat /tmp/dockerd.log >&2
        exit 1
    fi
fi

host_uid="${DEKART_TEST_HOST_UID:-0}"
host_gid="${DEKART_TEST_HOST_GID:-0}"
group_name="$(getent group "$host_gid" | cut -d: -f1 || true)"
if [ -z "$group_name" ]; then
    groupadd --gid "$host_gid" dekart-test
    group_name=dekart-test
fi
user_name="$(getent passwd "$host_uid" | cut -d: -f1 || true)"
if [ -z "$user_name" ]; then
    useradd --create-home --uid "$host_uid" --gid "$group_name" dekart-test
    user_name=dekart-test
fi
user_home="$(getent passwd "$user_name" | cut -d: -f6)"
usermod --gid "$group_name" "$user_name"
if getent group docker >/dev/null; then
    usermod --append --groups docker "$user_name"
fi
chown -R "$host_uid:$host_gid" /opt/venv "$user_home"

/opt/venv/bin/python -m pip install --quiet --editable /workspace

echo
echo "Disposable Dekart CLI shell ready."
echo "  Mode: $mode"
echo "  Source: /workspace (mounted read/write)"
echo "  User: $user_name ($host_uid:$host_gid)"
echo "  Python: $(python3 --version)"
echo "  Claude: $(claude --version)"
if command -v docker >/dev/null 2>&1; then
    if docker info >/dev/null 2>&1; then
        echo "  Docker: running"
    else
        echo "  Docker: installed, daemon stopped"
    fi
else
    echo "  Docker: not installed"
fi
echo
echo "Testing plan: docs/local-docker-testing.md"
echo "Start with: dekart local status"
echo

exec gosu "$user_name" env HOME="$user_home" XDG_CONFIG_HOME="$user_home/.config" PATH="/opt/venv/bin:$PATH" bash --rcfile /etc/dekart-test-shell.bashrc
