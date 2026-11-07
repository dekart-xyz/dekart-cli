"""Include the desktop extension in wheels built from a checkout or an sdist."""
from hatchling.builders.hooks.plugin.interface import BuildHookInterface
from pathlib import Path
from runpy import run_path


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        builder = run_path(str(Path(self.root) / "scripts" / "build_mcpb.py"))
        builder["build"](self.root)
        build_data["artifacts"] = ["dekart/geosql.mcpb"]
