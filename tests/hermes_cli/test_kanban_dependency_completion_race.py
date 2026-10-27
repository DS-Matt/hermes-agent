"""A dependency completing during its child's claim must not create a human wait."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing


@pytest.fixture
def board(monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "forge")
    kb.init_db()
    with connect_closing() as conn:
        yield conn


def claim(conn, title, **kwargs):
    tid = kb.create_task(conn, title=title, assignee="forge", **kwargs)
    return tid, kb.claim_task(conn, tid).current_run_id


@pytest.mark.parametrize("timing", ["after_block", "before_block", "before_link", "multiple_parents"])
def test_dependency_handoff_resumes_same_card_once(board, monkeypatch, tmp_path, timing):
    from tools import kanban_tools as kt

    conn = board
    parent, parent_run = claim(conn, "Scribe assessment")
    source, run = claim(conn, "Source", workspace_kind="worktree", branch_name="existing-draft", workspace_path=str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", source)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run))
    def complete():
        return kb.complete_task(conn, parent, result="Verified child links", expected_run_id=parent_run)

    if timing == "before_link":
        assert complete()
    assert json.loads(kt._handle_link({"parent_id": parent, "child_id": source}))["ok"]
    if timing == "multiple_parents":
        other, other_run = claim(conn, "Other assessment")
        kb.link_tasks(conn, other, source, expected_child_run_id=run)
        assert kb.complete_task(conn, other, result="Other result", expected_run_id=other_run)
    if timing == "before_block":
        assert complete()
    result = json.loads(kt._handle_block({"kind": "dependency", "reason": "Await Scribe report"}))
    assert result["ok"] and result["status"] == "todo" and result["block_kind"] == "dependency", result
    task = kb.get_task(conn, source)
    assert task.claim_lock is None and task.current_run_id is None
    assert kb.latest_run(conn, source).ended_at is not None
    if timing in ("after_block", "multiple_parents"):
        assert complete()
        assert kb.get_task(conn, source).status == "ready"  # complete_task wakes children itself
    kb.recompute_ready(conn)
    task = kb.get_task(conn, source)
    assert (task.status, task.assignee, task.branch_name, task.workspace_path) == (
        "ready", "forge", "existing-draft", str(tmp_path))
    assert (task.consecutive_failures, task.block_recurrences) == (0, 0)
    assert "Verified child links" in kb.build_worker_context(conn, source)
    assert any(e.kind == "dependency_wait" and e.run_id == run for e in kb.list_events(conn, source))
    next_run = kb.claim_task(conn, source).current_run_id
    assert next_run != run
    assert kb.block_task(conn, source, kind="dependency", expected_run_id=next_run)
    kb.recompute_ready(conn)
    task = kb.get_task(conn, source)
    assert (task.status, task.block_kind, task.block_recurrences) == ("blocked", "needs_input", 1)


@pytest.mark.parametrize("case", ["no_parent", "old_parent", "unrelated_completion", "ready", "foreign", "stale"])
def test_dependency_wait_preserves_sticky_and_ownership_guards(board, case):
    conn = board
    parent, parent_run = claim(conn, "Assessment")
    if case == "old_parent":
        assert kb.complete_task(conn, parent, result="Old result", expected_run_id=parent_run)
    source, run = claim(conn, "Source")
    if case not in ("no_parent", "unrelated_completion"):
        kb.link_tasks(conn, parent, source, expected_child_run_id=run)
    if case != "old_parent":
        assert kb.complete_task(conn, parent, result="New result", expected_run_id=parent_run)
    if case == "ready":
        assert kb.block_task(conn, source, kind="transient", expected_run_id=run)
        assert kb.unblock_task(conn, source)
        run = None
    if case == "stale":
        assert kb.block_task(conn, source, kind="transient", expected_run_id=run)
        assert kb.unblock_task(conn, source)
        kb.claim_task(conn, source)
    expected = parent_run if case == "foreign" else run
    before = kb.get_task(conn, source)
    ok = kb.block_task(conn, source, kind="dependency", expected_run_id=expected)
    if case in ("foreign", "stale"):
        assert not ok
        assert kb.get_task(conn, source) == before
    else:
        assert ok
        kb.recompute_ready(conn)
        task = kb.get_task(conn, source)
        assert (task.status, task.block_kind) == ("blocked", "needs_input")
