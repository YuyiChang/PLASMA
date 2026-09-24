# Cutting a PLASMA release

**Internal maintainer doc.** Not part of the operator playbook.

## What a version tag triggers

Pushing a `vX.Y.Z` tag (matching `plasma.__version__`, enforced by
`publish.yml`'s `check-version` job) fires two workflows:

- `build.yml` — builds all four platforms in PyInstaller **onedir** mode (a
  folder — the exe plus a support-files directory — not a single
  self-extracting file; switched from onefile to cut launch time, since
  onefile re-extracts the whole bundle to a temp dir on every run),
  smoke-tests each, and attaches release assets:
    - macOS: `PLASMA_MacOS_arm64.zip` (raw onedir folder), `.app.zip`
      (`.github/build_macos_app.sh` — the Homebrew cask's install target;
      see the "why not PyInstaller's own `BUNDLE()`" comment at the top of
      that script), and `.dmg` (`.github/build_macos_dmg.sh`, drag-to-
      Applications). All three get a companion `.sha256` file. The `.app`
      gets codesigned + notarized + stapled by
      `.github/codesign_notarize_macos.sh` between those two steps — unless
      the signing secrets aren't configured, in which case it's a no-op and
      the build stays unsigned (see "Codesigning & notarization" below).
      The raw `.zip` is never signed either way (only the `.app` inside the
      `.app.zip`/`.dmg` is).
    - Windows: `PLASMA_Windows_x64.zip` (raw onedir folder) and
      `PLASMA_Windows_x64_Setup.exe` (Inno Setup,
      `packaging/windows/plasma_installer.iss` — unsigned, so SmartScreen
      still warns on first run).
    - Linux (x64 + arm64): `PLASMA_Linux_<arch>.tar.gz` (raw onedir folder
      only — no installer for Linux by design).
- `publish.yml` — builds the sdist/wheel and publishes `plasma-app` to PyPI.

Both are source-of-truth downstream of the tag; nothing else needs to run to
make a release "live" on PyPI or GitHub Releases.

## Codesigning & notarization (macOS)

`build-macos`'s "Codesign + notarize .app" step (`.github/codesign_notarize_macos.sh`)
signs `dist/PLASMA.app` with a "Developer ID Application" certificate, submits
it to Apple's notary service, and staples the resulting ticket — but only if
five repo secrets are set. **Until they are, this step no-ops and every macOS
asset ships unsigned/unnotarized exactly as before** (first launch blocked by
Gatekeeper, right-click → Open) — nothing else needs to change to turn this
on later.

**One-time setup, on the Apple side (requires an Apple Developer Program
membership, $99/yr):**

1. Create a **Developer ID Application** certificate: Xcode → Settings →
   Accounts → Manage Certificates → **+** → *Developer ID Application* (or
   via the [Certificates page](https://developer.apple.com/account/resources/certificates)
   using a CSR generated in Keychain Access). This is the certificate for
   distributing outside the App Store — not "Apple Development" or "Apple
   Distribution".
2. In Keychain Access, find that certificate, expand it, select **both** the
   certificate and its private key, right-click → **Export 2 items…** → save
   as a `.p12`, set an export password.
3. Base64-encode it: `base64 -i DeveloperIDApplication.p12 | pbcopy` (macOS
   `base64` auto-wraps; that's fine, `base64 --decode` in the script handles
   it either way).
4. In [App Store Connect](https://appstoreconnect.apple.com) → Users and
   Access → Integrations → **App Store Connect API** → generate a key with
   the **Developer** role (notarization doesn't need Admin). Download the
   `.p8` **immediately** — it's only downloadable once. Note its Key ID and
   Issuer ID (shown on that same page).
5. Base64-encode the `.p8` the same way as step 3.

**Set these as GitHub Actions repo secrets** (Settings → Secrets and
variables → Actions → New repository secret, or via `gh secret set <name>`
run from your own machine — never paste secret material into a chat or
commit):

| Secret | Value |
|---|---|
| `MACOS_CERTIFICATE_P12_BASE64` | output of step 3 |
| `MACOS_CERTIFICATE_PASSWORD` | the export password from step 2 |
| `MACOS_NOTARY_KEY_ID` | Key ID from step 4 |
| `MACOS_NOTARY_ISSUER_ID` | Issuer ID from step 4 |
| `MACOS_NOTARY_KEY_P8_BASE64` | output of step 5 |

```bash
gh secret set MACOS_CERTIFICATE_P12_BASE64 < cert_base64.txt --repo YuyiChang/PLASMA
gh secret set MACOS_CERTIFICATE_PASSWORD --repo YuyiChang/PLASMA   # prompts, hidden input
gh secret set MACOS_NOTARY_KEY_ID --repo YuyiChang/PLASMA
gh secret set MACOS_NOTARY_ISSUER_ID --repo YuyiChang/PLASMA
gh secret set MACOS_NOTARY_KEY_P8_BASE64 < notary_key_base64.txt --repo YuyiChang/PLASMA
```

No `MACOS_SIGN_IDENTITY` secret is needed — the script imports the
certificate into a throwaway keychain and reads the identity string
(`security find-identity`) straight out of it. No keychain-password secret
either — that keychain is created fresh and deleted at the end of every job
run, so its unlock password is just generated inline (`openssl rand`) and
never needs to be stored.

`.github/macos_entitlements.plist` grants the Hardened Runtime exceptions a
PyInstaller onedir bundle needs to actually *run* once signed (library
validation would otherwise block loading bundled numpy/scipy/liblsl, since
they're not signed by this project's Team ID) — see the comments in that
file before changing it.

**Once this is live**, two things become worth revisiting (not done as part
of turning signing on): the Homebrew cask's `caveats` text ("PLASMA is not
code-signed or notarized...") and its quarantine-clearing `postflight_steps`
both go stale for signed releases — harmless to leave (clearing quarantine
on an already-legitimate app is a no-op, not a problem), but worth cleaning
up once a signed release has actually shipped.

## Codesigning (Windows, self-signed)

`build-windows`'s "Codesign raw exe" / "Codesign installer" steps
(`.github/codesign_windows.ps1`) sign `PLASMA_Windows_x64.exe` and
`PLASMA_Windows_x64_Setup.exe` with `signtool` — but only if two repo
secrets are set. Until they are, this no-ops and every Windows asset ships
unsigned exactly as before.

**Important — set expectations before setting this up:** unlike the macOS
notarization above, there is no free path to a CA-issued Windows
certificate, and **a self-signed certificate does not stop SmartScreen's
"Windows protected your PC" warning** — SmartScreen trusts a signature via
Microsoft's reputation system, which only recognizes certificates from a
handful of trusted CAs (building reputation over many downloads for a
standard OV cert, or instantly for a paid EV cert). A self-signed cert is
invisible to that system entirely. What it *does* buy: tamper-evidence, a
consistent signer identity across releases (so an update can be verified as
"from the same publisher" even though that publisher is unverified), and
the exact signing plumbing a real purchased cert would slot into later —
just swap the two secrets below, no pipeline changes.

**One-time setup** — generate a self-signed code-signing certificate
(anywhere with `openssl`, e.g. this same macOS machine; it doesn't need to
be done on Windows):

```bash
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 3650 -nodes \
  -subj "/CN=PLASMA/O=YuyiChang" -addext "extendedKeyUsage=codeSigning"
openssl pkcs12 -export -out plasma_selfsigned.pfx -inkey key.pem -in cert.pem \
  -passout pass:<choose a password> -legacy   # -legacy: signtool needs RC2/3DES-era PKCS12, not OpenSSL 3's new default
base64 -i plasma_selfsigned.pfx -o pfx_base64.txt
rm key.pem cert.pem   # don't leave the private key sitting on disk afterward
```

**Set these as GitHub Actions repo secrets** (never paste secret material
into a chat or commit):

```bash
gh secret set WINDOWS_CERTIFICATE_PFX_BASE64 < pfx_base64.txt --repo YuyiChang/PLASMA
gh secret set WINDOWS_CERTIFICATE_PASSWORD --repo YuyiChang/PLASMA   # prompts, hidden input
rm pfx_base64.txt plasma_selfsigned.pfx
```

No verification step runs after signing (unlike the macOS script's
`codesign --verify`) — `signtool verify /pa` checks the signature chains to
a trusted root, which a self-signed cert never does by design, so that
check would always "fail" even on a correct signature. The `signtool sign`
call's own exit code is the real check.

## Homebrew tap (yuyichang/homebrew-plasma)

Separate repo. **Self-automated** via
`.github/workflows/bump-plasma.yml` *in that repo* (not this one): a daily
scheduled job (plus `workflow_dispatch` for an immediate manual run right
after cutting a release) checks PLASMA's `/releases/latest` — which already
excludes prereleases, so it naturally skips the rolling `nightly` tag — and,
if the version differs from what `Casks/plasma.rb` currently has, downloads
that release's `PLASMA_MacOS_arm64.app.zip`, computes its sha256, and opens
a PR bumping both. Merge (or close) that PR by hand; nothing pushes to
`main` automatically.

This intentionally runs as a job *inside* `homebrew-plasma`, using that
repo's own default `GITHUB_TOKEN` (scoped to itself, via `permissions:
contents: write` / `pull-requests: write`) — not a job in *this* repo's CI
reaching across to bump the tap. The latter would need a cross-repo PAT
stored as a secret here with write access to a different public repo, a
meaningfully bigger credential blast radius for no real benefit; running the
poll the other direction avoids needing any extra credential at all.

If you ever need to bump it by hand instead (e.g. the scheduled job is
broken, or you don't want to wait for it):

```bash
brew bump-cask-pr --version <new-version> --write-only yuyichang/plasma/plasma
git -C "$(brew --repo yuyichang/plasma)" diff   # review, then commit + push
```

`brew bump-cask-pr` downloads the release's `PLASMA_MacOS_arm64.app.zip`
itself and computes the sha256 — you don't need to copy it from the
`.sha256` file GitHub Actions attaches (that file exists for manual
verification, e.g. by someone auditing the cask before merging). **Gotcha:**
it finds the *old* sha256 by searching for its literal (lowercased) text and
replacing it — only works once the cask already carries a real, valid
sha256; it can't fill in a non-hex placeholder like `"REPLACE_WITH_SHA256"`.
Only relevant the very first time the tap gets a real release.

**One-time note, already resolved:** the first onedir-based release (v2.2.3)
needed a manual fix to the cask's `binary` stanza — changed from
`.../Contents/Resources/plasma-bin` to
`.../Contents/Resources/PLASMA_MacOS_arm64/PLASMA_MacOS_arm64` (the onedir
folder is copied into `Contents/Resources/` keeping its own name now,
instead of being flattened to a single `plasma-bin` file) — since neither
`bump-cask-pr` nor the scheduled-bump workflow touch anything but
`version`/`sha256`. That path is stable going forward, so this shouldn't
recur. (Considered switching the cask to consume the `.dmg` directly via
Homebrew Cask's native `.dmg` handling instead of the hand-rolled
`.app.zip` — deferred; `.app.zip` stays the cask's source for now.)

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
