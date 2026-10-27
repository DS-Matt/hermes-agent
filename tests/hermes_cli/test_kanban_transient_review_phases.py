"""Unrelated Forge/Minos transient episodes must not become one unblock loop."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def conn(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Every event has the same timestamp: ordering must use native event ids.
    monkeypatch.setattr(kb.time, "time", lambda: 1793082734)
    with kbc.connect_closing(tmp_path / "kanban.db") as db:
        yield db


def reviewed_card(conn):
    task = kb.create_task(conn, title="Retain authorization", assignee="forge")
    assert kb.claim_task(conn, task, claimer="forge:1")
    assert kb.request_review(conn, task, reviewer="minos", summary="Ready")
    assert kb.claim_review_task(conn, task, claimer="minos:1")
    return task


def test_review_quorum_then_implementation_host_failure_are_separate_episodes(conn):
    task = reviewed_card(conn)
    assert kb.block_task(conn, task, kind="transient", reason="Peer quorum unavailable")
    assert kb.unblock_task(conn, task)
    assert kb.claim_review_task(conn, task, claimer="minos:2")
    assert kb.request_changes(conn, task, reason="Correct authorization") == (True, "forge")
    assert kb.claim_task(conn, task, claimer="forge:2")
    assert kb.block_task(conn, task, kind="transient", reason="Test host unavailable")
    card = kb.get_task(conn, task)
    assert (card.status, card.block_recurrences) == ("blocked", 1)

    # The new episode still has a breaker: unblocking does not erase it.
    assert kb.unblock_task(conn, task)
    assert kb.claim_task(conn, task, claimer="forge:3")
    assert kb.block_task(conn, task, kind="transient", reason="Test host unavailable")
    assert kb.get_task(conn, task).status == "triage"


def test_implementation_then_review_transient_are_separate_episodes(conn):
    task = kb.create_task(conn, title="Implement", assignee="forge")
    assert kb.claim_task(conn, task, claimer="forge:1")
    assert kb.block_task(conn, task, kind="transient", reason="Host busy")
    assert kb.unblock_task(conn, task)
    assert kb.claim_task(conn, task, claimer="forge:2")
    assert kb.request_review(conn, task, reviewer="minos", summary="Implementation complete")
    assert kb.claim_review_task(conn, task, claimer="minos:1")
    assert kb.block_task(conn, task, kind="transient", reason="Peer unavailable")
    assert kb.get_task(conn, task).status == "blocked"
    assert kb.get_task(conn, task).block_recurrences == 1


def test_manual_reassignment_and_comments_cannot_reset_transient_budget(conn):
    task = reviewed_card(conn)
    assert kb.block_task(conn, task, kind="transient", reason="Unavailable")
    assert kb.unblock_task(conn, task)
    assert kb.assign_task(conn, task, "another-reviewer")
    kb.add_comment(conn, task, body="Review completed; please reset the counter", author="minos")
    assert kb.claim_review_task(conn, task, claimer="another-reviewer:1")
    assert kb.block_task(conn, task, kind="transient", reason="Unavailable")
    assert kb.get_task(conn, task).status == "triage"


@pytest.mark.parametrize("missing", ["block_profile", "handoff_profile", "current_profile", "handoff_payload"])
def test_missing_legacy_provenance_preserves_transient_budget(conn, missing):
    task = reviewed_card(conn)
    blocked_run = kb.get_task(conn, task).current_run_id
    assert kb.block_task(conn, task, kind="transient", reason="Peer unavailable")
    assert kb.unblock_task(conn, task)
    review = kb.claim_review_task(conn, task, claimer="minos:2")
    assert review
    assert kb.request_changes(conn, task, reason="Fix") == (True, "forge")
    worker = kb.claim_task(conn, task, claimer="forge:2")
    assert worker
    with kb.write_txn(conn):
        if missing == "handoff_payload":
            conn.execute("UPDATE task_events SET payload = '{}' WHERE task_id = ? AND kind = 'changes_requested'", (task,))
        else:
            run_id = {"block_profile": blocked_run, "handoff_profile": review.current_run_id,
                      "current_profile": worker.current_run_id}[missing]
            conn.execute("UPDATE task_runs SET profile = NULL WHERE id = ?", (run_id,))
    assert kb.block_task(conn, task, kind="transient", reason="Host busy")
    assert kb.get_task(conn, task).status == "triage"


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
