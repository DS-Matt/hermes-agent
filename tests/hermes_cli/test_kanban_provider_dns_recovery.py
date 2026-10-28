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


@pytest.mark.parametrize("historical_scope", [False, True])
def test_dns_worker_requeues_same_card_without_spending_budget(tmp_path, monkeypatch, historical_scope):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kbd._recent_worker_exits.clear()
    monkeypatch.setattr(kb, "_resolve_rate_limit_cooldown_seconds", lambda: 300)
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda name: tmp_path / name)
    for name in ("forge", "independent"):
        home = tmp_path / name
        home.mkdir()
        (home / "config.yaml").write_text("model: {}\n")
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
            payload = {"pid": proc.pid, "claim_lock": task.claim_lock,
                       "exit_code": code, "last_output": MESSAGE}
            if historical_scope:
                # Already stored receipts remain valid but cannot cool another card.
                stat = (tmp_path / "forge/config.yaml").stat()
                payload["dns_transport_scope"] = {
                    "home": str(tmp_path / "forge"),
                    "config": [stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size],
                    "env": None, "model_override": None, "provider_override": None,
                }
            kb._append_event(conn, tid, "worker_result", payload, run_id=task.current_run_id)
        assert tid not in kbd.detect_crashed_workers(conn)
        card = kb.get_task(conn, tid)
        assert card.status == "ready"
        assert card.consecutive_failures == 0
        assert conn.execute("SELECT outcome FROM task_runs WHERE id=?", (task.current_run_id,)).fetchone()[0] == "rate_limited"
        assert kbd.check_respawn_guard(conn, tid) == "rate_limit_cooldown"

        fresh = kb.create_task(conn, title="Fresh same profile", assignee="forge")
        other = kb.create_task(conn, title="Fresh other profile", assignee="independent")
        monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda _: True)
        monkeypatch.setattr(kbd._kbw, "resolve_workspace", lambda *a, **k: tmp_path)
        spawns = []
        dispatched = kb.DispatchResult()
        for task_id in (tid, fresh, other):
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            kbd._dispatch_lane_task(
                conn, row, row["assignee"], dispatched, lane="ready", dry_run=False,
                ttl_seconds=None, board=None, failure_limit=3,
                spawn_fn=lambda task, workspace: spawns.append(task.id),
                per_profile_cap=None, per_profile_running={},
            )
        assert spawns == [fresh, other]
        assert dispatched.respawn_guarded == [(tid, "rate_limit_cooldown")]
        assert kb.get_task(conn, tid).status == "ready"
        for task_id in (tid, fresh, other):
            assert kb.get_task(conn, task_id).consecutive_failures == 0
        for task_id in (fresh, other):
            assert kb.get_task(conn, task_id).status == "running"
