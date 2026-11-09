from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from orchestrator.ui import chat_history
from orchestrator.ui.chat import ChatService
from tests.test_chat import FakeClient, hello, run, service

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset() -> None:
    FakeClient.instances = []
    FakeClient.script = []


@pytest.fixture
def runs(tmp_path: Path) -> Path:
    path = tmp_path / "runs"
    path.mkdir()
    return path


@pytest.fixture
def studio(tmp_path: Path) -> Path:
    root = tmp_path / "studio"
    root.mkdir()
    return root


def answer(text: str = "Hello") -> dict[str, Any]:
    return {"text": text, "html": f"<p>{text}</p>", "steps": ["Reading app.py"], "proposals": []}


# --- the stored chats -------------------------------------------------------------------------


def test_a_chat_is_stored_with_its_turns_and_titled_by_the_first_message(tmp_path: Path) -> None:
    chat_history.remember(
        tmp_path, "abc123abc123", repo="studio", tier="fast", project="demo", cost=0.02,
        you="Why did QA fail? " + "x" * 200, assistant=answer(), now=NOW,
    )  # fmt: skip
    chat_history.remember(
        tmp_path, "abc123abc123", repo="studio", tier="fast", project="demo", cost=0.05,
        you="And now?", assistant=answer("Because"), now=NOW,
    )  # fmt: skip

    stored = chat_history.load_chat(tmp_path, "abc123abc123")

    assert stored is not None
    assert stored["title"].startswith("Why did QA fail?") and len(stored["title"]) <= 60
    assert [t["role"] for t in stored["turns"]] == ["you", "assistant", "you", "assistant"]
    assert stored["cost_usd"] == 0.05 and stored["project"] == "demo" and stored["tier"] == "fast"


def test_chats_are_listed_newest_first_for_one_project(tmp_path: Path) -> None:
    later = datetime(2026, 11, 9, 13, 0, tzinfo=UTC)
    for sid, project, when in (
        ("a" * 12, "demo", NOW),
        ("b" * 12, "demo", later),
        ("c" * 12, "other", later),
    ):
        chat_history.remember(
            tmp_path, sid, repo="studio", tier="strong", project=project, cost=0,
            you=f"hello {sid[0]}", assistant=answer(), now=when,
        )  # fmt: skip

    listed = chat_history.list_chats(tmp_path, project="demo")

    assert [c["id"] for c in listed] == ["b" * 12, "a" * 12]
    assert listed[0]["title"] == "hello b" and listed[0]["turns"] == 2
    assert "turns_list" not in listed[0] and "html" not in str(listed[0])


def test_a_bad_id_or_a_missing_chat_is_not_found_and_never_reads_elsewhere(tmp_path: Path) -> None:
    assert chat_history.load_chat(tmp_path, "../../etc/passwd") is None
    assert chat_history.load_chat(tmp_path, "0" * 12) is None
    assert chat_history.delete_chat(tmp_path, "../x") is False


def test_a_damaged_file_is_skipped_not_fatal(tmp_path: Path) -> None:
    chat_history.remember(
        tmp_path, "a" * 12, repo="studio", tier="strong", project=None, cost=0,
        you="hi", assistant=answer(), now=NOW,
    )  # fmt: skip
    (tmp_path / "_studio" / "chats" / f"{'b' * 12}.json").write_text("{not json")

    assert [c["id"] for c in chat_history.list_chats(tmp_path)] == ["a" * 12]


def test_deleting_a_chat_removes_it(tmp_path: Path) -> None:
    chat_history.remember(
        tmp_path, "a" * 12, repo="studio", tier="strong", project=None, cost=0,
        you="hi", assistant=answer(), now=NOW,
    )  # fmt: skip

    assert chat_history.delete_chat(tmp_path, "a" * 12) is True
    assert chat_history.list_chats(tmp_path) == []


# --- the service keeps and resumes them -------------------------------------------------------


def test_every_finished_turn_is_kept(runs: Path, studio: Path, tmp_path: Path) -> None:
    FakeClient.script = [hello, hello]
    chat = service(runs, studio, tmp_path)

    first = run(chat, "What is this?", project="demo")
    run(chat, "And more?", session_id=first[0]["id"], project="demo")

    stored = chat_history.load_chat(runs, first[0]["id"])
    assert stored is not None and stored["project"] == "demo"
    assert [t["text"] for t in stored["turns"] if t["role"] == "you"] == [
        "What is this?",
        "And more?",
    ]
    assistant = [t for t in stored["turns"] if t["role"] == "assistant"]
    assert assistant[0]["html"].strip() == "<p>Hello</p>" and stored["cost_usd"] == pytest.approx(
        0.02
    )


