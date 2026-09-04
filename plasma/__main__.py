import atexit
import os
import signal
import gradio as gr
from plasma import plugins
from plasma.app_context import app_context
from plasma.integrated_panel import IntegratedPanel
from plasma.config import device_config


def _handle_sigterm(signum, frame):
    # run atexit hooks (device shutdown-cleanup handlers) then exit hard, so
    # `kill <pid>` doesn't leave a device mid-transfer holding a resource
    # (e.g. a BLE connection slot) until its own supervision timeout
    atexit._run_exitfuncs()
    os._exit(143)

js_func = """
function refresh() {
    const url = new URL(window.location);

    if (url.searchParams.get('__theme') !== 'light') {
        url.searchParams.set('__theme', 'light');
        window.location.href = url.href;
    }

    // Some tabs (e.g. MSense SQC) redraw a Plotly chart on a timer; Plotly
    // recreates the chart's DOM on every redraw, which resets the whole
    // page's scroll position back to top. Rather than hook into Gradio's
    // per-event dependency chain (fragile — a first attempt broke plot
    // rendering entirely), passively watch known plot containers and
    // silently re-apply the scroll position that was in effect just before
    // each redraw. Containers opt in via elem_id, listed here.
    const scrollGuardIds = ['sqc-plot-container'];
    let lastScrollY = window.scrollY;
    window.addEventListener('scroll', () => { lastScrollY = window.scrollY; }, {passive: true});

    const attachScrollGuards = () => {
        for (const id of scrollGuardIds) {
            const el = document.getElementById(id);
            if (!el || el.__scrollGuardAttached) continue;
            el.__scrollGuardAttached = true;
            new MutationObserver(() => {
                if (Math.abs(window.scrollY - lastScrollY) > 1) {
                    window.scrollTo(0, lastScrollY);
                }
            }).observe(el, {childList: true, subtree: true});
        }
    };
    attachScrollGuards();
    // Gradio tabs can mount lazily, so the container may not exist yet
    const scrollGuardInterval = setInterval(() => {
        attachScrollGuards();
        if (scrollGuardIds.every(id => document.getElementById(id)?.__scrollGuardAttached)) {
            clearInterval(scrollGuardInterval);
        }
    }, 500);
}
"""

def main():
    try:
        signal.signal(signal.SIGTERM, _handle_sigterm)
    except ValueError:
        pass  # not the main thread (e.g. imported oddly) — atexit still covers normal exit

    plugins.load_plugins()
    device_config.refresh_defaults()

    ip = IntegratedPanel()
    # pl = PupilLabsDashboard()

    with gr.Blocks(title=app_context().app_name, theme=gr.themes.Ocean(), js=js_func) as app:
        with gr.Tab("Session Dashboard"):
            ip.interface()
        with gr.Tab("Data Dashboard"):
            ip.visualizer_interface()
        # extra tabs contributed by enabled plugins (e.g. MSense SQC / IMU)
        for plugin in device_config.get_active_table().values():
            for tab_title, builder in plugin.tabs:
                with gr.Tab(tab_title):
                    builder(ip)
        with gr.Tab("Configuration"):
            device_config.interface()
        # with gr.Tab("PL"):
        #     pl.interface()

    app.launch(inbrowser=True, share=False)


if __name__ == '__main__':
    main()

    

