#!/usr/bin/env python3
"""Bake the current git commit's short SHA into plasma/__init__.py before a
release build is frozen with PyInstaller (see .github/workflows/build.yml).

A frozen bundle ships no .git directory to introspect at runtime, so
plasma.build_info.git_commit_hash() falls back to this baked-in value
instead of resolving one live via `git rev-parse` (which is what a plain
source checkout / dev install does instead).

Usage: python .github/stamp_commit.py <sha>
"""
import pathlib
import re
import sys


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: stamp_commit.py <sha>")
    sha = sys.argv[1][:7]
    path = pathlib.Path(__file__).resolve().parent.parent / "plasma" / "__init__.py"
    text = path.read_text()
    new_text = re.sub(r"__git_commit__ = .*", f"__git_commit__ = {sha!r}", text)
    if new_text == text:
        raise SystemExit(f"__git_commit__ assignment not found in {path}")
    path.write_text(new_text)
    print(f"{path}: stamped __git_commit__ = {sha!r}")


if __name__ == "__main__":
    main()
