from pathlib import Path

import pytest

from orchestrator.notes import add_note, consume_notes, pending_notes


def test_a_note_is_saved_and_listed_until_it_is_used(tmp_path: Path) -> None:
    note = add_note(tmp_path, "demo", "Use server actions, not API routes")

    assert [n.text for n in pending_notes(tmp_path, "demo")] == [
        "Use server actions, not API routes"
    ]
    consume_notes(tmp_path, "demo", [note.id])
    assert pending_notes(tmp_path, "demo") == []


def test_notes_are_per_project(tmp_path: Path) -> None:
    add_note(tmp_path, "one", "A")

    assert pending_notes(tmp_path, "two") == []


def test_only_the_named_notes_are_consumed(tmp_path: Path) -> None:
    first = add_note(tmp_path, "demo", "A")
    add_note(tmp_path, "demo", "B")

    consume_notes(tmp_path, "demo", [first.id])

    assert [n.text for n in pending_notes(tmp_path, "demo")] == ["B"]


def test_a_blank_or_huge_note_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        add_note(tmp_path, "demo", "   ")
    with pytest.raises(ValueError):
        add_note(tmp_path, "demo", "x" * 2001)


def test_a_damaged_file_means_no_notes(tmp_path: Path) -> None:
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "notes.json").write_text("{nope")

    assert pending_notes(tmp_path, "demo") == []
