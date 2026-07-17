PYTHON ?= python3
TWINE ?= $(PYTHON) -m twine
BUILD ?= $(PYTHON) -m build
PIP ?= $(PYTHON) -m pip

.PHONY: help deps clean build check publish-test publish release-test release shell-no-docker-claude shell-docker-stopped-claude shell-docker-claude _shell-local-docker

DEKART_CLI_TEST_IMAGE ?= dekart-cli-test-shell:local

help:
	@echo "Targets:"
	@echo "  deps          Install packaging tools (build, twine)"
	@echo "  clean         Remove build artifacts"
	@echo "  build         Build sdist + wheel into dist/"
	@echo "  check         Run twine checks for dist artifacts"
	@echo "  publish-test  Upload dist artifacts to TestPyPI"
	@echo "  publish       Upload dist artifacts to PyPI"
	@echo "  release-test  Run deps + build + check + publish-test"
	@echo "  release       Run deps + build + check + publish"
	@echo "  shell-no-docker-claude       Disposable shell without the Docker command"
	@echo "  shell-docker-stopped-claude  Disposable shell with Docker daemon stopped"
	@echo "  shell-docker-claude          Privileged Docker-in-Docker shell, ports 8080-8099"

deps:
	$(PIP) install --upgrade build twine

clean:
	rm -rf build dist *.egg-info

build: clean
	$(BUILD)

check:
	$(TWINE) check dist/*

publish-test:
	$(TWINE) upload --repository testpypi dist/*

publish:
	$(TWINE) upload dist/*

release-test: deps build check publish-test

release: deps build check publish

shell-no-docker-claude:
	@$(MAKE) _shell-local-docker DOCKER_MODE=missing PRIVILEGED= PUBLISH=

shell-docker-stopped-claude:
	@$(MAKE) _shell-local-docker DOCKER_MODE=stopped PRIVILEGED= PUBLISH=

shell-docker-claude:
	@$(MAKE) _shell-local-docker DOCKER_MODE=running PRIVILEGED=--privileged PUBLISH='--publish 127.0.0.1:8080-8099:8080-8099'

_shell-local-docker:
	@set -eu; \
	command -v docker >/dev/null 2>&1 || { echo "Need 'docker' in PATH." >&2; exit 1; }; \
	docker info >/dev/null 2>&1 || { echo "Docker is not running." >&2; exit 1; }; \
	docker buildx version >/dev/null 2>&1 || { echo "Need Docker Buildx for this target." >&2; exit 1; }; \
	echo "Building disposable Dekart CLI test image $(DEKART_CLI_TEST_IMAGE)..."; \
	docker buildx build --load --file Dockerfile.test-shell --tag "$(DEKART_CLI_TEST_IMAGE)" .; \
	if [ "$(DOCKER_MODE)" = "running" ]; then echo "WARNING: this trusted-code test shell uses --privileged for Docker-in-Docker."; fi; \
	docker run --rm -it \
		$(PRIVILEGED) \
		$(PUBLISH) \
		--env ANTHROPIC_API_KEY \
		--env CLAUDE_CODE_OAUTH_TOKEN \
		--env DEKART_TEST_DOCKER_MODE="$(DOCKER_MODE)" \
		--env DEKART_TEST_HOST_UID="$$(id -u)" \
		--env DEKART_TEST_HOST_GID="$$(id -g)" \
		--mount type=bind,source="$(CURDIR)",target=/workspace \
		--workdir /workspace \
		"$(DEKART_CLI_TEST_IMAGE)" \
		/usr/local/bin/dekart-test-shell
