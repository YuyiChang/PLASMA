import gradio as gr
import struct
import os
import html as _html
import re
from plasma.lsl_session import encode_participant, SessionInfo
from plasma.journal import open_journal_outlet
import logging, datetime, time
from logging import Logger
from plasma import plugins, __version__
from plasma.config import device_config
from plasma.app_context import app_context
import plotly.graph_objects as go
from plotly.subplots import make_subplots

VISUALIZER_PLOT_ELEM_ID = "plasma-visualizer-plot"
MEMO_PANEL_ELEM_ID = "plasma-memo-panel"

# device-status string -> semantic colour. Devices keep setting emoji glyphs on
# PlasmaMemo.sts (🟢/🟥/⛔ …); the memo panel classifies them into name colours.
_STATUS_HEX = {
    "ok": "#15803d",    # green  = collecting
    "idle": "#1d4ed8",  # blue   = initialised / ready
    "warn": "#a16207",  # yellow = disconnected / stalled / calibrating
    "err": "#b91c1c",   # red    = stopped / error
    "ext": "#6b7280",   # grey   = external stream seen on the network (not recording yet)
}


def _status_class(sts):
    s = (sts or "").strip()
    low = s.lower()
    if s.startswith("🟢") or "reconnected" in low or "bias saved" in low or "in progress" in low:
        return "ok"
    if (any(g in s for g in ("⛔", "❌", "🚫", "🟥", "🛑"))
            or "fault" in low or "stopped" in low or "reconnect failed" in low):
        return "err"
    if (any(g in s for g in ("⚠️", "🎯", "🔌", "🧨"))
            or "calibrat" in low or "stall" in low or "erased" in low):
        return "warn"
    return "idle"


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
    f"#{MEMO_PANEL_ELEM_ID} .m-sub.rec{{color:#7c3aed}}"
    f"#{MEMO_PANEL_ELEM_ID} .m-dim{{opacity:.55}}"
    "</style>"
)


