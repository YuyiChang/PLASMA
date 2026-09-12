"""``plasma-install-shortcut`` — drop a double-clickable PLASMA icon on the
Desktop (and Start Menu / app launcher where the platform has one) for a
``pip install``-ed PLASMA.

``pip install`` only ever puts ``plasma`` on ``$PATH`` as a console-script —
it never touches the Dock, Start Menu, or Desktop. This is the optional extra
step that closes that gap for anyone who wants a clickable icon without the
standalone PyInstaller bundle (see ``app_macos.spec`` / ``app_linux.spec`` /
``app_windows.spec`` for that separate, Python-free distribution channel).

Points the shortcut directly at *this* environment's ``plasma`` console-script
(found via ``$PATH`` or this interpreter's bin/Scripts dir) using
``noexe=True``, so the generated shortcut runs that script standalone rather
than re-deriving a `python -m ...` invocation — see the pyshortcuts prototype
notes in the packaging discussion for why.

    plasma-install-shortcut [--name NAME] [--no-terminal] [--no-startmenu]

Requires the ``desktop`` extra (``pip install plasma-app[desktop]``).
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

_ICON_DIR = Path(__file__).parent / "resources" / "icons"


def _icon_path() -> str:
    """Best icon file for this platform, matching what pyshortcuts expects
    (.icns on macOS, .ico on Windows, .ico/.png on Linux) — same assets the
    PyInstaller specs already bundle."""
    if sys.platform == "darwin":
        return str(_ICON_DIR / "plasma.icns")
    if os.name == "nt":
        return str(_ICON_DIR / "plasma.ico")
    return str(_ICON_DIR / "plasma.png")


def _plasma_executable() -> str:
    """Path to the ``plasma`` console-script installed by this package in the
    current Python environment. Prefers ``$PATH`` (covers pipx / activated
    venvs); falls back to this interpreter's own bin/Scripts dir, which is
    where pip puts console-scripts regardless of whether that dir is on
    ``$PATH``."""
    exe = shutil.which("plasma")
    if exe:
        return exe
    bindir = Path(sys.executable).parent
    candidate = bindir / ("plasma.exe" if os.name == "nt" else "plasma")
    if candidate.exists():
        return str(candidate)
    raise FileNotFoundError(
        "Could not find the 'plasma' console-script in this Python "
        "environment. Is plasma-app installed here (`pip install "
        "plasma-app`)?"
    )


def install_shortcut(name="PLASMA", terminal=True, startmenu=True, folder=None):
    """Create the Desktop (+ Start Menu, if requested) shortcut. Returns the
    ``pyshortcuts.Shortcut`` namedtuple describing what was written.

    ``terminal=True`` (default) keeps a visible console window, matching the
    PyInstaller builds' ``console=True`` — simplest and most robust. Passing
    ``terminal=False`` on macOS switches to pyshortcuts' Automator-stub
    wrapper, which needs `codesign`/Automator present and may need a one-time
    Gatekeeper approval; prefer the terminal window unless a chrome-less
    launch is worth that trade-off.
    """
    try:
        from pyshortcuts import make_shortcut
    except ImportError as e:
        raise ImportError(
            "pyshortcuts is required to create a desktop shortcut — install "
            "it with `pip install plasma-app[desktop]`."
        ) from e

    return make_shortcut(
        script=_plasma_executable(),
        name=name,
        description="PLASMA sensor acquisition dashboard",
        icon=_icon_path(),
        terminal=terminal,
        desktop=True,
        startmenu=startmenu,  # pyshortcuts itself no-ops this on macOS
        folder=folder,
        noexe=True,  # script is already a standalone console-script binary
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="plasma-install-shortcut",
        description="Create a double-clickable PLASMA icon pointing at this "
                     "Python environment's `plasma` install.",
    )
    parser.add_argument("--name", default="PLASMA", help="shortcut display name")
    parser.add_argument(
        "--no-terminal", action="store_true",
        help="hide the console window (macOS: uses an Automator wrapper, "
             "may need a Gatekeeper/TCC re-approval)")
    parser.add_argument(
        "--no-startmenu", action="store_true",
        help="Desktop icon only; skip Start Menu / app-launcher entry")
    args = parser.parse_args(argv)

    scut = install_shortcut(
        name=args.name,
        terminal=not args.no_terminal,
        startmenu=not args.no_startmenu,
    )
    print(f"Created shortcut: {scut.target}")
    print(f"  runs:    {scut.script}")
    print(f"  desktop: {scut.desktop_dir}")
    if scut.startmenu_dir:
        print(f"  menu:    {scut.startmenu_dir}")


if __name__ == "__main__":
    main()
