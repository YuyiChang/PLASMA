"""Native Gradio row editor with restricted participant-ID dropdowns.

Only structural actions re-render rows; typing does not create polling work or
touch drivers. Every structural action reads all current controls first.
"""
import re
import uuid

import gradio as gr

from .config import SUBJECT_OVERRIDE, OVERRIDE_ENABLED, subject_override

PARTICIPANT_IDS = "participant_ids"
INHERIT = "__dashboard__"
ROW_KEY = "_editor_key"


def parse_participant_ids(value):
    """A reusable, ordered list; preserve leading zeroes and remove duplicates."""
    values = re.split(r"[,;\s]+", value.strip()) if isinstance(value, str) else (value or [])
    result = []
    for value in values:
        participant = subject_override(value)
        if participant and participant not in result:
            result.append(participant)
    return result


def initial_participant_ids(blob, records):
    # Older configs have only per-row IDs. Make them selectable on migration,
    # including an assigned ID omitted from a manually edited/imported list.
    return parse_participant_ids([*parse_participant_ids(blob.get(PARTICIPANT_IDS, [])),
                                  *(r.get(SUBJECT_OVERRIDE, "") for r in records)])


def validate_selections(records, participants):
    missing = {subject_override(r.get(SUBJECT_OVERRIDE)) for r in records} - {"", *participants}
    if missing:
        raise ValueError("Participant IDs still assigned to wristbands: " + ", ".join(sorted(missing))
                         + ". Select another ID or Use dashboard Subject ID before removing them.")


def keyed_records(records):
    return [{**r, ROW_KEY: r.get(ROW_KEY) or uuid.uuid4().hex} for r in records]


def build_wristband_editor(host, plugin_id, devices, columns, normalize, new_record,
                           apply_label, *, choices=None, scan_action=None):
    """Build one editor for real or simulated wristbands.

    ``scan_action`` optionally supplies (button, input_components, merge_fn).
    merge_fn receives its extra inputs followed by the current row dictionaries.
    """
    choices = choices or {}
    blob = host.get_plugin_config(plugin_id)
    participants = initial_participant_ids(blob, devices)
    override_enabled = gr.Checkbox(bool(blob.get(OVERRIDE_ENABLED, False)),
                                  label="Enable participant ID overrides", interactive=True)
    participant_text = gr.Textbox(
        value=", ".join(participants), label="Available participant IDs",
        interactive=True,
        placeholder="1001, 1002, 0007",
        info="Four digits per ID, separated by commas or newlines. Update choices, then Apply to save.")
    update_choices = gr.Button("Update ID choices")
    rows_state = gr.State(keyed_records(devices))
    ids_state = gr.State(participants)
    with gr.Row():
        add = gr.Button("➕ Add wristband" if plugin_id == "msense" else "➕ Add demo wristband")
        apply = gr.Button(apply_label, variant="primary")
    status = gr.Textbox(interactive=False, value="", show_label=False, container=False)

    @gr.render(inputs=[rows_state, ids_state], show_progress="hidden")
    def render_rows(rows, pool):
        controls, remove_buttons = [], []
        with gr.Column(elem_id=f"{plugin_id}-wristband-rows"):
            if not rows:
                gr.Markdown("No wristbands configured. Add a wristband to begin.")
            for row in rows:
                key = row[ROW_KEY]
                with gr.Row():
                    for column in columns:
                        value = row.get(column, "")
                        revision = row.get("_name_revision", "") if column == "Name" else ""
                        options = dict(label=column, key=f"{key}-{column}-{revision}",
                                       min_width=200, interactive=True)
                        if column == SUBJECT_OVERRIDE:
                            options.update(min_width=200, scale=2)
                            control = gr.Dropdown(
                                choices=[("Use dashboard Subject ID", INHERIT), *pool],
                                value=value or INHERIT, allow_custom_value=False, **options)
                        elif column in ("Enabled", "IMU Stream"):
                            options.update(min_width=90, scale=0)
                            control = gr.Checkbox(bool(value), **options)
                        elif column in choices:
                            if column == "Sensor":
                                options.update(min_width=90, scale=0)
                            control = gr.Dropdown(choices=choices[column], value=value,
                                                  allow_custom_value=False, **options)
                        else:
                            if column == "UUID / MAC Address":
                                options.update(min_width=140, scale=0)
                            control = gr.Textbox(str(value), **options)
                        controls.append(control)
                    remove_buttons.append(gr.Button("Remove", scale=0, min_width=75,
                                                    key=f"{key}-remove"))

        def collect(values):
            width = len(columns)
            result = []
            for index, row in enumerate(rows):
                record = dict(zip(columns, values[index * width:(index + 1) * width]))
                if record[SUBJECT_OVERRIDE] == INHERIT:
                    record[SUBJECT_OVERRIDE] = ""
                result.append({**row, **record})
            return result

        def update_ids(text, *values):
            records = collect(values)
            try:
                ids = parse_participant_ids(text)
                validate_selections(records, ids)
            except ValueError as exc:
                raise gr.Error(str(exc)) from exc
            return records, ids, "ID choices updated — press Apply to save."

        def save(text, enabled, *values):
            raw_records = collect(values)
            try:
                ids = parse_participant_ids(text)
                records = normalize(raw_records)
                validate_selections(records, ids)
            except ValueError as exc:
                raise gr.Error(str(exc)) from exc
            host.update_plugin_config(plugin_id, {**host.get_plugin_config(plugin_id),
                "devices": records, PARTICIPANT_IDS: ids, OVERRIDE_ENABLED: bool(enabled)})
            count = sum(bool(r.get("Enabled", True)) for r in records)
            message = f"Saved — {count}/{len(records)} wristband(s) enabled"
            if plugin_id == "msense":
                addresses = [r["UUID / MAC Address"].strip().upper() for r in records if r["Enabled"]]
                duplicates = sorted({a for a in addresses if addresses.count(a) > 1})
                if duplicates:
                    message += f" — ⚠️ {', '.join(duplicates)} listed more than once; last row is used"
            return raw_records, ids, message

        def add_row(*values):
            records = collect(values)
            return keyed_records([*records, new_record(len(records))]), "Added — press Apply to save."

        def removal(index):
            def remove_row(*values):
                records = collect(values)
                return records[:index] + records[index + 1:], "Removed — press Apply to save."
            return remove_row

        update_choices.click(update_ids, [participant_text, *controls],
                             [rows_state, ids_state, status], show_progress="hidden")
        apply.click(save, [participant_text, override_enabled, *controls],
                    [rows_state, ids_state, status], show_progress="hidden")
        add.click(add_row, controls, [rows_state, status], show_progress="hidden")
        for index, button in enumerate(remove_buttons):
            button.click(removal(index), controls, [rows_state, status], show_progress="hidden")
        if scan_action:
            button, extra_inputs, merge = scan_action

            def merge_scanned(*values):
                extra_count = len(extra_inputs)
                previous = collect(values[extra_count:])
                names = {r[ROW_KEY]: r.get("Name") for r in previous}
                records, message = merge(*values[:extra_count], previous)
                # Preserve local edits on ordinary re-renders. A scan's explicit
                # advertised-name refresh is the one field that must be replaced.
                records = [{**r, "_name_revision": uuid.uuid4().hex}
                           if r.get(ROW_KEY) in names and r.get("Name") != names[r[ROW_KEY]]
                           else r for r in records]
                return keyed_records(records), message

            button.click(merge_scanned, [*extra_inputs, *controls],
                         [rows_state, status], show_progress="hidden")

    return status
