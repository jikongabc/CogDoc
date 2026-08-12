#!/usr/bin/env python3
"""Fail when package/native/tag versions do not describe one release."""

from __future__ import annotations

import argparse
import ast
import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _python_version() -> str:
    module = ast.parse((ROOT / "src/cogdoc/__init__.py").read_text("utf-8"))
    for node in module.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__version__"
                for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    raise RuntimeError("src/cogdoc/__init__.py does not define __version__")


def versions() -> dict[str, str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    native = tomllib.loads((ROOT / "rust_core/Cargo.toml").read_text("utf-8"))
    return {
        "python-package": str(project["project"]["version"]),
        "python-runtime": _python_version(),
        "rust-package": str(native["package"]["version"]),
    }


def check(*, tag: str | None = None) -> str:
    declared = versions()
    distinct = set(declared.values())
    if len(distinct) != 1:
        detail = ", ".join(f"{name}={value}" for name, value in declared.items())
        raise RuntimeError(f"release versions disagree: {detail}")
    version = distinct.pop()
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[A-Za-z0-9.-]+)?", version):
        raise RuntimeError(f"unsupported release version: {version!r}")
    if tag is not None:
        expected = tag[1:] if tag.startswith("v") else tag
        if expected != version:
            raise RuntimeError(
                f"release tag {tag!r} does not match package version {version!r}"
            )
    return version


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", help="optional v-prefixed release tag")
    args = parser.parse_args()
    version = check(tag=args.tag)
    print(f"release version verified: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
