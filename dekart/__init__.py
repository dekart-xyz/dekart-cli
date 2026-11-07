"""Dekart CLI package."""


def __getattr__(name):
    if name == "__version__":
        from importlib import metadata
        try:
            return metadata.version("dekart")
        except metadata.PackageNotFoundError:
            return "unknown"
    raise AttributeError("module 'dekart' has no attribute {0!r}".format(name))
