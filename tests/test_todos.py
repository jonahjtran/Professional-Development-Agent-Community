from pathlib import Path

from orchestrator.todos import (
    add_todo,
    all_open_todos,
    close_source,
    mark_done,
    open_todos,
)


def test_a_todo_is_saved_and_listed_as_open(tmp_path: Path) -> None:
    todo = add_todo(
        tmp_path, "demo", key="k1", title="Add the Apollo key", detail="In .env", source="developer"
    )

    [listed] = open_todos(tmp_path, "demo")

    assert listed.id == todo.id and listed.title == "Add the Apollo key"
    assert listed.source == "developer" and listed.done_at is None


def test_the_same_open_item_is_not_added_twice(tmp_path: Path) -> None:
    first = add_todo(tmp_path, "demo", key="k1", title="A", source="developer")
    second = add_todo(tmp_path, "demo", key="k1", title="A", source="developer")

    assert first.id == second.id and len(open_todos(tmp_path, "demo")) == 1


def test_marking_it_done_removes_it_from_the_open_list(tmp_path: Path) -> None:
    todo = add_todo(tmp_path, "demo", key="k1", title="A", source="developer")

    assert mark_done(tmp_path, "demo", todo.id) is True

    assert open_todos(tmp_path, "demo") == []
    assert mark_done(tmp_path, "demo", todo.id) is False  # already done
    assert mark_done(tmp_path, "demo", "nope") is False


def test_an_item_that_comes_up_again_after_being_done_is_raised_again(tmp_path: Path) -> None:
    todo = add_todo(tmp_path, "demo", key="k1", title="A", source="error")
    mark_done(tmp_path, "demo", todo.id)

    again = add_todo(tmp_path, "demo", key="k1", title="A", source="error")

    assert len(open_todos(tmp_path, "demo")) == 1 and again.done_at is None


def test_a_whole_source_can_be_closed_at_once(tmp_path: Path) -> None:
    add_todo(tmp_path, "demo", key="e1", title="paused", source="error")
    add_todo(tmp_path, "demo", key="d1", title="key", source="developer")

    assert close_source(tmp_path, "demo", "error") == 1

    assert [t.title for t in open_todos(tmp_path, "demo")] == ["key"]


def test_open_items_across_projects_are_listed_together(tmp_path: Path) -> None:
    add_todo(tmp_path, "one", key="a", title="A", source="qa")
    add_todo(tmp_path, "two", key="b", title="B", source="qa")

    assert {t.project for t in all_open_todos(tmp_path)} == {"one", "two"}


def test_a_damaged_file_means_no_items(tmp_path: Path) -> None:
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "todos.json").write_text("{nope")

    assert open_todos(tmp_path, "demo") == []
