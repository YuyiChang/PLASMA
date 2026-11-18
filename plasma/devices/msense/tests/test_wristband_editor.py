"""Participant-list migration and protection against accidental reassignment."""
import pytest

from plasma.devices.msense.config import SUBJECT_OVERRIDE
from plasma.devices.msense.wristband_editor import (
    parse_participant_ids, initial_participant_ids, validate_selections, PARTICIPANT_IDS,
)


def test_id_list_preserves_leading_zeroes_and_order():
    assert parse_participant_ids("1001, 0007\n1002;1001 0007") == ["1001", "0007", "1002"]


@pytest.mark.parametrize("text", ["123", "12345", "sub-1001", "1001,abc", "1001,7"])
def test_list_rejects_invalid_id_without_partial_update(text):
    with pytest.raises(ValueError, match="four digits"):
        parse_participant_ids(text)


def test_existing_assignments_seed_choices_without_changing_them():
    records = [{SUBJECT_OVERRIDE: "0007"}, {SUBJECT_OVERRIDE: ""}, {SUBJECT_OVERRIDE: "0007"}]
    assert initial_participant_ids({}, records) == ["0007"]
    assert initial_participant_ids({PARTICIPANT_IDS: ["1001"]}, records) == ["1001", "0007"]
    assert records[0][SUBJECT_OVERRIDE] == "0007"


def test_used_id_cannot_be_removed_until_wristband_uses_another_choice():
    with pytest.raises(ValueError, match="still assigned.*0007"):
        validate_selections([{SUBJECT_OVERRIDE: "0007"}], ["1001"])
    validate_selections([{SUBJECT_OVERRIDE: ""}, {SUBJECT_OVERRIDE: "1001"}], ["1001"])


def test_same_participant_can_be_selected_for_multiple_wristbands():
    validate_selections([{SUBJECT_OVERRIDE: "0007"}, {SUBJECT_OVERRIDE: "0007"}], ["0007"])
