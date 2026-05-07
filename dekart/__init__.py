"""Dekart CLI package."""

from importlib import metadata

try:
    __version__ = metadata.version("dekart")
except metadata.PackageNotFoundError:
    __version__ = "unknown"
