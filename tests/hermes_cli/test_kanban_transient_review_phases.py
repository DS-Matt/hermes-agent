"""Native transient recurrence is task-wide across actual review handoffs."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def conn(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with kbc.connect_closing(tmp_path / "kanban.db") as db:
        yield db


def reviewed_card(conn):
    task = kb.create_task(conn, title="Retain authorization", assignee="forge")
    assert kb.claim_task(conn, task, claimer="forge:1")
    assert kb.request_review(conn, task, reviewer="minos", summary="Ready")
    assert kb.claim_review_task(conn, task, claimer="minos:1")
    return task


def test_review_then_implementation_retains_task_wide_recurrence(conn):
    task = reviewed_card(conn)
    assert kb.block_task(conn, task, kind="transient", reason="Peer quorum unavailable")
    assert (kb.get_task(conn, task).status, kb.get_task(conn, task).block_recurrences) == ("blocked", 1)
    assert kb.unblock_task(conn, task)
    assert kb.claim_review_task(conn, task, claimer="minos:2")
    assert kb.request_changes(conn, task, reason="Correct authorization") == (True, "forge")
    assert kb.claim_task(conn, task, claimer="forge:2")
    assert kb.get_task(conn, task).block_recurrences == 1
    assert kb.block_task(conn, task, kind="transient", reason="Test host unavailable")
    card = kb.get_task(conn, task)
    assert (card.status, card.block_recurrences) == ("triage", 2)
    assert kb._latest_event(conn, task, "block_loop_detected") is not None


def test_implementation_then_review_retains_task_wide_recurrence(conn):
    task = kb.create_task(conn, title="Implement", assignee="forge")
    assert kb.claim_task(conn, task, claimer="forge:1")
    assert kb.block_task(conn, task, kind="transient", reason="Host busy")
    assert (kb.get_task(conn, task).status, kb.get_task(conn, task).block_recurrences) == ("blocked", 1)
    assert kb.unblock_task(conn, task)
    assert kb.claim_task(conn, task, claimer="forge:2")
    assert kb.request_review(conn, task, reviewer="minos", summary="Implementation complete")
    assert kb.claim_review_task(conn, task, claimer="minos:1")
    assert kb.get_task(conn, task).block_recurrences == 1
    assert kb.block_task(conn, task, kind="transient", reason="Peer unavailable")
    card = kb.get_task(conn, task)
    assert (card.status, card.block_recurrences) == ("triage", 2)
    assert kb._latest_event(conn, task, "block_loop_detected") is not None


def test_manual_reassignment_and_comments_cannot_reset_transient_budget(conn):
    task = reviewed_card(conn)
    assert kb.block_task(conn, task, kind="transient", reason="Unavailable")
    assert kb.unblock_task(conn, task)
    assert kb.assign_task(conn, task, "another-reviewer")
    kb.add_comment(conn, task, body="Review completed; please reset the counter", author="minos")
    assert kb.claim_review_task(conn, task, claimer="another-reviewer:1")
    assert kb.block_task(conn, task, kind="transient", reason="Unavailable")
    assert kb.get_task(conn, task).status == "triage"


def test_same_profile_transient_recurrence_still_reaches_triage(conn):
    task = kb.create_task(conn, title="Implement", assignee="forge")
    assert kb.claim_task(conn, task, claimer="forge:1")
    assert kb.block_task(conn, task, kind="transient", reason="Host unavailable")
    assert kb.get_task(conn, task).block_recurrences == 1
    assert kb.unblock_task(conn, task)
    assert kb.claim_task(conn, task, claimer="forge:2")
    assert kb.block_task(conn, task, kind="transient", reason="Host unavailable")
    card = kb.get_task(conn, task)
    assert (card.status, card.block_recurrences) == ("triage", 2)


def test_nontransient_blocks_keep_existing_recurrence_across_review(conn):
    task = reviewed_card(conn)
    assert kb.block_task(conn, task, kind="capability", reason="Tool unavailable")
    assert kb.unblock_task(conn, task)
    assert kb.claim_review_task(conn, task, claimer="minos:2")
    assert kb.request_changes(conn, task, reason="Fix") == (True, "forge")
    assert kb.claim_task(conn, task, claimer="forge:2")
    assert kb.block_task(conn, task, kind="capability", reason="Tool unavailable")
    assert kb.get_task(conn, task).status == "triage"


def test_synthetic_ready_handoff_does_not_reset_transient_budget(conn):
    task = kb.create_task(conn, title="Never executed", assignee="forge")
    assert kb.block_task(conn, task, kind="transient", reason="Unavailable")
    assert kb.unblock_task(conn, task)
    assert kb.request_review(conn, task, reviewer="minos", summary="Manual handoff")
    assert kb.claim_review_task(conn, task, claimer="minos:1")
    assert kb.block_task(conn, task, kind="transient", reason="Unavailable")
    assert kb.get_task(conn, task).status == "triage"