def test_a_failed_turn_is_kept_with_its_error(runs: Path, studio: Path, tmp_path: Path) -> None:
    def fails(_c: FakeClient) -> list[Any]:
        raise RuntimeError("boom")

    FakeClient.script = [fails]
    events = run(service(runs, studio, tmp_path), "hi")

    stored = chat_history.load_chat(runs, events[0]["id"])
    assert stored is not None and "boom" in stored["turns"][-1]["error"]


def test_a_past_chat_continues_after_a_restart_with_what_was_said_before(
    runs: Path, studio: Path, tmp_path: Path
) -> None:
    FakeClient.script = [hello, hello]
    first = run(service(runs, studio, tmp_path), "Why did QA fail?")
    sid = first[0]["id"]

    restarted: ChatService = service(runs, studio, tmp_path)  # the app was restarted: no sessions
    again = run(restarted, "And the fix?", session_id=sid)

    assert again[0] == {"type": "session", "id": sid}
    prompt = FakeClient.instances[1].prompts[0]
    assert "Why did QA fail?" in prompt and "Hello" in prompt and prompt.endswith("And the fix?")
    # only the first message after the restart carries the earlier conversation
    FakeClient.script = [hello]
    run(restarted, "thanks", session_id=sid)
    assert FakeClient.instances[1].prompts[1] == "thanks"
    stored = chat_history.load_chat(runs, sid)
    assert stored is not None and len(stored["turns"]) == 6


def test_a_resumed_chat_keeps_its_cost_against_the_cap(
    runs: Path, studio: Path, tmp_path: Path
) -> None:
    chat_history.remember(
        runs, "a" * 12, repo="studio", tier="strong", project=None, cost=5.0,
        you="hi", assistant=answer(), now=NOW,
    )  # fmt: skip

    events = run(service(runs, studio, tmp_path), "more", session_id="a" * 12)

    assert events[0]["type"] == "error" and "cap" in events[0]["message"]


# --- stopping an answer -----------------------------------------------------------------------


def writing(_c: FakeClient) -> list[Any]:
    from tests.test_chat import HANG, stream

    return [stream("Hel"), stream("lo wor"), HANG]


async def stop_after_two_chunks(chat: ChatService, message: str, **kw: Any) -> list[dict[str, Any]]:
    import asyncio

    seen: list[dict[str, Any]] = []
    got_text = asyncio.Event()

    async def read() -> None:
        async for event in chat.turn(
            message, session_id=kw.get("session_id"), repo="studio", tier="strong",
            context=None, project="demo",
        ):  # fmt: skip
            seen.append(event)
            if sum(1 for e in seen if e["type"] == "text") == 2:
                got_text.set()

    task = asyncio.create_task(read())
    await asyncio.wait_for(got_text.wait(), 5)
    task.cancel()  # the person pressed Stop: the browser closed the stream
    await asyncio.gather(task, return_exceptions=True)
    return seen


def test_stopping_an_answer_interrupts_the_assistant_and_keeps_what_it_had_written(
    runs: Path, studio: Path, tmp_path: Path
) -> None:
    import asyncio

    FakeClient.script = [writing]
    chat = service(runs, studio, tmp_path)

    seen = asyncio.run(stop_after_two_chunks(chat, "Explain the budget"))

    assert FakeClient.instances[0].interrupted
    stored = chat_history.load_chat(runs, seen[0]["id"])
    assert stored is not None
    answer = stored["turns"][-1]
    assert answer["text"] == "Hello wor" and answer["stopped"] is True
    assert "Hello wor" in answer["html"]
    assert stored["turns"][0]["text"] == "Explain the budget"


def test_the_chat_keeps_working_after_a_stop_in_the_same_session(
    runs: Path, studio: Path, tmp_path: Path
) -> None:
    import asyncio

    FakeClient.script = [writing, hello]
    chat = service(runs, studio, tmp_path)

    async def both() -> list[dict[str, Any]]:
        seen = await stop_after_two_chunks(chat, "first")
        return [e async for e in chat.turn(
            "second", session_id=seen[0]["id"], repo="studio", tier="strong",
            context=None, project="demo",
        )]  # fmt: skip

    events = asyncio.run(both())

    assert events[-1]["type"] == "done"
    assert len(FakeClient.instances) == 1  # the same assistant session carried on


def test_the_cost_of_a_stopped_answer_still_counts(
    runs: Path, studio: Path, tmp_path: Path
) -> None:
    import asyncio

    from orchestrator.assistant_cost import assistant_totals

    FakeClient.script = [writing]
    chat = service(runs, studio, tmp_path)

    asyncio.run(stop_after_two_chunks(chat, "hi"))

    assert assistant_totals(runs, "_studio")["chat_usd"] == pytest.approx(0.01)
