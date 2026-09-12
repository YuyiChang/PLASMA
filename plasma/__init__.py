__version__ = "2.1.3"

# Short git commit hash baked in by CI (.github/workflows/build.yml) just
# before a release build is frozen with PyInstaller — a frozen bundle ships no
# .git directory to introspect at runtime. None from a source checkout;
# plasma.build_info.git_commit_hash() resolves that case live from git.
__git_commit__ = None
