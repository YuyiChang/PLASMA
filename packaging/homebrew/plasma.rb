cask "plasma" do
  version "2.2.0"
  sha256 "REPLACE_WITH_SHA256" # from PLASMA_MacOS_arm64.app.zip.sha256 on the v#{version} release

  url "https://github.com/YuyiChang/PLASMA/releases/download/v#{version}/PLASMA_MacOS_arm64.app.zip"
  name "PLASMA"
  desc "Platform for LSL-based Acquisition of Sensor Metrics and Analytics"
  homepage "https://github.com/YuyiChang/PLASMA"

  # only the Apple-silicon binary is built (see .github/workflows/build.yml)
  depends_on arch: :arm64

  app "PLASMA.app"
  # Also expose the console binary directly as `plasma`, for anyone who'd
  # rather skip PLASMA.app's Terminal-launcher indirection (see
  # .github/build_macos_app.sh — the .app has no window of its own; it opens
  # this same binary in a visible Terminal window so startup failures stay
  # visible instead of failing silently in the background).
  binary "#{appdir}/PLASMA.app/Contents/Resources/plasma-bin", target: "plasma"

  postflight do
    system_command "/usr/bin/xattr",
                    args: ["-dr", "com.apple.quarantine", "#{appdir}/PLASMA.app"]
  end

  livecheck do
    url :url
    strategy :github_latest
  end

  caveats <<~EOS
    PLASMA is not code-signed or notarized; this cask clears the
    com.apple.quarantine attribute so Gatekeeper won't block the first run.

    Launching PLASMA.app opens a Terminal window running the real console
    app. Prefer a plain terminal instead? Run `plasma` directly.

    Config, gyro-bias calibration and recordings are written to
    ~/Library/Application Support/PLASMA — not the working directory or a
    pip install's PLASMA_HOME/cwd default, since this binary runs "frozen".
  EOS
end
