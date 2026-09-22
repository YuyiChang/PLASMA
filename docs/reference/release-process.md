# Cutting a PLASMA release

**Internal maintainer doc.** Not part of the operator playbook.

## What a version tag triggers

Pushing a `vX.Y.Z` tag (matching `plasma.__version__`, enforced by
`publish.yml`'s `check-version` job) fires two workflows:

- `build.yml` — builds `PLASMA_MacOS_arm64` / `PLASMA_Linux_x64` /
  `PLASMA_Linux_arm64` / `PLASMA_Windows_x64.exe`, smoke-tests each, and
  attaches them to the GitHub Release. The macOS job also wraps the binary
  in `PLASMA_MacOS_arm64.app.zip` (`.github/build_macos_app.sh` — the
  Homebrew cask's install target; see the "why not PyInstaller's own
  `BUNDLE()`" comment at the top of that script). Both macOS assets get a
  companion `.sha256` file.
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

`brew bump-cask-pr` downloads the new release's `PLASMA_MacOS_arm64.app.zip`
itself and computes the sha256 — you don't need to copy it from the
`.sha256` file GitHub Actions attaches (that file exists for manual
verification, e.g. by someone auditing the cask before merging).

**Gotcha:** `bump-cask-pr` finds the *old* sha256 by searching the file for
its literal (lowercased) text and replacing it — it only works when the
cask already carries a real, valid sha256. It cannot fill in a placeholder
like `"REPLACE_WITH_SHA256"` (fails with `Could not find 'sha256' stanza
with value ...`, since the placeholder isn't valid lowercase hex to begin
with). Only relevant the very first time the tap gets a real release —
after that there's always a real value to bump from.

This is a deliberate manual step, not wired into CI: it would need a
cross-repo write credential (a PAT stored as a secret in this repo, since the
default `GITHUB_TOKEN` can't push to `homebrew-plasma`) for a single command
that already takes seconds to run by hand. Revisit if releases become
frequent enough for that to be worth the added credential surface.

### `plasma@nightly` — no bump needed, ever

`packaging/homebrew/plasma@nightly.rb` (copy verbatim into the tap's
`Casks/plasma@nightly.rb` once, then leave it alone) tracks the rolling
`nightly` pre-release tag `build.yml`'s `schedule` job republishes daily.
Unlike `plasma.rb`, its `url` points at the fixed `nightly` tag rather than a
version-substituted one, and it uses `version :latest` / `sha256 :no_check`
instead of a pinned checksum — so there is no per-release bump step for this
cask at all. The tradeoff is the one `brew bump-cask-pr` normally buys you:
Homebrew can't tell when a new nightly has landed, so users have to
`brew reinstall --cask plasma@nightly` themselves to pick one up (documented
in the cask's own `caveats`).
