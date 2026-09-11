import atexit
import gradio as gr
import struct
import os
import sys
import html as _html
import re
from plasma.lsl_session import encode_participant, SessionInfo
from plasma.journal import open_journal_outlet
import logging, datetime, time
from logging import Logger
from plasma import plugins, __version__, build_info
from plasma.config import device_config
from plasma.app_context import app_context
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from plasma.plot_util import decimate_minmax

VISUALIZER_PLOT_ELEM_ID = "plasma-visualizer-plot"
MEMO_PANEL_ELEM_ID = "plasma-memo-panel"

# Device-status string -> presentation colour. Devices keep setting emoji glyphs
# on PlasmaMemo.sts (🟢/🟥/⛔ …); the failure-level model in plasma/status.py
# classifies them (internal L1-3 severity + a colour category). See
# docs/failure-levels.md — levels are internal, the operator only sees colours.
from plasma import status as _status
from plasma.status import CATEGORY_HEX as _STATUS_HEX
from plasma.api import match_recorder_streams


def _status_class(sts, phase=_status.COLLECTING, **kw):
    """Presentation category for a status string (key into _STATUS_HEX)."""
    return _status.render_class(sts, phase, **kw)


def _phase_of(session_sts):
    """SETUP / COLLECTING / STOPPED from the session status line — used when a
    caller doesn't pass an explicit phase."""
    low = (session_sts or "").lower()
    if "in progress" in low:
        return _status.COLLECTING
    if "stopped" in low:
        return _status.STOPPED
    return _status.SETUP


def _sts_detail(sts):
    """Human text of a status glyph string with the leading emoji removed
    ('⛔ connect failed' -> 'connect failed', '🟢' -> '')."""
    return re.sub(r"^[^\w]+", "", (sts or "").strip())


def _rec_stats(s):
    rate = f" @ {s['srate']:.0f} Hz" if s['srate'] else " (irregular)"
    return f"{s['n_samples']:,} samp{rate} → {s['xdf_basename']}"


def _fmt_elapsed(seconds):
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


_MEMO_CSS = (
    "<style>"
    f"#{MEMO_PANEL_ELEM_ID} .prose{{max-width:none}}"
    f"#{MEMO_PANEL_ELEM_ID} .m-wrap{{font:13px/1.5 ui-monospace,Menlo,Consolas,monospace}}"
    f"#{MEMO_PANEL_ELEM_ID} .m-hdr{{font-weight:600;margin:1px 0 5px}}"
    f"#{MEMO_PANEL_ELEM_ID} .m-row{{margin:4px 0}}"
    f"#{MEMO_PANEL_ELEM_ID} .m-name{{font-weight:600}}"
    f"#{MEMO_PANEL_ELEM_ID} .m-tag{{opacity:.6;font-weight:400;margin-left:7px}}"
    f"#{MEMO_PANEL_ELEM_ID} .m-sub{{color:#555;padding-left:18px;font-size:12px}}"
    f"#{MEMO_PANEL_ELEM_ID} .m-sub.rec{{color:{_STATUS_HEX['guidance']}}}"
    f"#{MEMO_PANEL_ELEM_ID} .m-sub.guidance{{color:{_STATUS_HEX['guidance']}}}"
    f"#{MEMO_PANEL_ELEM_ID} .m-dim{{opacity:.55}}"
    "</style>"
)


