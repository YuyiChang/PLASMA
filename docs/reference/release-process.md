# Cutting a PLASMA release

**Internal maintainer doc.** Not part of the operator playbook.

## What a version tag triggers

Pushing a `vX.Y.Z` tag (matching `plasma.__version__`, enforced by
`publish.yml`'s `check-version` job) fires two workflows:

- `build.yml` — builds `PLASMA_MacOS_arm64` / `PLASMA_Linux_x64` /
  `PLASMA_Linux_arm64` / `PLASMA_Windows_x64.exe`, smoke-tests each, and
  attaches them (plus `PLASMA_MacOS_arm64.sha256`) to the GitHub Release.
- `publish.yml` — builds the sdist/wheel and publishes `plasma-app` to PyPI.

Both are source-of-truth downstream of the tag; nothing else needs to run to
make a release "live" on PyPI or GitHub Releases.

## Homebrew tap (yuyichang/homebrew-plasma)

Separate repo, **not automated** — bump it by hand after the release build
finishes:

```bash
brew bump-cask-pr --version <new-version> --write-only yuyichang/plasma/plasma
git -C "$(brew --repo yuyichang/plasma)" diff   # review, then commit + push
```

`brew bump-cask-pr` downloads the new release's `PLASMA_MacOS_arm64` itself
and computes the sha256 — you don't need to copy it from the `.sha256` file
GitHub Actions attaches (that file exists for manual verification, e.g. by
someone auditing the cask before merging).

This is a deliberate manual step, not wired into CI: it would need a
cross-repo write credential (a PAT stored as a secret in this repo, since the
default `GITHUB_TOKEN` can't push to `homebrew-plasma`) for a single command
that already takes seconds to run by hand. Revisit if releases become
frequent enough for that to be worth the added credential surface.
