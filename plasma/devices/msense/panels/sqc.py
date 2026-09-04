"""The "ECG/PPG Signal Quality" tab: on-demand raw ECG/PPG snapshots over the
NUS bounded-stream protocol. Takes the IntegratedPanel instance."""
import os

import gradio as gr
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from plasma.app_context import app_context
from ..signal_quality import filter_ecg, filter_ppg
from ._common import msense_device as _msense_device


def _rel_data_path(path):
    """`saved_path` is always somewhere under the data folder (either the
    active session's log dir or data/sqc_snapshots/...) — show it relative to
    that root instead of the full absolute path. Falls back to the original
    path if it's ever outside the data dir."""
    if not path:
        return path
    try:
        rel = os.path.relpath(path, app_context().data_dir)
    except Exception:
        return path
    return path if rel.startswith("..") else rel


def _fmt_bytes(n):
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.2f} MiB"
    return f"{n / 1024:.1f} KiB"

# Plotly recreates the chart's DOM on every figure update, which resets page
# scroll position — worse the more subplot rows there are. A first attempt
# hooked scroll save/restore into the Gradio .click()/.tick() event chain
# (fn=None + js= steps chained with .then()) — that broke plot rendering
# entirely (including the live "receiving" preview), likely an interaction
# between chaining and gr.Timer's repeat-triggering, not fully root-caused.
# Reverted; see SCROLL_GUARD_ELEM_ID below for the replacement approach,
# which doesn't touch this file's event wiring at all.
SCROLL_GUARD_ELEM_ID = "sqc-plot-container"


