cask "plasma" do
  version "2.2.0"
  sha256 "REPLACE_WITH_SHA256" # from PLASMA_MacOS_arm64.sha256 on the v#{version} release

  url "https://github.com/YuyiChang/PLASMA/releases/download/v#{version}/PLASMA_MacOS_arm64"
  name "PLASMA"
  desc "Platform for LSL-based Acquisition of Sensor Metrics and Analytics"
  homepage "https://github.com/YuyiChang/PLASMA"

  # only the Apple-silicon binary is built (see .github/workflows/build.yml)
  depends_on arch: :arm64

  binary "PLASMA_MacOS_arm64", target: "plasma"

  # The binary ships unsigned (codesign_identity=None in app_macos.spec), so
  # Gatekeeper quarantines it on download like any other unsigned executable.
  # Clear it the same way the install docs tell a manual downloader to.
  postflight do
    system_command "/usr/bin/xattr",
                    args: ["-dr", "com.apple.quarantine", "#{staged_path}/PLASMA_MacOS_arm64"]
  end

  livecheck do
    url :url
    strategy :github_latest
  end

  caveats <<~EOS
    PLASMA is not code-signed or notarized; this cask clears the
    com.apple.quarantine attribute so Gatekeeper won't block the first run.

    Run `plasma`, then open http://127.0.0.1:7860 in a browser.

    Config, gyro-bias calibration and recordings are written to
    ~/Library/Application Support/PLASMA — not the working directory or a
    pip install's PLASMA_HOME/cwd default, since this binary runs "frozen".
  EOS
end
