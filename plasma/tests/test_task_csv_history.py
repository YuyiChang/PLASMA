import pytest

from plasma.task_csv_history import TaskCsvHistory


def csv_file(directory, name="tasks.csv", condition="Walk"):
    path = directory / name
    path.write_text(f"Condition,Move_Instruction,load\n{condition},Move slowly,65\n", encoding="utf-8")
    return path


def test_saved_copy_survives_original_cleanup_and_app_restart(tmp_path):
    source = csv_file(tmp_path)
    cache = tmp_path / "saved"
    identifier = TaskCsvHistory(cache).remember(source)
    source.unlink()
    history = TaskCsvHistory(cache)
    assert history.choices()[0][0].startswith("tasks.csv · ")
    assert history.load(identifier)[0].name == "Walk"
    assert history.load(identifier)[0].seconds == 65


def test_same_upload_is_deduplicated_but_new_contents_are_preserved(tmp_path):
    source = csv_file(tmp_path)
    history = TaskCsvHistory(tmp_path / "saved")
    first = history.remember(source)
    assert history.remember(source) == first
    assert len(history.choices()) == 1
    csv_file(tmp_path, condition="Rest")
    second = history.remember(source)
    assert first != second
    assert history.load(first)[0].name == "Walk"
    assert history.load(second)[0].name == "Rest"


def test_invalid_csv_does_not_replace_saved_history(tmp_path):
    source = csv_file(tmp_path)
    history = TaskCsvHistory(tmp_path / "saved")
    identifier = history.remember(source)
    source.write_text("wrong,columns\n1,2\n")
    with pytest.raises(ValueError):
        history.remember(source)
    assert [value for _, value in history.choices()] == [identifier]
    assert history.load(identifier)[0].name == "Walk"
    assert not list(history.directory.glob(".task-*"))


def test_history_is_bounded_and_never_removes_originals(tmp_path):
    history = TaskCsvHistory(tmp_path / "saved", limit=2)
    files = [csv_file(tmp_path, f"task-{i}.csv") for i in range(3)]
    ids = [history.remember(path) for path in files]
    assert [value for _, value in history.choices()] == ids[:0:-1]
    assert len(list(history.directory.glob("*.csv"))) == 2
    assert all(path.exists() for path in files)
    with pytest.raises(ValueError):
        history.load(ids[0])


def test_missing_cached_file_or_unknown_selection_cannot_load_arbitrary_paths(tmp_path):
    history = TaskCsvHistory(tmp_path / "saved")
    identifier = history.remember(csv_file(tmp_path))
    (history.directory / (identifier + ".csv")).unlink()
    assert history.choices() == []
    with pytest.raises(ValueError):
        history.load("../tasks.csv")


def test_corrupt_cached_csv_is_revalidated_before_loading(tmp_path):
    history = TaskCsvHistory(tmp_path / "saved")
    identifier = history.remember(csv_file(tmp_path))
    (history.directory / (identifier + ".csv")).write_text("Condition,Move_Instruction,load\nBad,,0\n")
    with pytest.raises(ValueError):
        history.load(identifier)