def build_sqc_tab(ip):
    with gr.Column():
        with gr.Row():
            btn_request_sqc = gr.Button("📡 Snapshot all wristbands", variant="primary", scale=2)
            btn_refresh_sqc = gr.Button("🔄 Refresh")
            btn_cancel_sqc = gr.Button("✖ Cancel all")
        with gr.Row():
            sqc_stream_mode = gr.Radio(
                            choices=["Sequential", "Parallel", "Hybrid"], value="Sequential",
                            label="Streaming mode")
            sqc_mode = gr.Radio(
                choices=["All", "History Only", "Custom"], value="All",
                label="Capture mode")
            sqc_max_s = gr.Number(
                value=5, precision=1, minimum=0,
                label="Custom — stop after N s", interactive=True)

        with gr.Accordion(label="📈 Plot options", open=False):
            ppg_display_mode = gr.Radio(
                choices=["Raw", "Filtered", "Both"], value="Filtered",
                label="PPG channels show")
            with gr.Row():
                ppg_y_min = gr.Number(value=-1000, label="Filtered Y-axis min")
                ppg_y_max = gr.Number(value=1000, label="Filtered Y-axis max")
            show_hist_boundary = gr.Checkbox(
                value=True, label="Show history/forward boundary")

        sqc_status = gr.Markdown()
        # elem_id lets a page-level script (plasma/__main__.py's js_func)
        # find this container and counteract Plotly's scroll-jump-on-redraw
        # without touching any event wiring here — see SCROLL_GUARD_ELEM_ID.
        sqc_plot = gr.Plot(show_label=False, elem_id=SCROLL_GUARD_ELEM_ID)

        # coarse refresh — the BLE transfer is slow, so ~1 Hz is plenty and
        # keeps re-decoding / re-plotting the growing signal cheap
        sqc_timer = gr.Timer(value=1.0, active=True)

        def _request(mode, max_seconds=None, stream_mode="Sequential"):
            dev = _msense_device(ip)
            if dev is None:
                return "⛔ MSense device not initialized — initialize it on the Session dashboard tab first"
            history_only = (mode == "History Only")
            ms = None
            if mode == "Custom":
                try:
                    ms = float(max_seconds) if max_seconds not in (None, "", 0) else None
                except (TypeError, ValueError):
                    ms = None
                if ms is not None and ms <= 0:
                    ms = None
            return dev.request_all_sqc_snapshots(
                max_seconds=ms, history_only=history_only,
                stream_mode=stream_mode.lower())

        def _cancel():
            dev = _msense_device(ip)
            if dev is None:
                return "⛔ MSense device not initialized"
            return dev.cancel_all_sqc_snapshots()

        def _on_mode_change(mode):
            return gr.update(interactive=(mode == "Custom"))

        plot_opt_inputs = [ppg_display_mode, ppg_y_min, ppg_y_max, show_hist_boundary]

        def _update_with_opts(mode, lo, hi, show_boundary):
            return _update_sqc(ip, mode, lo, hi, show_boundary)

        btn_refresh_sqc.click(_update_with_opts, inputs=plot_opt_inputs,
                              outputs=[sqc_status, sqc_plot])
        btn_request_sqc.click(_request, inputs=[sqc_mode, sqc_max_s, sqc_stream_mode], outputs=sqc_status)
        btn_cancel_sqc.click(_cancel, outputs=sqc_status)
        sqc_mode.change(_on_mode_change, inputs=sqc_mode, outputs=sqc_max_s)
        sqc_timer.tick(fn=_update_with_opts, inputs=plot_opt_inputs,
                       outputs=[sqc_status, sqc_plot])
        for comp in plot_opt_inputs:
            comp.change(_update_with_opts, inputs=plot_opt_inputs,
                       outputs=[sqc_status, sqc_plot])

    with gr.Accordion(open=False, label="ℹ️ Help"):
        gr.Markdown(
                    "On-demand raw ECG/PPG snapshot for every connected MSense wristband so you can "
                    "eyeball electrode / optical contact before starting a full session. Initialize the "
                    "MSense device on the **Session dashboard** tab first, then Refresh here. Each "
                    "capture is saved to disk (session log dir if a recording is running, else "
                    "`data/sqc_snapshots/`).\n\n"
                    "The device always streams a fixed 96 KiB (a pre-buffered *history* window then a "
                    "*forward* window it acquires live — ~16 s total ECG / ~24 s PPG). Pick a capture "
                    "mode: **All** takes the full stream; **History Only** sends CANCEL at the "
                    "history→forward boundary (~5 s ECG / ~8 s PPG), skipping the live-acquisition wait; "
                    "**Custom** sends CANCEL once the given *N* seconds have arrived and keeps the "
                    "partial.\n\n"
                    "Pick a **streaming mode** for how wristbands are scheduled: **Sequential** "
                    "(default) — one wristband fully finishes before the next starts; safest, since the "
                    "Mac's single BLE radio is time-sliced across all connections and a burst transfer "
                    "is bandwidth-heavy. **Parallel** — every wristband starts at once and streams "
                    "concurrently; fastest wall-clock time, but each wristband's own transfer runs "
                    "slower (radio time-sliced N ways) and stalls are more likely. **Hybrid** — a "
                    "pipeline: the first wristband starts alone, and as soon as it finishes its brief "
                    "*history* burst and drops into the lighter live *forward* phase, the next "
                    "wristband's snapshot starts (now running alongside it); this repeats down the "
                    "list, so only one wristband is ever in the heavy history burst at a time — a "
                    "middle ground between speed and radio contention.\n\n"
                    "PPG's raw optical trace is dominated by a large DC term that hides the "
                    "pulsatile component, so the **Plot options** accordion lets you pick what's "
                    "plotted for PPG wristbands: **Filtered** (default) — the bandpass output, the "
                    "only readily readable view; **Raw** — the unfiltered channels; **Both** — raw "
                    "on the main axis, filtered on a secondary axis. Applies to PPG only; ECG always "
                    "shows raw + filtered together. **Filtered Y-axis min/max** fixes the filtered "
                    "trace's scale (default -1000 to 1000) instead of auto-scaling per capture, so "
                    "amplitude is comparable across channels/wristbands at a glance. **Show "
                    "history/forward boundary** draws a dashed vertical line on every plot (ECG and "
                    "PPG, live or finished) at the point the pre-buffered *history* window gives way "
                    "to the live-acquired *forward* window.\n"
                )