def build_memo_html(session_sts, session_info, devices, snap, ext_streams=(),
                    elapsed=None, phase=None):
    """Render the memo panel as an HTML string. `snap` is
    SessionRecorder.status() or None. `ext_streams` is a list of
    {"name","type","srate","host"} dicts for LSL streams found on the network
    that aren't PLASMA's own — shown only before recording starts (once `snap`
    is present the recorder lists every stream itself). `elapsed` is an
    "HH:MM:SS" collection-elapsed string or None. `phase` is SETUP / COLLECTING
    / STOPPED for the failure-level classifier (inferred from `session_sts`
    when None). All interpolated text is escaped — gr.HTML does no sanitisation."""
    esc = _html.escape
    if phase is None:
        phase = _phase_of(session_sts)
    out = [_MEMO_CSS, '<div class="m-wrap">']

    c = _STATUS_HEX[_status_class(session_sts, phase)]
    elapsed_html = f'<span class="m-tag">{esc(elapsed)}</span>' if elapsed else ''
    out.append(
        f'<div class="m-hdr" style="color:{c}">{esc(str(session_sts))}'
        f'<span class="m-tag">{esc(str(session_info["sub_id"]))} '
        f'{esc(str(session_info["ses_id"]))}</span>{elapsed_html}</div>'
        f'<div class="m-sub m-dim">{esc(str(session_info["log_dir"]))}</div>'
    )

    if snap is not None:
        rc = {"recording": "healthy", "stopped": "info",
              "unavailable": "warning"}.get(snap["state"], "info")
        out.append(
            f'<div class="m-row"><span class="m-name" style="color:{_STATUS_HEX[rc]}">'
            f'📼 {esc(snap["summary"])}</span>'
            f'<span class="m-tag">{esc(os.path.basename(snap["file"]) or "")}</span></div>'
        )

    # match the recorder's per-stream stats to the device that publishes each
    # stream (shared with plasma.api.session_status)
    recorded, orphans = match_recorder_streams(devices, snap)

    for dev in devices:
        memos = dev.memo.values() if isinstance(dev.memo, dict) else [dev.memo]
        for memo in memos:
            label = getattr(memo, "label", getattr(memo, "name", "?"))
            s = recorded.get(id(memo))
            # a device row escalates when its own recorded stream goes silent,
            # even if the driver never updated memo.sts
            hk = ({"stream_health": s["health"], "srate": s.get("srate", 0.0)}
                  if s is not None else {})
            c = _STATUS_HEX[_status_class(getattr(memo, "sts", ""), phase, **hk)]
            detail = _sts_detail(getattr(memo, "sts", ""))
            row = [f'<div class="m-row"><span class="m-name" style="color:{c}">'
                   f'{"📼 " if s is not None else ""}{esc(str(label))}</span>']
            if detail:
                row.append(f'<span class="m-tag">{esc(detail)}</span>')
            row.append('</div>')
            transition = getattr(memo, "transition", None)
            if transition:
                row.append(f'<div class="m-sub guidance">{esc(str(transition))}</div>')
            else:
                row.append(f'<div class="m-sub">{esc(str(getattr(memo, "latest", "")))}</div>')
            if s is not None:
                row.append(f'<div class="m-sub rec">{esc(_rec_stats(s))}</div>')
            out.append("".join(row))

    for s in orphans:
        lvl = _status.health_level(s["health"], s.get("srate", 0.0), phase=phase)
        cat = {_status.Level.L3: "warning", _status.Level.L2: "caution",
               _status.Level.L1: "info"}.get(lvl, "healthy")
        out.append(
            f'<div class="m-row"><span class="m-name" style="color:{_STATUS_HEX[cat]}">'
            f'📼🛰 {esc(s["name"])}</span></div>'
            f'<div class="m-sub rec">{esc(_rec_stats(s))}</div>'
        )

    if snap is None:
        for e in ext_streams:
            bits = [e.get("type") or "stream"]
            if e.get("srate"):
                bits.append(f"{e['srate']:.0f} Hz")
            if e.get("host"):
                bits.append(e["host"])
            out.append(
                f'<div class="m-row"><span class="m-name" style="color:{_STATUS_HEX["external"]}">'
                f'🛰 {esc(e["name"])}</span>'
                f'<span class="m-tag">{esc(" · ".join(bits))}</span></div>'
            )

    out.append('</div>')
    return "".join(out)


