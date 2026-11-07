"""Build the dependency-free Claude Desktop extension as a reproducible ZIP."""
from pathlib import Path
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED


def build(root):
    """Place the bundle where the MCP server and Python package expect it."""
    root = Path(root)
    output = root / "dekart" / "geosql.mcpb"
    with ZipFile(output, "w") as bundle:
        for name in ("manifest.json", "server.cjs"):
            info = ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            bundle.writestr(info, (root / "desktop-extension" / name).read_bytes())
    return output


if __name__ == "__main__":
    print(build(Path(__file__).resolve().parents[1]))
