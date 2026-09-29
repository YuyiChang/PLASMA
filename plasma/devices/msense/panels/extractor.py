"""Data extractor + Data extractor pro tabs, and the shared advanced-options
accordion (`ExtractionOptionsPanel`). Wraps the pure `..extract.pipeline`.
"""
import gradio as gr

from ..extract.options import (
    AC_FORMAT_CHOICES,
    CONFLICT_CHOICES,
    ECG_FORMAT_CHOICES,
    FORMAT_HELP,
    PANEL_FIELDS,
    PPG_FORMAT_CHOICES,
    ExtractionOptions,
)
from ..extract.pipeline import extract_dir, extract_zip, get_session_encoding


_SAVE_FORMATS = ("feather", "csv", "pickle")
_PREF_CONFIG_KEY = "msense_extraction"


def _saved_save_format():
    from plasma.config import device_config
    blob = device_config.get_plugin_config(_PREF_CONFIG_KEY)
    saved = blob.get("save_format") if isinstance(blob, dict) else None
    return saved if saved in _SAVE_FORMATS else ExtractionOptions().save_format


def _save_save_format(value):
    from plasma.config import device_config
    if value not in _SAVE_FORMATS:
        raise ValueError(f"Unsupported extraction save format: {value}")
    existing = device_config.get_plugin_config(_PREF_CONFIG_KEY)
    blob = dict(existing) if isinstance(existing, dict) else {}
    blob["save_format"] = value
    device_config.update_plugin_config(_PREF_CONFIG_KEY, blob)


class ExtractionOptionsPanel:
    """The advanced-options accordion. Build one inside each Blocks context.

        opts = ExtractionOptionsPanel(open=True)
        btn.click(opts.bind(fn), inputs=[in_dir, out_dir, ...] + opts.inputs)

    `bind` hands the wrapped function `options=ExtractionOptions(...)`.
    """

    FIELDS = PANEL_FIELDS

    def __init__(self, open=False, visible=True):
        with gr.Accordion("⚙️ Advanced extraction options", open=open, visible=visible) as accordion:
            self.accordion = accordion
            with gr.Row():
                self.legacy_fs = gr.Checkbox(False, label="(Uncommon) legacy sampling rate")
                self.save_format = gr.Radio(
                    _SAVE_FORMATS, value=_saved_save_format(), label="Save format",
                    info="feather (fast, binary) · csv (plain text, needed for clocksync) · "
                         "pickle · selection saved for future launches")
                self.save_format.change(_save_save_format, inputs=self.save_format,
                                        outputs=None)
                self.ignore_id_parsing = gr.Checkbox(False, label="Ignore subject/session ID parsing")
            with gr.Row():
                self.ppg_format = gr.Dropdown(PPG_FORMAT_CHOICES, value="auto",
                                              label="PPG record format", info="auto = detect from content")
                self.ac_format = gr.Dropdown(AC_FORMAT_CHOICES, value="auto",
                                             label="IMU record format", info="auto = detect from content")
                self.ecg_format = gr.Dropdown(ECG_FORMAT_CHOICES, value="auto",
                                              label="ECG record format", info="auto = detect from content")
            with gr.Row():
                self.validate_with_uuid = gr.Checkbox(False, label="Cross-check against uuid.txt")
                self.on_format_conflict = gr.Radio(CONFLICT_CHOICES, value="warn", label="On conflict",
                                                   info="Content wins unless trust_uuid")
                self.strict_ppg = gr.Checkbox(False, label="Strict record validation")
                self.force_new_format = gr.Checkbox(False, label="Assume v4.7.0+ (fallback only)")
                self.include_cdct = gr.Checkbox(
                    False, label="Include CDCT/Datetime",
                    info="PPG-device files only — start time is always in README.txt regardless")
            with gr.Accordion("Help", open=False):
                gr.Markdown(FORMAT_HELP)

    @property
    def inputs(self):
        return [getattr(self, name) for name in self.FIELDS]

    def bind(self, fn):
        n = len(self.FIELDS)

        def wrapper(*args):
            fixed, values = args[:-n], args[-n:]
            return fn(*fixed, options=ExtractionOptions(**dict(zip(self.FIELDS, values))))

        return wrapper

    def gate_on(self, checkbox):
        checkbox.change(lambda on: gr.Accordion(visible=bool(on)),
                        inputs=checkbox, outputs=self.accordion)


def build_extractor(ip=None):
    with gr.Column():
        gr.Markdown("Convert a folder of raw MSense `.bin` files to CSV.")
        in_dir = gr.Text(label="Input directory (folder of .bin files)")
        out_dir = gr.Text(label="Output directory")
        note = gr.Text("", label="Note")
        opts = ExtractionOptionsPanel(open=False)
        btn = gr.Button("Extract raw data", variant="primary")
        status = gr.Markdown()

        with gr.Accordion("Encoding mapping", open=False):
            gr.DataFrame(value=get_session_encoding(), label="session_table.csv")

        def _run(in_dir, out_dir, note, options=None):
            if not in_dir or not out_dir:
                return "⛔ set both an input and an output directory"
            report = extract_dir(in_dir, out_dir, note=note, options=options)
            gr.Info(report.summary())
            return report.summary()

        btn.click(opts.bind(_run), inputs=[in_dir, out_dir, note] + opts.inputs, outputs=status)


def build_extractor_pro(ip=None):
    with gr.Column():
        gr.Markdown("Drop a downloaded `<...>_msense.zip` — it is extracted per device and "
                    "handed back as `<name>_extracted.zip`.")
        in_file = gr.File(file_types=[".zip"], label="Downloaded msense zip")
        opts = ExtractionOptionsPanel(open=False)
        out = gr.DownloadButton(label="No data to download", interactive=False)

        def _run(in_file, options=None):
            if not in_file:
                return gr.DownloadButton(label="No data to download", interactive=False)
            import tempfile
            path = extract_zip(in_file, out_dir=tempfile.gettempdir(), options=options)
            if not path:
                return gr.DownloadButton(label="Nothing extracted", interactive=False)
            gr.Info("Extraction complete")
            return gr.DownloadButton(label="🎉 Download extracted data", value=path, interactive=True)

        in_file.change(opts.bind(_run), inputs=[in_file] + opts.inputs, outputs=out)