class IntegratedPanel():
    def __init__(self):
        self.device_list = list(device_config.get_active_table().keys())
        self.log_root = app_context().data_dir

        self.available_devices = []

        self.sts = "Welcome"

        self.logger = get_logger(self.log_root)
        self.logger.info(f"Begin PLASMA v{__version__} session log")
        # identify exactly what was running and where — the first thing a bug
        # report / support log needs (see plasma/build_info.py)
        self.logger.info(f"Build: commit {build_info.git_commit_hash()} "
                         f"| OS: {build_info.os_info()}")

        # session task-marker LSL stream (None if liblsl is unavailable)
        self.journal_outlet = open_journal_outlet()

        # built-in LSL -> XDF recorder (default on; see interface() checkbox).
        # Created fresh on each Start, kept after Stop so the memo panel can
        # show the final summary.
        self.record_lsl = True
        self.lsl_recorder = None

        # collection-elapsed clock shown in the memo header
        self._collection_started = None
        self._collection_stopped = None

        # background LSL resolver for the memo panel's pre-Start "external
        # streams seen on the network" rows. Started here so it has warmed up
        # by the time the panel is looked at; _external_lsl_streams() re-creates
        # it lazily if this fails (e.g. liblsl not ready yet).
        self._lsl_resolver = None
        try:
            import pylsl
            self._lsl_resolver = pylsl.ContinuousResolver()
        except Exception:
            pass

        # flush a running recording + drop device connections on any process
        # exit (Ctrl-C, window close, the Configuration Restart/Shut down
        # buttons, SIGTERM via plasma.__main__._handle_sigterm). MSense devices
        # register their own hook too; stop()/disconnect() are idempotent.
        atexit.register(self._atexit_cleanup)

    def _atexit_cleanup(self):
        rec = getattr(self, "lsl_recorder", None)
        if rec is not None:
            try:
                rec.stop()
            except Exception:
                pass
        for dev in list(getattr(self, "available_devices", []) or []):
            for m in ("stop", "disconnect"):
                try:
                    getattr(dev, m)()
                except Exception:
                    pass

    def journal(self, msg):
        """Push a task/flag marker onto the journaler LSL stream + the session log."""
        if self.journal_outlet is not None:
            self.journal_outlet.push_sample([msg])
        self.logger.info(f"JOURNAL: {msg}")

    def visualizer_interface(self):
        with gr.Column():
            gr.Markdown(
                "Live device signal viewer. Initialize device(s) on the **Session dashboard** tab first, "
                "then refresh sources here. X-axis uses each device's own sample counter, not computer clock time."
            )
            with gr.Row():
                source_select = gr.Dropdown(choices=[], multiselect=True, label="Data source(s)")
                channel_select = gr.CheckboxGroup(choices=[], label="Channel(s)")

            with gr.Row():
                btn_refresh_sources = gr.Button("🔄 Refresh sources")
                btn_fullscreen = gr.Button("⛶ Fullscreen")

            with gr.Column(elem_id=VISUALIZER_PLOT_ELEM_ID):
                plot = gr.Plot(show_label=False)

            # starts inactive — build_blocks() activates it only while the Data
            # Dashboard tab is on screen (see plasma/__main__.py). 2 Hz is
            # plenty for a live monitor and halves the figure churn vs 5 Hz.
            timer = gr.Timer(value=0.5, active=False)
            self._viz_timer = timer

            btn_refresh_sources.click(
                self.refresh_visual_sources, outputs=source_select
            ).then(
                self.refresh_channels, inputs=source_select, outputs=channel_select
            )
            source_select.change(self.refresh_channels, inputs=source_select, outputs=channel_select)
            timer.tick(fn=self.update_plot, inputs=[source_select, channel_select], outputs=plot)
            btn_fullscreen.click(
                None, None, None,
                js=f"""() => {{
                    const el = document.getElementById('{VISUALIZER_PLOT_ELEM_ID}');
                    if (el && el.requestFullscreen) {{ el.requestFullscreen(); }}
                }}""",
            )

    def get_visual_sources(self):
        """Flat {"device tag [· sub-source]": PlasmaMemo} map of every live
        source that currently has at least one data channel to plot.

        Two sub-sources can carry the same display label (e.g. two MSense
        wristbands never renamed from the factory default) — disambiguate the
        second and later with their own key so neither drops out of the map."""
        sources = {}
        for dev in self.available_devices:
            for name, memo in dev.get_sources().items():
                if not memo.channels:
                    continue
                label = getattr(memo, "label", name)
                if label in sources:
                    label = f"{label} · {name}"
                sources[label] = memo
        return sources

    def refresh_visual_sources(self):
        names = list(self.get_visual_sources().keys())
        return gr.Dropdown(choices=names, value=names)

    def find_device(self, cls):
        """First live device that is an instance of ``cls`` (plugin panels use
        this to reach their own driver), or None."""
        return next((d for d in self.available_devices if isinstance(d, cls)), None)

    def _groups_for(self, selected_sources):
        """Ordered {label: [channels]} for the selected sources. A device
        contributes related-channel groupings via ``memo.channel_groups``
        (label -> [channel names]); any channel in no group is its own group."""
        sources = self.get_visual_sources()
        grouped, order, in_group = {}, [], set()
        for name in selected_sources or []:
            memo = sources.get(name)
            if memo is None:
                continue
            for label, chs in getattr(memo, "channel_groups", {}).items():
                present = [c for c in chs if c in memo.channels]
                if not present:
                    continue
                if label not in grouped:
                    grouped[label] = []
                    order.append(label)
                for c in present:
                    if c not in grouped[label]:
                        grouped[label].append(c)
                    in_group.add(c)
        for name in selected_sources or []:
            memo = sources.get(name)
            if memo is None:
                continue
            for ch in memo.channels:
                if ch in in_group or ch in grouped:
                    continue
                grouped[ch] = [ch]
                order.append(ch)
        return order, grouped

    def refresh_channels(self, selected_sources):
        order, _ = self._groups_for(selected_sources)
        return gr.CheckboxGroup(choices=order, value=order)

    def update_plot(self, selected_sources, selected_groups):
        sources = self.get_visual_sources()
        _, grouped = self._groups_for(selected_sources)
        groups = selected_groups or []

        if not groups or not selected_sources:
            if getattr(self, "_viz_sig", None) == "idle":
                return gr.skip()
            self._viz_sig = "idle"
            fig = go.Figure()
            fig.update_layout(
                title="Select a data source and channel(s) to visualize",
                height=300,
                uirevision="plasma-visualizer",
            )
            return fig

        # Skip the rebuild + full re-serialisation when no plotted buffer has
        # advanced since the last tick. gr.Plot tears down and recreates its
        # Plotly <div> on every value it receives (gradio#10252), so an
        # unchanged figure costs real browser memory — same reason _render_memo
        # caches its HTML.
        sig = [tuple(selected_sources), tuple(groups)]
        for group in groups:
            for ch in grouped.get(group, [group]):
                for src_name in selected_sources:
                    memo = sources.get(src_name)
                    if memo is None or ch not in memo.channels:
                        continue
                    buf = memo.channels.get(ch)
                    sig.append((src_name, ch, len(buf) if buf else 0,
                                buf[-1][0] if buf else None))
        sig = tuple(sig)
        if getattr(self, "_viz_sig", None) == sig:
            return gr.skip()
        self._viz_sig = sig

        fig = make_subplots(rows=len(groups), cols=1, shared_xaxes=True, vertical_spacing=0.015)

        for row, group in enumerate(groups, start=1):
            for ch in grouped.get(group, [group]):
                for src_name in selected_sources:
                    memo = sources.get(src_name)
                    if memo is None or ch not in memo.channels:
                        continue
                    x, y = memo.get_series(ch)
                    x, y = decimate_minmax(x, y, max_points=4000)
                    fig.add_trace(go.Scatter(x=x, y=y, mode="lines", name=f"{src_name} · {ch}"), row=row, col=1)

            fig.update_yaxes(title_text=group, title_standoff=4, row=row, col=1)
            # only the bottom-most subplot needs an x-axis title/tick labels;
            # the rest just waste vertical space repeating the same axis
            if row == len(groups):
                fig.update_xaxes(title_text="Time since session start (s)", row=row, col=1)
            else:
                fig.update_xaxes(showticklabels=False, row=row, col=1)

        fig.update_layout(
            height=max(150 * len(groups), 200),
            margin=dict(l=50, r=20, t=10, b=30),
            showlegend=True,
            uirevision="plasma-visualizer",
        )
        return fig

    def interface(self):
        with gr.Row():
            with gr.Column():
                with gr.Accordion(label="Session info", open=True):
                    default_sub = "sub-1000"
                    default_ses = "ses-00"

                    with gr.Row():
                        sub_name = gr.Text(default_sub, label="Subject ID", info="Format: sub-XXXX, X is integer")
                        ses_name = gr.Text(default_ses, label="Session ID", info="Format: ses-YY, Y is integer")
                        subject_enc = gr.Number(self.get_participant_encoding(default_sub, default_ses), label='Participant encoding (Read-only)', interactive=False,
                                                info="Format: XXXXYY")
                        sub_name.change(self.get_participant_encoding, inputs=[sub_name, ses_name], outputs=subject_enc)
                        ses_name.change(self.get_participant_encoding, inputs=[sub_name, ses_name], outputs=subject_enc)
                        _ = self.get_participant_encoding(default_sub, default_ses)


                with gr.Accordion(label="Device initialization", open=True):
                    device_grp = gr.CheckboxGroup(choices=self.device_list, value=self.device_list, label="Select sensor(s)")

                    self.chk_record = gr.Checkbox(
                        value=self.record_lsl,
                        label="📼 Record all LSL streams to XDF",
                        info="While a session runs, resolves every LSL stream "
                                "on the network (PLASMA's own included) and saves "
                                "them to one .xdf file — no LabRecorder needed.")
                    self.chk_record.change(self._set_record_lsl, inputs=self.chk_record)
                    
                    with gr.Row():
                        btn_init = gr.Button("🚦Initialize selected device(s)")
                        btn_refresh = gr.Button("Refresh list")

                    btn_init.click(self.init_devices, inputs=device_grp)
                    btn_refresh.click(self._refresh_devices, outputs=device_grp)

            with gr.Column():
                with gr.Accordion(label="Device control", open=True):
                
                    with gr.Row():
                        self.btn_start = gr.Button("Start▶️")
                        self.btn_stop = gr.Button("Stop🛑")
                                        
                    self.btn_start.click(self.start_collection)
                    self.btn_stop.click(self.stop_collection)

                    

                with gr.Accordion(label="🗒️ Journaler", open=False):
                    j_free = gr.Text(label="Marker text", placeholder="free text",
                                     show_label=False)
                    with gr.Row():
                        j_send = gr.Button("✍️ Send marker", variant="primary")
                        j_flag = gr.Button("🚩 Flag", scale=0)

                    j_send.click(self._record_journal, inputs=j_free, outputs=j_free)
                    j_flag.click(self._flag_journal)

                with gr.Accordion(label="ℹ️ Memo", open=True):
                    self.memo_html = gr.HTML(self._render_memo(),
                                            elem_id=MEMO_PANEL_ELEM_ID, padding=False)
                    timer = gr.Timer(value=1)
                    timer.tick(fn=self._render_memo, outputs=self.memo_html,
                            show_progress="hidden")

        # with gr.Accordion("Help", open=False):
        #     with open("./plasma/help.md") as f:
        #         help_txt = f.read()
        #         # print(help_txt)
        #     md = gr.Markdown(help_txt)

    def _refresh_devices(self):
        active = list(device_config.get_active_table().keys())
        return gr.CheckboxGroup(choices=active, value=active)

    def init_devices(self, selected_devices):
        self.sts = "Initializing..."
        for dev in self.available_devices:
            try:
                dev.stop()
            except Exception as e:
                self.logger.warning(f"Error stopping previous device before reinit: {e}")
            try:
                dev.disconnect()
            except Exception as e:
                self.logger.warning(f"Error disconnecting previous device before reinit: {e}")

        self.available_devices = []
        active_table = device_config.get_active_table()
        for dev in selected_devices:
            plugin = active_table[dev]
            print(dev, plugin)
            Device = plugins.load_device_class(plugin)
            device_instance = Device(self.session_info, self.logger, tag=dev)
            device_instance.journal_hook = self.journal   # SQC start/end markers, etc.
            self.available_devices.append(device_instance)

        self.sts = "Ready to start"


    def _set_record_lsl(self, value):
        self.record_lsl = bool(value)

    def _start_recorder(self):
        """Spin up a fresh SessionRecorder for this run. Never blocks Start:
        any failure becomes a gr.Warning and leaves self.lsl_recorder = None."""
        if self.lsl_recorder is not None:
            try:
                self.lsl_recorder.stop()
            except Exception as e:
                self.logger.warning(f"Error stopping previous LSL recorder: {e}")
            self.lsl_recorder = None

        session_dir = getattr(self, "session_dir", None) or os.path.join(
            self.session_info["log_dir"],
            f"{self.session_info['participant_enc']}_{time.strftime('%y%m%d_%H%M%S')}")
        os.makedirs(session_dir, exist_ok=True)
        try:
            from plasma.lsl_recorder import SessionRecorder
            recorder = SessionRecorder(logger=self.logger)
            if recorder.start(session_dir):
                self.lsl_recorder = recorder
                self.logger.info(
                    f"LSL recording -> {recorder.status()['file']}")
            else:
                gr.Warning("LSL recording unavailable (liblsl/pylsl missing?) "
                           "— session will not be captured to XDF.")
        except Exception as e:
            self.logger.error(f"Could not start LSL recorder: {e}")
            gr.Warning(f"LSL recording failed to start: {e}")
            self.lsl_recorder = None

    def start_collection(self):
        self.sts = "Starting..."
        self._collection_started = time.monotonic()
        self._collection_stopped = None
        self.session_dir = os.path.join(
            self.session_info["log_dir"],
            f"{self.session_info['participant_enc']}_{time.strftime('%y%m%d_%H%M%S')}")
        os.makedirs(self.session_dir, exist_ok=True)
        self.logger.info(f"Session dir: {self.session_dir}")
        for dev in self.available_devices:
            try:
                # re-point at the current session identity: SessionInfo is
                # rebuilt on every Subject/Session-ID edit, but the device only
                # snapshotted it at Initialize — without this a post-Initialize
                # ID change never reaches the wristband's participant-encoding
                # characteristic (it keeps writing the Initialize-time value).
                dev.session_info = self.session_info
                dev.session_dir = self.session_dir
                dev.start()
            except Exception as e:
                self.logger.error(f"Error starting device {dev.tag}: {e}")
        if self.record_lsl:
            self._start_recorder()
        self.sts = "Collection in progress"

    def stop_collection(self):
        self.sts = "Stopping..."
        for dev in self.available_devices:
            try:
                dev.stop()
            except Exception as e:
                self.logger.warning(f"Error stopping device {dev.tag}: {e}")
        if self.lsl_recorder is not None:
            try:
                self.lsl_recorder.stop()
                self.logger.info(
                    f"LSL recording stopped -> {self.lsl_recorder.status()['file']}")
            except Exception as e:
                self.logger.warning(f"Error stopping LSL recorder: {e}")
        self._collection_stopped = time.monotonic()
        self.sts = "Collection stopped"

    def _external_lsl_streams(self):
        """LSL streams currently on the network that aren't PLASMA's own — so
        the memo panel can show them before Start / Initialize. Cheap: reads a
        background pylsl.ContinuousResolver (created lazily). Returns [] once the
        recorder is running (its status() already lists every stream)."""
        if self.lsl_recorder is not None:
            return []
        try:
            import pylsl
            if getattr(self, "_lsl_resolver", None) is None:
                self._lsl_resolver = pylsl.ContinuousResolver()
            infos = self._lsl_resolver.results()
        except Exception:
            return []

        own = {app_context().journal_stream}
        for dev in self.available_devices:
            try:
                own.update(dev.lsl_streams().keys())
            except Exception:
                pass

        seen, out = set(), []
        for info in infos:
            try:
                name = info.name()
                if not name or name in own or name in seen:
                    continue
                seen.add(name)
                out.append({"name": name, "type": info.type(),
                            "srate": float(info.nominal_srate() or 0.0),
                            "host": info.hostname()})
            except Exception:
                pass
        out.sort(key=lambda e: e["name"])
        return out

    @property
    def phase(self):
        """SETUP (before Start) / COLLECTING / STOPPED — gates the failure-level
        classifier (a device reporting 'stopped' is a WARNING mid-collection but
        neutral once the operator has pressed Stop)."""
        if self._collection_started is None:
            return _status.SETUP
        return _status.STOPPED if self._collection_stopped is not None else _status.COLLECTING

    def _render_memo(self):
        """HTML for the memo panel. Returns gr.skip() when nothing changed since
        the last tick, so gr.HTML doesn't tear down + re-render its subtree
        (which reads as a 1 Hz blink)."""
        rec = self.lsl_recorder
        started = self._collection_started
        elapsed = None
        if started is not None:
            end = self._collection_stopped or time.monotonic()
            elapsed = _fmt_elapsed(end - started)
        html = build_memo_html(
            self.sts, self.session_info, self.available_devices,
            rec.status() if rec is not None else None,
            ext_streams=self._external_lsl_streams(), elapsed=elapsed,
            phase=self.phase)
        if html == getattr(self, "_memo_last", None):
            return gr.skip()
        self._memo_last = html
        return html


    def _record_journal(self, free_text):
        msg = (free_text or "").strip()
        if not msg:
            gr.Warning("Nothing to record — enter marker text first.")
            return gr.skip()
        self.journal(msg)
        gr.Info(f"Recorded: {msg}")
        return ""  # clear the box

    def _flag_journal(self):
        self.journal("[FLAG]")
        gr.Info("Recorded: [FLAG]")

    def get_participant_encoding(self, sub, ses):
        integer_representation = encode_participant(sub, ses)

        # print(name, integer_representation)
        self.participant_byte = struct.pack("<I", integer_representation)
        self.session_info = SessionInfo(
            sub_id=sub,
            ses_id=ses,
            participant_enc=integer_representation,
            log_root=self.log_root,
        )
        return integer_representation