def _update_sqc(ip, ppg_mode="Filtered", ppg_y_min=-1000, ppg_y_max=1000, show_hist_boundary=True):
    dev = _msense_device(ip)
    if dev is None:
        return "⛔ MSense device not initialized — initialize it on the Session dashboard tab first", gr.update()

    names = dev.get_sqc_devices()
    caps_note = dev.caps_summary()
    if not names:
        msg = "No NUS-capable MSense wristbands connected."
        if caps_note:
            msg += f"\n\n_{caps_note}_"
        return msg, gr.update()

    def _diag_str(status):
        d = status.get("diag") or {}
        if not d.get("notifs"):
            return ""
        s = (f" · {d['kib_s']} KiB/s ({d['notif_s']}/s × {d['mean_notif_bytes']}B), "
             f"gap {d['last_gap_s']}s (max {d['max_gap_s']}s), proc≤{d['max_proc_ms']}ms")
        if d.get("mtu") is not None or d.get("rssi") is not None:
            s += f", mtu {d.get('mtu')}, rssi {d.get('rssi')}"
        if d.get("seq_gaps"):
            s += f", ⚠️{d['seq_gaps']} seq gaps"
        if d.get("recoveries"):
            s += f", 🔄{d['recoveries']}× reconnect"
        return s

    lines, results = [], []
    for name in names:
        disp = dev.display_name(name)
        status = dev.get_sqc_status(name)
        st = status["status"]
        if st in ("idle", "unavailable"):
            lines.append(f"- **{disp}** — idle")
            continue
        if st in ("requesting", "receiving"):
            total = status["records_total"] or "?"
            pct = ""
            if isinstance(total, int) and total:
                pct = f" ({100 * status['records_received'] // total}%)"
            lines.append(
                f"- **{disp}** — 📡 {st} ({status['phase']}) "
                f"{status['records_received']}/{total} records{pct}{_diag_str(status)}"
            )
            preview = dev.get_sqc_preview(name)
            if preview is not None:
                results.append((disp, preview))
            continue
        if st == "finishing":
            lines.append(f"- **{disp}** — 💾 finalizing…{_diag_str(status)}")
            continue
        if st == "rejected":
            lines.append(f"- **{disp}** — 🚫 rejected: {status['error']}")
            continue
        if st == "error":
            lines.append(f"- **{disp}** — ❌ {status['error']}{_diag_str(status)}")
            continue

        result = dev.get_sqc_result(name)
        if result is None:
            lines.append(f"- **{disp}** — ❌ ready but no data")
            continue
        results.append((disp, result))          # always plot a decoded result
        prov = result.get("provenance") or {}
        dirty = " ⚠️dirty" if prov.get("git_tree_state") == "dirty" else ""
        quick = ""
        if result.get("quick_seconds"):
            ph = prov.get("phase_at_cancel", "")
            quick = f" · ✂ partial {result['quick_seconds']:g}s{' (' + ph + ')' if ph else ''}"
        icon = "⚠️" if result.get("warning") else "✅"
        warn = f" — _{result['warning']}_" if result.get("warning") else ""
        diag = status.get("diag") or {}
        xfer = ""
        if diag.get("kib_s"):
            xfer = (f" · {_fmt_bytes(diag['bytes'])}, {diag['duration_s']:.1f}s, "
                   f"{diag['kib_s']:g} KiB/s avg")
        lines.append(
            f"- **{disp}** — {icon} {result['device_type']}{quick} · id `{prov.get('device_id', '?')}` · "
            f"fw `{str(prov.get('git_commit', '?'))[:10]}`{dirty} · "
            f"saved `{_rel_data_path(status['saved_path'])}`{xfer}{warn}"
        )

    if caps_note and "NUS unavailable" in caps_note:
        lines.append(f"\n_{caps_note}_")

    try:
        y_range = (float(ppg_y_min), float(ppg_y_max))
        if y_range[0] >= y_range[1]:
            y_range = (-1000, 1000)
    except (TypeError, ValueError):
        y_range = (-1000, 1000)

    fig = (_build_sqc_figure(results, ppg_mode, y_range, bool(show_hist_boundary))
           if results else gr.update())
    return "\n".join(lines), fig


# PPG channel colors hint at the actual LED wavelength (infrared -> warm,
# green LEDs -> green) rather than an arbitrary categorical palette.
_PPG_CHANNEL_COLORS = {
    "ir1": "#d62728", "ir2": "#ff7f0e",   # infrared: red / orange
    "g1": "#2ca02c", "g2": "#17becf",     # green LEDs: green / teal
}
_FALLBACK_COLORS = ["#1f77b4", "#9467bd", "#8c564b", "#e377c2"]


def _channel_color(ch_name, idx):
    return _PPG_CHANNEL_COLORS.get(ch_name, _FALLBACK_COLORS[idx % len(_FALLBACK_COLORS)])


