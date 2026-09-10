"""检查稳定版本格式及发布标签的一致性。"""

import os
import re
from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parents[1] / "VERSION"
STABLE_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")


def check_version(ref: str = "") -> str:
    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not STABLE_VERSION.fullmatch(version):
        raise ValueError("VERSION 必须是稳定版本号，例如 1.0.0")
    if ref.startswith("refs/tags/") and ref != f"refs/tags/v{version}":
        raise ValueError("发布标签必须与 VERSION 一致，例如 v1.0.0")
    return version


if __name__ == "__main__":
    version = check_version(os.environ.get("GITHUB_REF", ""))
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"version={version}\n")
    print(version)