def get_logger(log_dir="data"):
    # current YYMMDD
    now = datetime.datetime.now()
    date = now.strftime("%Y-%m-%d")

    # init logger
    logger = logging.getLogger(__name__)
    os.makedirs(log_dir, exist_ok=True)

    # Status/journal messages are full of glyphs (🔌 🟢 🔴 ⚠️ …) and other
    # non-ASCII characters (→). Without an explicit encoding, FileHandler's
    # open() falls back to the OS default text encoding — on Windows that's
    # a legacy ANSI codepage (e.g. cp1252), which can't represent most of
    # them. logging swallows the resulting UnicodeEncodeError per record
    # (handleError() dumps a traceback to stderr and drops the line), which
    # on Windows silently deleted every "JOURNAL: [FAULT]/[RECOVER]" line —
    # the whole fault-journal audit trail — from the log file. Forcing UTF-8
    # matches macOS/Linux (already UTF-8 by default) and guarantees no
    # record is ever dropped.
    file_handler = logging.FileHandler(
        os.path.join(log_dir, f"{date}_plasma_session.log"), encoding="utf-8")
    try:
        # StreamHandler() with no args defaults to stderr, in whatever
        # encoding the console already uses (still a legacy codepage on
        # Windows) — its codepage may still be unable to *render* a given
        # glyph. Degrade to a backslash escape there instead of raising;
        # this only changes error handling, not the stream's identity, so
        # it's safe even when stderr is a test runner's capture object.
        sys.stderr.reconfigure(errors="backslashreplace")
    except (AttributeError, ValueError):
        pass  # no .reconfigure() (e.g. some capture/redirect objects) — skip
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [%(levelname)s] %(message)s',
                        handlers=[
                            file_handler,
                            logging.StreamHandler(),
                        ])
    return logger