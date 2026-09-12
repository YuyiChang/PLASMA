# Install & first launch

PLASMA runs a local web server and opens in your browser at
`http://127.0.0.1:7860`. Nothing leaves your machine.

## Get PLASMA

=== "Prebuilt binary (no Python)"

    Download the asset for your platform from the
    [latest release](https://github.com/YuyiChang/PLASMA/releases/latest):

    | Platform | Asset |
    |---|---|
    | macOS (Apple silicon) | `PLASMA_MacOS_arm64` |
    | Linux (x86-64) | `PLASMA_Linux_x64` |
    | Linux arm64 — Jetson Orin, **JetPack 6** | `PLASMA_Linux_arm64` |
    | Windows (x86-64) | `PLASMA_Windows_x64.exe` |

    `PLASMA_Linux_arm64` is built against Ubuntu 22.04 (glibc 2.35), so it needs
    **JetPack 6**. On JetPack 5 (Ubuntu 20.04), install from source instead. It
    does not bundle the qb2 LiDAR plugin.

    It is a **single console executable**, not a `.app` or installer. On macOS,
    the first run is blocked by Gatekeeper — right-click → **Open**, or
    `xattr -dr com.apple.quarantine PLASMA_MacOS_arm64`, then run it from a
    terminal:

    ```bash
    chmod +x PLASMA_MacOS_arm64
    ./PLASMA_MacOS_arm64
    ```

=== "From source"

    For development or to run unreleased changes. You need
    [`liblsl`](https://github.com/sccn/liblsl) — a native library pip cannot
    supply everywhere; conda-forge is the reliable source.

    ```bash
    conda create -n plasma python=3.12
    conda activate plasma
    conda install -c conda-forge liblsl
    pip install -e ".[all]"        # or a lean subset: ".[msense]", ".[qb2,pupil]"
    python -m plasma
    ```

    !!! warning "Don't skip `liblsl`"
        It is the step most source installs miss. Without it the built-in
        recorder reports *unavailable* and no XDF is written.

    `pip install` only puts `plasma` on `$PATH` — it doesn't add a clickable
    icon anywhere. For a Desktop (and Start Menu / app-launcher) icon that
    launches this environment's PLASMA without a terminal command:

    ```bash
    pip install -e ".[desktop]"
    plasma-install-shortcut
    ```

    This is separate from the prebuilt-binary tab above: it's a shortcut to
    *this* pip install, not a standalone Python-free executable. Add
    `--no-terminal` to hide the console window (macOS: uses an Automator
    wrapper and may need a one-time Gatekeeper approval), or
    `--no-startmenu` for a Desktop-only icon. See
    `plasma-install-shortcut --help`.

=== "Into another project"

    The distribution is `plasma-app`; the import package is `plasma`.

    ```bash
    pip install "plasma-app[msense] @ git+https://github.com/YuyiChang/PLASMA@v2.0.0"
    ```

## First launch

On first run PLASMA creates its **home directory** and writes a default
configuration. Where the home directory is depends on how you started it:

| How you run PLASMA | Home directory |
|---|---|
| `$PLASMA_HOME` is set | that path (always wins) |
| Prebuilt binary, macOS | `~/Library/Application Support/PLASMA` |
| Prebuilt binary, Windows | `%LOCALAPPDATA%\PLASMA` |
| Prebuilt binary, Linux | `~/.local/share/plasma` |
| From source | the current working directory |

The path is resolved **once, at startup** — set `PLASMA_HOME` before launching
if you want to override it.

After the first launch the home directory contains:

```text
<home>/
├── plasma_device_config.json      # device catalog + addresses (see Configure)
└── data/
    └── 2026-09-08_plasma_session.log   # one log file per calendar day
```

Once you run a session, `data/` fills in with a per-session folder and a rolling
event log — see [Files & paths](../reference/files-and-paths.md).

## The window

![PLASMA Session Dashboard on first launch](../assets/screenshots/session-dashboard.png){ .shot }

PLASMA opens on the **Session Dashboard**. The tab strip across the top is:

- **Session Dashboard** — enter IDs, initialise devices, Start / Stop, watch the memo.
- **Data Dashboard** — live per-channel plots of any stream.
- **🍠 YAMS (MSense Tools)** — wristband signal-quality, orientation, download, extract.
- **Configuration** — the device catalog, network addresses, demo mode, power.

The YAMS tab only appears when an MSense device (real or simulated) is enabled.

---

Next: [Configure PLASMA](configure.md).