def _build_sqc_figure(results, ppg_mode="Filtered", ppg_y_range=(-1000, 1000),
                      show_hist_boundary=True):
    """One row per device/capture — every channel a device reports (all 4 PPG
    optical channels, or ECG's single channel) is a trace sharing that row's
    axes, rather than eating its own row. PPG's raw signal is dominated by a
    large DC term that hides the pulsatile component, so `ppg_mode` controls
    what's drawn for PPG rows: "Raw", "Filtered" (default — the only
    practically readable view), or "Both" (raw on the primary axis, filtered
    on a secondary axis, per channel). ECG keeps its existing raw+filtered
    overlay unconditionally — its raw trace is already legible on its own.
    A still-streaming ("partial") capture always shows raw regardless of
    `ppg_mode` — filtering a still-growing signal just adds edge artifacts.

    `ppg_y_range` fixes the Y-axis range of whichever axis is showing the
    filtered PPG trace (the primary axis in "Filtered" mode, the secondary
    axis in "Both" mode) — a static scale makes it possible to compare
    signal amplitude across captures/channels by eye instead of Plotly
    auto-scaling each row to its own data. Not applied to raw traces (a
    fixed range around 0 would just clip PPG's large DC-heavy raw signal)
    or to ECG (unaffected by this whole option, like the rest of `ppg_mode`).

    `show_hist_boundary` draws a dashed vertical line on every row (ECG and
    PPG alike, live preview or finished) at the point the pre-buffered
    *history* window gives way to the live-acquired *forward* window —
    `history_records / fs` seconds in. Skipped for a row if that boundary
    falls beyond what's actually been captured so far (e.g. a history-only
    quick capture, or a live preview still mid-history).
    """
    rows = []
    for name, result in results:
        partial = result.get("partial")            # live preview, still streaming
        quick_s = result.get("quick_seconds")      # finished but cut short (on purpose or not)
        title = name
        if partial:
            title += " — receiving…"
        elif quick_s:
            title += f" — {quick_s:g}s partial"
        rows.append((title, result))

    fig = make_subplots(
        rows=len(rows), cols=1, shared_xaxes=False, vertical_spacing=0.06 / max(len(rows), 1),
        subplot_titles=[title for title, _ in rows],
        specs=[[{"secondary_y": True}] for _ in rows],
    )
    # Plotly has one shared legend for the whole figure — with several PPG
    # wristbands each contributing the same channel names, show each
    # (channel, raw/filtered) legend entry only once (first occurrence) so
    # the legend stays a color key, not a repeated per-device list.
    seen_legend_names = set()
    for i, (title, result) in enumerate(rows, start=1):
        fs = result["fs"]
        is_ecg = result["device_type"] == "ECG"
        partial = result.get("partial")
        filtered_axis_secondary = None  # which axis got a filtered trace this row, if any
        n_samples = 0
        for idx, (ch_name, y) in enumerate(result["channels"].items()):
            y = np.asarray(y, dtype=float)
            n_samples = max(n_samples, len(y))
            t = np.arange(len(y)) / fs
            can_filter = not partial and len(y) > 64
            filt = None
            if can_filter:
                try:
                    filt = filter_ecg(y, fs) if is_ecg else filter_ppg(y, fs)
                except Exception:
                    filt = None

            if is_ecg:
                # unchanged: always raw (primary, blue) + filtered (secondary,
                # red), no legend — ECG is already a single labeled channel
                fig.add_trace(go.Scatter(x=t, y=y, mode="lines", name="raw",
                                         line=dict(color="#1f77b4"), showlegend=False),
                              row=i, col=1, secondary_y=False)
                if filt is not None:
                    fig.add_trace(go.Scatter(x=t, y=filt, mode="lines", name="filtered",
                                             line=dict(color="#d62728", width=1), opacity=0.7,
                                             showlegend=False),
                                  row=i, col=1, secondary_y=True)
                continue

            # PPG: ppg_mode gates what's drawn, per channel, in that channel's
            # color. Can't filter yet (still streaming / too short) -> always
            # fall back to raw so a live preview always shows something.
            mode = ppg_mode if filt is not None else "Raw"
            show_raw = mode in ("Raw", "Both")
            show_filt = mode in ("Filtered", "Both") and filt is not None
            color = _channel_color(ch_name, idx)
            if show_raw:
                legend_name = f"{ch_name} raw"
                fig.add_trace(go.Scatter(x=t, y=y, mode="lines", name=legend_name,
                                         line=dict(color=color),
                                         legendgroup=legend_name,
                                         showlegend=legend_name not in seen_legend_names),
                              row=i, col=1, secondary_y=False)
                seen_legend_names.add(legend_name)
            if show_filt:
                legend_name = f"{ch_name} filtered"
                fig.add_trace(go.Scatter(x=t, y=filt, mode="lines", name=legend_name,
                                         line=dict(color=color, width=1.4,
                                                    dash="dot" if show_raw else "solid"),
                                         legendgroup=legend_name,
                                         showlegend=legend_name not in seen_legend_names),
                              row=i, col=1, secondary_y=show_raw)
                seen_legend_names.add(legend_name)
                filtered_axis_secondary = show_raw
        if filtered_axis_secondary is not None:
            fig.update_yaxes(range=list(ppg_y_range), row=i, col=1,
                             secondary_y=filtered_axis_secondary)
        history_records = result.get("history_records")
        if show_hist_boundary and history_records and n_samples:
            boundary_t = history_records / fs
            if boundary_t < n_samples / fs:
                fig.add_vline(x=boundary_t, row=i, col=1,
                              line=dict(color="gray", dash="dash", width=1))
        fig.update_xaxes(title_text="Time (s)", row=i, col=1)

    fig.update_layout(
        height=200 * max(len(rows), 1),
        margin=dict(l=50, r=20, t=60, b=30),
        showlegend=True,   # PPG traces opt in (deduped above); ECG traces opt out
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        uirevision="plasma-sqc",
    )
    return fig
