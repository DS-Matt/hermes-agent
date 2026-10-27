"""A proved DNS failure cools fresh cards only on the same unchanged profile."""
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as kbd
from gateway import session_kanban
from tests.gateway.test_session_kanban_turn_recovery import _claimed_frame, _Agent, FAILED


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda name: tmp_path / name)
    monkeypatch.setattr(kb, "_resolve_rate_limit_cooldown_seconds", lambda: 300)
    for name in ("forge", "independent"):
        home = tmp_path / name
        home.mkdir()
        (home / "config.yaml").write_text("model: {}\n")
    with kbc.connect_closing(tmp_path / "kanban.db") as db:
        yield db


def receipt(conn, *, kind="dns", profile="forge", ended=None, scope=None):
    task = kb.create_task(conn, title="Attempt", assignee=profile)
    run = kb.claim_task(conn, task, claimer="worker:1")
    if scope is None:
        scope = kb._profile_transport_scope(profile)
    with kb.write_txn(conn):
        payload = {"exit_code": 75}
        if kind == "dns":
            payload["dns_transport_scope"] = scope
        kb._append_event(conn, task, "worker_result", payload, run_id=run.current_run_id)
        kb._end_run(conn, task, outcome="rate_limited", status="rate_limited")
        conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL WHERE id=?", (task,))
        if ended is not None:
            conn.execute("UPDATE task_runs SET ended_at=? WHERE id=?", (ended, run.current_run_id))
    return task


def test_fresh_same_profile_waits_independent_profile_can_dispatch(conn, monkeypatch, tmp_path):
    receipt(conn)
    fresh = kb.create_task(conn, title="Fresh", assignee="forge")
    other = kb.create_task(conn, title="Independent", assignee="independent")
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda _: True)
    monkeypatch.setattr(kbd._kbw, "resolve_workspace", lambda *a, **k: tmp_path)
    spawns = []
    result = kb.DispatchResult()
    for task_id in (fresh, other):
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        kbd._dispatch_lane_task(conn, row, row["assignee"], result, lane="ready", dry_run=False,
            ttl_seconds=None, board=None, failure_limit=3, spawn_fn=lambda task, workspace: spawns.append(task.id),
            per_profile_cap=None, per_profile_running={})
    assert spawns == [other]
    assert result.respawn_guarded == [(fresh, "profile_transport_cooldown")]
    assert kb.get_task(conn, fresh).consecutive_failures == 0


@pytest.mark.parametrize("kind", ["quota", "timeout", "legacy"])
def test_unproved_or_task_specific_failures_do_not_cool_fresh_cards(conn, kind):
    receipt(conn, kind=kind)
    fresh = kb.create_task(conn, title="Fresh", assignee="forge")
    assert kbd.check_respawn_guard(conn, fresh) is None


@pytest.mark.parametrize("change", ["config", "env", "override", "profile"])
def test_changed_routing_does_not_inherit_old_cooldown(conn, tmp_path, change):
    old = receipt(conn)
    kwargs = {"assignee": "forge"}
    if change == "config":
        (tmp_path / "forge/config.yaml").write_text("model: {provider: other}\n")
    elif change == "env":
        (tmp_path / "forge/.env").write_text("ANTHROPIC_BASE_URL=https://fixture.invalid\n")
    elif change == "override":
        kwargs.update(model_override="other-model", provider_override="other")
    elif change == "profile":
        kwargs["assignee"] = "independent"
    fresh = kb.create_task(conn, title="Changed route", **kwargs)
    assert kbd.check_respawn_guard(conn, fresh) is None
    assert kb.assign_task(conn, old, "independent")
    # Run profile provenance is immutable; editing the old task cannot spread it.
    other = kb.create_task(conn, title="Independent", assignee="independent")
    assert kbd.check_respawn_guard(conn, other) is None


def test_cooldown_expiry_and_disabled_setting(conn, monkeypatch):
    receipt(conn, ended=int(kbd.time.time()) - 301)
    fresh = kb.create_task(conn, title="Fresh", assignee="forge")
    assert kbd.check_respawn_guard(conn, fresh) is None
    receipt(conn)
    assert kbd.check_respawn_guard(conn, fresh) == "profile_transport_cooldown"
    monkeypatch.setattr(kb, "_resolve_rate_limit_cooldown_seconds", lambda: 0)
    assert kbd.check_respawn_guard(conn, fresh) is None


def test_missing_config_has_no_shareable_scope(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda _: tmp_path)
    assert kb._profile_transport_scope("forge") is None


@pytest.mark.parametrize("transport", [None, "dns"])
def test_bound_worker_records_scope_only_for_proved_dns(tmp_path, monkeypatch, transport):
    path, context, frame = _claimed_frame(tmp_path, monkeypatch, expired=True)
    context["transport_scope"] = {"home": "fixture", "config": [1, 2, 3, 4]}
    frame["policy"]["kanban_json"] = json.dumps(context)
    result = dict(FAILED)
    if transport:
        result["failure_transport"] = transport
    session_kanban.run_worker_turns(_Agent([result]), frame, [])
    recorded = session_kanban.worker_result(path, context)
    assert recorded["exit_code"] == 75
    assert recorded.get("dns_transport_scope") == (context["transport_scope"] if transport else None)
