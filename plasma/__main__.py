import atexit
import os
import signal
import sys
import gradio as gr
from plasma import plugins
from plasma.app_context import app_context
from plasma.integrated_panel import IntegratedPanel
from plasma.config import device_config
from plasma import api as plasma_api


def _handle_sigterm(signum, frame):
    # run atexit hooks (device shutdown-cleanup handlers) then exit hard, so
    # `kill <pid>` doesn't leave a device mid-transfer holding a resource
    # (e.g. a BLE connection slot) until its own supervision timeout
    atexit._run_exitfuncs()
    os._exit(143)


def _relaunch_argv():
    """argv for re-launching this same PLASMA. Under PyInstaller (onefile)
    `sys.executable` IS the bundle exe; from source it's the interpreter plus
    `-m plasma`. Any extra CLI args are preserved."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *sys.argv[1:]]
    return [sys.executable, "-m", "plasma", *sys.argv[1:]]


# set by main() so shutdown() can stop the server (frees the port before a
# restart re-binds it)
app = None


def shutdown(restart=False):
    """Run every registered cleanup hook (each MSense driver's
    `_shutdown_cleanup`, the recording flush + device disconnect from
    `IntegratedPanel._atexit_cleanup`), stop the Gradio server, then re-exec
    PLASMA (`restart=True`) or exit the process. Safe to call from a Gradio
    event-handler thread — mirrors `_handle_sigterm`'s
    `atexit._run_exitfuncs()` + hard-exit pattern."""
    atexit._run_exitfuncs()
    if app is not None:
        try:
            app.close()
        except Exception:
            pass
    if restart:
        argv = _relaunch_argv()
        if os.name == "nt":
            # execv on Windows is spawn-then-exit, which flickers the console;
            # an explicit Popen + exit is more predictable there.
            import subprocess
            subprocess.Popen(argv, close_fds=False)
            os._exit(0)
        os.execv(argv[0], argv)
    os._exit(0)

js_func = """
function refresh() {
    const url = new URL(window.location);

    if (url.searchParams.get('__theme') !== 'light') {
        url.searchParams.set('__theme', 'light');
        window.location.href = url.href;
    }

    // Gradio 5's gr.Plot destroys and recreates the whole Plotly <div> on
    // every value it receives (a <svelte:component> {#key}), and its Plotly
    // wrapper never calls Plotly.purge — so a plot on a gr.Timer orphans a
    // graph div (event handlers, internal caches, a WebGL context for 3D /
    // scattergl) roughly once a second, until the tab runs out of memory
    // (gradio#10252). One <body> observer purges each removed graph div, and
    // also re-applies the scroll position Plotly's DOM swap resets to top.
    let lastScrollY = window.scrollY;
    window.addEventListener('scroll', () => { lastScrollY = window.scrollY; },
                            {passive: true});

    const purge = (node) => {
        if (!node || node.nodeType !== 1) return;
        const divs = node.matches && node.matches('.js-plotly-plot')
            ? [node]
            : Array.from(node.querySelectorAll?.('.js-plotly-plot') || []);
        for (const gd of divs) {
            try { window.Plotly && window.Plotly.purge(gd); } catch (e) {}
        }
    };

    let sawSwap = false;
    const obs = new MutationObserver((records) => {
        for (const r of records) {
            for (const n of r.removedNodes) purge(n);
            if (r.removedNodes.length || r.addedNodes.length) sawSwap = true;
        }
        if (sawSwap && Math.abs(window.scrollY - lastScrollY) > 1) {
            window.scrollTo(0, lastScrollY);
        }
        sawSwap = false;
    });
    obs.observe(document.body, {childList: true, subtree: true});
    window.addEventListener('pagehide', () => obs.disconnect(), {once: true});
}
"""

def build_blocks(ip):
    """Assemble the full PLASMA `gr.Blocks` (every tab + the headless API) for
    the given `IntegratedPanel`. Shared by `main()` and the screenshot-capture
    tool (`scripts/capture_screenshots.py`) so both build the identical UI.
    Does not launch the server."""
    with gr.Blocks(title=app_context().app_name, theme=gr.themes.Ocean(), js=js_func) as app:
        yams_title = None
        with gr.Tabs() as top_tabs:
            with gr.Tab("Session Dashboard"):
                ip.interface()
            with gr.Tab("Data Dashboard"):
                ip.visualizer_interface()
            # extra tabs contributed by enabled plugins (e.g. MSense SQC / IMU).
            # The real and the simulated MSense plugins both contribute the same
            # "🍠 YAMS (MSense Tools)" tab — de-dupe by title so enabling both
            # doesn't build it twice.
            seen_tabs = set()
            for plugin in device_config.get_active_table().values():
                for tab_title, builder in plugin.tabs:
                    if tab_title in seen_tabs:
                        continue
                    seen_tabs.add(tab_title)
                    if "YAMS" in tab_title:
                        yams_title = tab_title
                    with gr.Tab(tab_title):
                        builder(ip)
            with gr.Tab("Configuration"):
                device_config.interface()
            # with gr.Tab("PL"):
            #     pl.interface()

        _wire_plot_timer_gate(ip, top_tabs, yams_title)

        # stop any live MSense stream when the last browser tab goes away — a
        # continuous stream with nobody watching just churns the BLE radio and
        # (pre-fix) the plot memory. Best-effort; streams aren't multi-viewer.
        def _on_unload():
            for dev in list(getattr(ip, "available_devices", []) or []):
                fn = getattr(dev, "stop_all_live_streams", None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:
                        pass
        app.unload(_on_unload)

        # headless control + situational-awareness endpoints (/status, /start,
        # /stop, /mark, /events) + the 1 Hz durable event-log pump. Additive —
        # no UI. See docs/headless.md.
        plasma_api.register(ip)
    return app


def _wire_plot_timer_gate(ip, top_tabs, yams_title):
    """Run each live-plot refresh timer only while its tab is on screen.
    gr.Plot recreates its whole Plotly <div> for every value it receives
    (gradio#10252), so a timer ticking behind a hidden tab is a memory leak.
    The MSense sub-tab gate is in panels/tools.py; this adds the top-level one."""
    from plasma.devices.msense.panels.tools import _SQC_SUBTAB, _IMU_SUBTAB

    specs = [(ip._viz_timer, "Data Dashboard", None)]
    if getattr(ip, "_sqc_timer", None) is not None:
        specs.append((ip._sqc_timer, yams_title, _SQC_SUBTAB))
    if getattr(ip, "_imu_timer", None) is not None:
        specs.append((ip._imu_timer, yams_title, _IMU_SUBTAB))

    sub_state = getattr(ip, "_yams_sub", None) or gr.State("")

    def _on_top(sub, evt: gr.SelectData):
        top = evt.value or ""
        updates = [
            gr.Timer(active=(top == want_top
                             and (want_sub is None or sub == want_sub)))
            for _, want_top, want_sub in specs
        ]
        return updates if len(updates) > 1 else updates[0]

    top_tabs.select(_on_top, inputs=sub_state, outputs=[t for t, _, _ in specs])


def main():
    global app

    try:
        signal.signal(signal.SIGTERM, _handle_sigterm)
    except ValueError:
        pass  # not the main thread (e.g. imported oddly) — atexit still covers normal exit

    plugins.load_plugins()
    device_config.refresh_defaults()

    ip = IntegratedPanel()
    # pl = PupilLabsDashboard()

    app = build_blocks(ip)
    app.launch(inbrowser=True, share=False)


if __name__ == '__main__':
    main()

    

