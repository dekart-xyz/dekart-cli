PYTHON ?= python3
TWINE ?= $(PYTHON) -m twine
BUILD ?= $(PYTHON) -m build
PIP ?= $(PYTHON) -m pip

.PHONY: help deps clean build check publish-test publish release-test release

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