def build_memo_html(session_sts, session_info, devices, snap, ext_streams=(),
                    elapsed=None):
    """Render the memo panel as an HTML string. `snap` is
    SessionRecorder.status() or None. `ext_streams` is a list of
    {"name","type","srate","host"} dicts for LSL streams found on the network
    that aren't PLASMA's own — shown only before recording starts (once `snap`
    is present the recorder lists every stream itself). `elapsed` is an
    "HH:MM:SS" collection-elapsed string or None. All interpolated text is
    escaped — gr.HTML does no sanitisation."""
    esc = _html.escape
    out = [_MEMO_CSS, '<div class="m-wrap">']

    c = _STATUS_HEX[_status_class(session_sts)]
    elapsed_html = f'<span class="m-tag">{esc(elapsed)}</span>' if elapsed else ''
    out.append(
        f'<div class="m-hdr" style="color:{c}">{esc(str(session_sts))}'
        f'<span class="m-tag">{esc(str(session_info["sub_id"]))} '
        f'{esc(str(session_info["ses_id"]))}</span>{elapsed_html}</div>'
        f'<div class="m-sub m-dim">{esc(str(session_info["log_dir"]))}</div>'
    )

    if snap is not None:
        rc = {"recording": "ok", "stopped": "err",
              "unavailable": "err"}.get(snap["state"], "idle")
        out.append(
            f'<div class="m-row"><span class="m-name" style="color:{_STATUS_HEX[rc]}">'
            f'📼 {esc(snap["summary"])}</span>'
            f'<span class="m-tag">{esc(os.path.basename(snap["file"]) or "")}</span></div>'
        )

    name_to_memo = {}
    for dev in devices:
        try:
            src = dev.get_sources()
            for lsl_name, key in dev.lsl_streams().items():
                if key in src:
                    name_to_memo[lsl_name] = src[key]
        except Exception:
            pass

    recorded, orphans = {}, []
    if snap is not None:
        for s in snap["streams"]:
            m = name_to_memo.get(s["name"])
            if m is not None and not s["external"]:
                recorded[id(m)] = s
            else:
                orphans.append(s)

    for dev in devices:
        memos = dev.memo.values() if isinstance(dev.memo, dict) else [dev.memo]
        for memo in memos:
            label = getattr(memo, "label", getattr(memo, "name", "?"))
            s = recorded.get(id(memo))
            c = _STATUS_HEX[_status_class(getattr(memo, "sts", ""))]
            detail = _sts_detail(getattr(memo, "sts", ""))
            row = [f'<div class="m-row"><span class="m-name" style="color:{c}">'
                   f'{"📼 " if s is not None else ""}{esc(str(label))}</span>']
            if detail:
                row.append(f'<span class="m-tag">{esc(detail)}</span>')
            row.append('</div>')
            row.append(f'<div class="m-sub">{esc(str(getattr(memo, "latest", "")))}</div>')
            if s is not None:
                row.append(f'<div class="m-sub rec">{esc(_rec_stats(s))}</div>')
            out.append("".join(row))

    for s in orphans:
        cls = ("err" if s["health"].startswith("🔴")
               else "warn" if s["health"].startswith("🟡") else "ok")
        out.append(
            f'<div class="m-row"><span class="m-name" style="color:{_STATUS_HEX[cls]}">'
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
                f'<div class="m-row"><span class="m-name" style="color:{_STATUS_HEX["ext"]}">'
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

            timer = gr.Timer(value=0.2, active=True)

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
        source that currently has at least one data channel to plot."""
        sources = {}
        for dev in self.available_devices:
            for name, memo in dev.get_sources().items():
                if memo.channels:
                    # label = name if name == dev.tag else f"{dev.tag} · {name}"
                    label = getattr(memo, "label", name)
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
            fig = go.Figure()
            fig.update_layout(
                title="Select a data source and channel(s) to visualize",
                height=300,
                uirevision="plasma-visualizer",
            )
            return fig

        fig = make_subplots(rows=len(groups), cols=1, shared_xaxes=True, vertical_spacing=0.015)

        for row, group in enumerate(groups, start=1):
            for ch in grouped.get(group, [group]):
                for src_name in selected_sources:
                    memo = sources.get(src_name)
                    if memo is None or ch not in memo.channels:
                        continue
                    x, y = memo.get_series(ch)
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
        for dev in self.available_devices:
            try:
                dev.stop()
            except Exception as e:
                self.logger.info(f"Error stopping previous device before reinit: {e}")
            try:
                dev.disconnect()
            except Exception as e:
                self.logger.info(f"Error disconnecting previous device before reinit: {e}")

        self.available_devices = []
        active_table = device_config.get_active_table()
        for dev in selected_devices:
            plugin = active_table[dev]
            print(dev, plugin)
            Device = plugins.load_device_class(plugin)
            device_instance = Device(self.session_info, self.logger, tag=dev)
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
                self.logger.info(f"Error stopping previous LSL recorder: {e}")
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
            self.logger.info(f"Could not start LSL recorder: {e}")
            gr.Warning(f"LSL recording failed to start: {e}")
            self.lsl_recorder = None

    def start_collection(self):
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
                self.logger.info(f"Error starting device {dev.tag}: {e}")
        if self.record_lsl:
            self._start_recorder()
        self.sts = "Collection in progress"

    def stop_collection(self):
        for dev in self.available_devices:
            try:
                dev.stop()
            except Exception as e:
                self.logger.info(f"Error stopping device {dev.tag}: {e}")
        if self.lsl_recorder is not None:
            try:
                self.lsl_recorder.stop()
                self.logger.info(
                    f"LSL recording stopped -> {self.lsl_recorder.status()['file']}")
            except Exception as e:
                self.logger.info(f"Error stopping LSL recorder: {e}")
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
            ext_streams=self._external_lsl_streams(), elapsed=elapsed)
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
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [%(levelname)s] %(message)s',
                        handlers=[
                            logging.FileHandler(os.path.join(log_dir, f"{date}_plasma_session.log")),
                            logging.StreamHandler()
                        ])
    return logger