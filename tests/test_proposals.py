from pathlib import Path

import pytest

from orchestrator.proposals import (
    create_proposal,
    get_proposal,
    list_proposals,
    set_status,
)


def make(tmp_path: Path, **over: object):  # type: ignore[no-untyped-def]
    fields = {
        "action": "set_autonomy",
        "args": {"level": "autonomous"},
        "summary": "Set demo to autonomous",
    }
    return create_proposal(tmp_path, "demo", **{**fields, **over})  # type: ignore[arg-type]


def test_a_proposal_is_pending_until_someone_decides(tmp_path: Path) -> None:
    proposal = make(tmp_path)

    assert proposal.status == "pending"
    assert get_proposal(tmp_path, "demo", proposal.id) == proposal


def test_pending_proposals_are_listed_across_projects(tmp_path: Path) -> None:
    make(tmp_path)
    create_proposal(tmp_path, "other", "resume_project", {}, "Resume other")

    assert {p.project for p in list_proposals(tmp_path)} == {"demo", "other"}
    assert [p.project for p in list_proposals(tmp_path, project="other")] == ["other"]


def test_deciding_moves_it_out_of_the_pending_list(tmp_path: Path) -> None:
    proposal = make(tmp_path)

    done = set_status(tmp_path, "demo", proposal.id, "confirmed", "Done.")

    assert done is not None and done.status == "confirmed" and done.result == "Done."
    assert list_proposals(tmp_path, status="pending") == []
    assert list_proposals(tmp_path, status="confirmed")[0].id == proposal.id


def test_a_proposal_that_is_not_pending_cannot_be_decided_again(tmp_path: Path) -> None:
    proposal = make(tmp_path)
    set_status(tmp_path, "demo", proposal.id, "dismissed", "")

    assert set_status(tmp_path, "demo", proposal.id, "confirmed", "") is None


def test_an_unknown_action_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        make(tmp_path, action="delete_everything")


def test_an_unknown_proposal_is_none(tmp_path: Path) -> None:
    assert get_proposal(tmp_path, "demo", "nope") is None
    assert set_status(tmp_path, "demo", "nope", "confirmed", "") is None


def test_a_damaged_file_means_no_proposals(tmp_path: Path) -> None:
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "proposals.json").write_text("{nope")

    assert list_proposals(tmp_path) == []
