"""Export compatibility dependency files from the locked uv project."""
from pathlib import Path
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def main():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    if project["version"] != (ROOT / "VERSION").read_text().strip():
        raise SystemExit("pyproject.toml version must match VERSION")
    subprocess.run(["uv", "export", "--locked", "--no-dev", "--no-emit-project",
                    "--format", "requirements-txt", "--output-file", "requirements.txt"],
                   cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    (ROOT / "requirements.in").write_text(
        "# Generated from pyproject.toml by scripts/export_requirements.py; do not edit.\n"
        + "\n".join(project["dependencies"]) + "\n")


if __name__ == "__main__":
    main()
