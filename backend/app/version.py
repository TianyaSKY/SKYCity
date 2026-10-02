"""Read the backend's release version from its packaged project metadata."""
from pathlib import Path
import tomllib

APP_VERSION = tomllib.loads(
    (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
)["project"]["version"]
