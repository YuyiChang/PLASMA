"""The "ECG/PPG Signal Quality" tab: on-demand raw ECG/PPG snapshots over the
NUS bounded-stream protocol. Takes the IntegratedPanel instance."""
import gradio as gr
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from ..signal_quality import filter_ecg, filter_ppg
from ._common import msense_device as _msense_device


def build_sqc_tab(ip):
    with gr.Column():
        gr.Markdown(
            "On-demand raw ECG/PPG snapshot for every connected MSense wristband so you can "
            "eyeball electrode / optical contact before starting a full session. Initialize the "
            "MSense device on the **Session dashboard** tab first, then Refresh here. Wristbands "
            "are snapshotted **one at a time** (the Mac has a single BLE radio); each capture is "
            "saved to disk (session log dir if a recording is running, else "
            "`data/sqc_snapshots/`).\n\n"
            "The device always streams a fixed 96 KiB (a pre-buffered *history* window then a "
            "*forward* window it acquires live — ~16 s total ECG / ~24 s PPG). **Quick mode** "
            "(default) sends CANCEL once *N* seconds have arrived and keeps the partial; with "
            "*N* ≤ the history window (~5 s ECG / ~8 s PPG) the whole live-acquisition wait is "
            "skipped. Clear the field (and leave *History only* off) for a full capture.\n"
        )
        with gr.Row():
            btn_refresh_sqc = gr.Button("🔄 Refresh")
            btn_request_sqc = gr.Button("📡 Snapshot all wristbands (sequential)", variant="primary")
            btn_cancel_sqc = gr.Button("✖ Cancel all")
        with gr.Row():
            sqc_max_s = gr.Number(
                value=5, precision=1, minimum=0,
                label="Quick mode — stop after N s (clear for full capture)")
            sqc_hist_only = gr.Checkbox(
                value=False, label="History only (stop at the history→forward boundary)")

        sqc_status = gr.Markdown()
        sqc_plot = gr.Plot(show_label=False)

        # coarse refresh — the BLE transfer is slow, so ~1 Hz is plenty and
        # keeps re-decoding / re-plotting the growing signal cheap
        sqc_timer = gr.Timer(value=1.0, active=True)

        def _request(max_seconds=None, history_only=False):
            dev = _msense_device(ip)
            if dev is None:
                return "⛔ MSense device not initialized — initialize it on the Session dashboard tab first"
            try:
                ms = float(max_seconds) if max_seconds not in (None, "", 0) else None
            except (TypeError, ValueError):
                ms = None
            if ms is not None and ms <= 0:
                ms = None
            return dev.request_all_sqc_snapshots(max_seconds=ms, history_only=bool(history_only))

        def _cancel():
            dev = _msense_device(ip)
            if dev is None:
                return "⛔ MSense device not initialized"
            return dev.cancel_all_sqc_snapshots()

        btn_refresh_sqc.click(lambda: _update_sqc(ip), outputs=[sqc_status, sqc_plot])
        btn_request_sqc.click(_request, inputs=[sqc_max_s, sqc_hist_only], outputs=sqc_status)
        btn_cancel_sqc.click(_cancel, outputs=sqc_status)
        sqc_timer.tick(fn=lambda: _update_sqc(ip), outputs=[sqc_status, sqc_plot])


def _update_sqc(ip):
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
        results.append((disp, result))
        prov = result.get("provenance") or {}
        dirty = " ⚠️dirty" if prov.get("git_tree_state") == "dirty" else ""
        quick = ""
        if result.get("quick_seconds"):
            ph = prov.get("phase_at_cancel", "")
            quick = f" · ✂ quick {result['quick_seconds']:g}s{' (' + ph + ')' if ph else ''}"
        lines.append(
            f"- **{disp}** — ✅ {result['device_type']}{quick} · id `{prov.get('device_id', '?')}` · "
            f"fw `{str(prov.get('git_commit', '?'))[:10]}`{dirty} · saved `{status['saved_path']}`"
        )

    if caps_note and "NUS unavailable" in caps_note:
        lines.append(f"\n_{caps_note}_")

    fig = _build_sqc_figure(results) if results else gr.update()
    return "\n".join(lines), fig


def _build_sqc_figure(results):
    # one row per (unit x channel); raw on the primary y-axis, a light
    # bandpass overlay on a secondary y-axis so the pulsatile component
    # stays visible next to the large DC term. While a capture is still
    # streaming in ("partial") we plot the raw trace only — filtering a
    # still-growing signal just adds edge artifacts.
    rows = []
    for name, result in results:
        partial = result.get("partial")            # live preview, still streaming
        quick_s = result.get("quick_seconds")      # finished but cut short on purpose
        for ch_name, y in result["channels"].items():
            title = f"{name} · {ch_name}"
            if partial:
                title += " — receiving…"
            elif quick_s:
                title += f" — {quick_s:g}s quick"
            rows.append((title, np.asarray(y, dtype=float), result["fs"],
                         result["device_type"] == "ECG", partial))

    fig = make_subplots(
        rows=len(rows), cols=1, shared_xaxes=False, vertical_spacing=0.06 / max(len(rows), 1),
        subplot_titles=[title for title, *_ in rows],
        specs=[[{"secondary_y": True}] for _ in rows],
    )
    for i, (title, y, fs, is_ecg, partial) in enumerate(rows, start=1):
        t = np.arange(len(y)) / fs
        fig.add_trace(go.Scatter(x=t, y=y, mode="lines", name="raw",
                                 line=dict(color="#1f77b4")), row=i, col=1, secondary_y=False)
        if not partial and len(y) > 64:
            try:
                filt = filter_ecg(y, fs) if is_ecg else filter_ppg(y, fs)
                fig.add_trace(go.Scatter(x=t, y=filt, mode="lines", name="filtered",
                                         line=dict(color="#d62728", width=1), opacity=0.7),
                              row=i, col=1, secondary_y=True)
            except Exception:
                pass
        fig.update_xaxes(title_text="Time (s)", row=i, col=1)

    fig.update_layout(
        height=200 * max(len(rows), 1),
        margin=dict(l=50, r=20, t=30, b=30),
        showlegend=False,
        uirevision="plasma-sqc",
    )
    return fig
