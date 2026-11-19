"""Fail before building/publishing a version tag that disagrees with the package."""
import ast
import os
from pathlib import Path
import sys


def check(ref, root):
    if not ref.startswith("refs/tags/v"):
        print("No version release tag; skipping version comparison.")
        return 0
    tag = ref.removeprefix("refs/tags/")
    tree = ast.parse((root / "plasma/__init__.py").read_text(encoding="utf-8"))
    version = next(ast.literal_eval(node.value) for node in tree.body
                   if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "__version__"
                           for target in node.targets))
    print(f"tag={tag} package={version}")
    if tag != f"v{version}":
        print(f"::error::Tag {tag} does not match plasma.__version__ ({version}). "
              "Tag the commit containing the version bump.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(check(os.environ.get("GITHUB_REF", ""), Path(__file__).resolve().parents[1]))
