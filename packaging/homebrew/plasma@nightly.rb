cask "plasma@nightly" do
  version :latest
  sha256 :no_check

  # build.yml's `schedule` job republishes these assets under the same
  # "nightly" release tag every day (the tag itself moves, rather than a new
  # vX.Y.Z tag being cut) — so, unlike the versioned `plasma` cask, this URL
  # never changes and this file never needs a version/sha256 bump.
  url "https://github.com/YuyiChang/PLASMA/releases/download/nightly/PLASMA_MacOS_arm64.app.zip"
  name "PLASMA (nightly)"
  desc "Nightly build of PLASMA, the LSL sensor-acquisition platform"
  homepage "https://github.com/YuyiChang/PLASMA"

  # version :latest means Homebrew has nothing to compare against a newer
  # release, so `brew upgrade` can't detect a fresh nightly on its own.
  auto_updates false
  # both this and `plasma` install PLASMA.app / a `plasma` binary — can't
  # have both on disk at once.
  conflicts_with cask: "plasma"
  # only the Apple-silicon binary is built (see .github/workflows/build.yml)
  depends_on arch: :arm64
  depends_on :macos

  app "PLASMA.app"
  binary "#{appdir}/PLASMA.app/Contents/Resources/PLASMA_MacOS_arm64/PLASMA_MacOS_arm64", target: "plasma"

  postflight_steps do
    # {{appdir}} is expanded at run time by the install-steps runner — the
    # DSL block here has no `appdir` method of its own (see Homebrew's
    # install_steps.rb CONTENT_PATH_TOKENS). must_succeed: false because a
    # local/test install may not carry the quarantine attribute at all.
    run "/usr/bin/xattr", args:         ["-dr", "com.apple.quarantine", "{{appdir}}/PLASMA.app"],
                          must_succeed: false
  end

  caveats <<~EOS
    This is a nightly build off `dev`, rebuilt automatically every day at
    07:00 UTC — expect it to be less stable than `plasma`.

    Homebrew has no version number to compare here, so `brew upgrade` won't
    pick up a new nightly on its own. Grab the latest one with:
      brew reinstall --cask plasma@nightly

    Conflicts with the `plasma` cask (both install PLASMA.app) — uninstall
    one before installing the other.

    PLASMA is not code-signed or notarized; this cask clears the
    com.apple.quarantine attribute so Gatekeeper won't block the first run.

    Launching PLASMA.app opens a Terminal window running the real console
    app. Prefer a plain terminal instead? Run `plasma` directly.

    Config, gyro-bias calibration and recordings are written to
    ~/Library/Application Support/PLASMA — not the working directory or a
    pip install's PLASMA_HOME/cwd default, since this binary runs "frozen".
  EOS
end
