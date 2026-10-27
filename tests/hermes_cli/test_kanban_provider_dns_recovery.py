"""Captured DirectSDK DNS outage uses the native neutral worker recovery path."""
from pathlib import Path
import subprocess

import pytest

from agent.error_classifier import classify_api_error, FailoverReason
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as kbd
from hermes_cli.turn_exit import turn_exit_code

PROVIDER = "claude-subscription-directsdk-experimental"
MESSAGE = ("Incomplete upstream response (first upstream attempt: status None, capture incomplete, "
           "relay failure gaierror, native retries denied: 0): API Error: Connection dropped (ECONNRESET)")


class ClaudeAPIError(Exception):
    status_code = None


@pytest.mark.parametrize("message", [MESSAGE, MESSAGE.split(": API Error:")[0]])
def test_exact_dns_envelope_is_transport_even_for_large_context(message):
    result = classify_api_error(ClaudeAPIError(message), provider=PROVIDER, approx_tokens=100000)
    assert result.reason == FailoverReason.timeout
    assert not result.should_compress
    assert not result.should_rotate_credential
    assert turn_exit_code({"failed": True, "failure_reason": result.reason.value}, kanban_worker=True) == 75


@pytest.mark.parametrize("error,provider", [
    (RuntimeError(MESSAGE), PROVIDER),
    (ClaudeAPIError(MESSAGE), "custom"),
    (ClaudeAPIError("A tool reported relay failure gaierror and ECONNRESET"), PROVIDER),
    (ClaudeAPIError(MESSAGE.replace("gaierror", "ValueError")), PROVIDER),
])
def test_unproved_application_failures_remain_ordinary(error, provider):
    result = classify_api_error(error, provider=provider)
    assert result.reason == FailoverReason.unknown
    assert turn_exit_code({"failed": True, "failure_reason": result.reason.value}, kanban_worker=True) == 1


def test_dns_worker_requeues_same_card_without_spending_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kbd._recent_worker_exits.clear()
    failure = classify_api_error(ClaudeAPIError(MESSAGE), provider=PROVIDER)
    code = turn_exit_code({"failed": True, "failure_reason": failure.reason.value}, kanban_worker=True)
    with kbc.connect_closing(tmp_path / "kanban.db") as conn:
        tid = kb.create_task(conn, title="Existing implementation", assignee="forge")
        host = kb._claimer_id().split(":", 1)[0]
        task = kb.claim_task(conn, tid, claimer=f"{host}:owner")
        proc = subprocess.Popen(["true"])
        proc.wait()
        kbd._set_worker_pid(conn, tid, proc.pid)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "worker_bound", {"pid": proc.pid, "claim_lock": task.claim_lock}, run_id=task.current_run_id)
            kb._append_event(conn, tid, "worker_result", {"pid": proc.pid, "claim_lock": task.claim_lock,
                             "exit_code": code, "last_output": MESSAGE}, run_id=task.current_run_id)
        assert tid not in kbd.detect_crashed_workers(conn)
        card = kb.get_task(conn, tid)
        assert card.status == "ready"
        assert card.consecutive_failures == 0
        assert conn.execute("SELECT outcome FROM task_runs WHERE id=?", (task.current_run_id,)).fetchone()[0] == "rate_limited"
        assert kbd.check_respawn_guard(conn, tid) == "rate_limit_cooldown"


@pytest.mark.parametrize("message,provider,expected", [(MESSAGE, PROVIDER, "dns"), ("request timed out", PROVIDER, None),
                                                       (MESSAGE, "custom", None)])
def test_retry_exhaustion_carries_only_proved_transport(message, provider, expected):
    from agent.turn_recovery import max_retries_exhausted_result
    from tests.agent.test_failed_turn_chat_copy import _Agent
    error = ClaudeAPIError(message)
    classified = classify_api_error(error, provider=provider)
    result = max_retries_exhausted_result(
        _Agent(), error, classified, attempts=3, is_rate_limited=False, error_msg=str(error).lower(),
        api_kwargs=None, api_messages=[], messages=[], conversation_history=None, api_call_count=1,
        approx_tokens=10, provider=provider, base_url="process://claude-subscription-directsdk-experimental", model="m",
    )
    assert result.get("failure_transport") == expected
