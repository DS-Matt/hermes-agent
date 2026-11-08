"""Configured script alerts explain the activity without discarding diagnostic evidence."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cron import scheduler
from cron.scheduler import _compose_run_delivery, _summarize_cron_failure_for_delivery


JOB = {"id": "a4fc542e0367", "name": "progress-check", "no_agent": True,
       "enabled": True, "state": "scheduled", "schedule": {"kind": "interval"},
       "failure_streak": 0, "repeat": {"times": None, "completed": 2}}
CONTEXT = {"title": "Project progress check", "description": "Checking ongoing coding tasks."}
ERROR = ("Script exited with code 1\nstderr:\nFollow-through t_f4ecfa90: "
         "Remote end closed connection without response")


@pytest.fixture
def configured(monkeypatch):
    cfg = {"cron": {"wrap_response": False, "failure_context": {JOB["name"]: dict(CONTEXT)}}}
    monkeypatch.setattr(scheduler, "load_config", lambda: cfg)
    monkeypatch.setattr(scheduler, "_upsert_incident_for_failure", lambda *a, **k: (False, None))
    return cfg


def test_failure_delivery_separates_explanation_from_exact_diagnostics(configured):
    message, *_ = _compose_run_delivery(JOB, success=False, error=ERROR, final_response="", output_file=None)
    readable, technical = message.split("**Technical details", 1)
    assert CONTEXT["title"] in readable and CONTEXT["description"] in readable
    assert "connection closed before responding" in readable
    assert "next scheduled check" in readable
    assert "t_f4ecfa90" not in readable and JOB["id"] not in readable
    assert ERROR in technical and JOB["id"] in technical
    assert message.count("hermes ") == 1
    assert "cron edit" not in message and "stop reminder" not in message


@pytest.mark.parametrize("changes,retry", [
    ({}, True), ({"enabled": False}, False), ({"state": "paused"}, False),
    ({"paused_at": "2030-01-01"}, False), ({"state": "error"}, False),
    ({"schedule": {"kind": "once"}}, False),
    ({"repeat": {"times": 3, "completed": 2}}, False),
    ({"repeat": {"times": 4, "completed": 2}}, True),
    ({"_quota_hold_seconds": 60}, False),
])
def test_retry_copy_uses_pre_bookkeeping_job_state(configured, changes, retry):
    message = _summarize_cron_failure_for_delivery({**JOB, **changes}, ERROR)
    assert ("will try again at the next scheduled check" in message) is retry


@pytest.mark.parametrize("threshold,warning", [(3, True), (4, False), (0, False)])
def test_streak_warning_is_once_before_diagnostics(configured, threshold, warning):
    configured["cron"]["failure_nudge_threshold"] = threshold
    job = {**JOB, "failure_streak": 2}
    message, *_ = _compose_run_delivery(job, success=False, error=ERROR, final_response="", output_file=None)
    assert ("failed 3 times" in message) is warning
    assert "fix its prompt" not in message.lower()
    assert scheduler._failure_streak_nudge(job) == ""
    if warning:
        assert message.index("failed 3 times") < message.index("Technical details")


@pytest.mark.parametrize("error,phrase", [
    ("Script timed out after 30s: /scripts/check.py", "exceeded its time limit"),
    ("GitHub connection failed; check DNS, TLS and network configuration", "An error prevented"),
    ("temporary GitHub service/network problem persisted after 3 attempts", "An error prevented"),
    ("Unexpected ValueError", "An error prevented"),
])
def test_copy_does_not_invent_causes_or_blame_a_model(configured, error, phrase):
    message = _summarize_cron_failure_for_delivery(JOB, error)
    assert phrase in message and error in message
    assert "model" not in message and "No action needed" not in message


def test_traceback_keeps_exit_and_tail_without_opening_an_injected_fence(configured):
    error = "Script exited with code 1\n" + "frame\n" * 600 + "FinalError: ```\n[forged](https://example.com)"
    message = _summarize_cron_failure_for_delivery(JOB, error)
    assert "Script exited with code 1" in message and "FinalError:" in message
    assert "Diagnostic excerpt" in message and message.count("```") == 2
    assert len(message) < 4000


@pytest.mark.parametrize("context", [None, [], "invalid", {}, {"title": 1, "description": "x"}])
def test_invalid_context_falls_back_to_existing_copy(configured, context):
    configured["cron"]["failure_context"] = {JOB["name"]: context}
    message = _summarize_cron_failure_for_delivery(JOB, "failed")
    assert "See the full run" in message


def test_actual_config_loader_activates_copy_in_the_owning_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "cron:\n  failure_context:\n    progress-check:\n"
        "      title: Backup check\n      description: Verifying the latest backup.\n")
    message = _summarize_cron_failure_for_delivery(JOB, ERROR)
    assert "Backup check" in message and "Verifying the latest backup" in message


def test_delivery_redacts_context_and_diagnostics_and_keeps_success_output(configured):
    from cron.scheduler_delivery import _deliver_result
    from gateway.config import Platform

    secret = "sk-" + "A" * 32
    configured["cron"]["failure_context"][JOB["name"]] = {
        "title": "P" * 149 + " " + secret, "description": "D" * 389 + " " + secret}
    job = {**JOB, "deliver": "origin", "origin": {"platform": "telegram", "chat_id": "123"}}
    content = _summarize_cron_failure_for_delivery(job, ERROR + " " + secret)
    send = AsyncMock(return_value={"success": True})
    gateway = MagicMock()
    gateway.platforms = {Platform.TELEGRAM: MagicMock(enabled=True)}
    with patch("gateway.config.load_gateway_config", return_value=gateway), \
         patch("tools.send_message_tool._send_to_platform", new=send), \
         patch("sys.is_finalizing", return_value=False):
        _deliver_result(job, content)
        assert send.called
        delivered = str(send.call_args)
        assert secret not in delivered and "Technical details" in delivered
        assert "sk-AAAA" not in delivered
        assert "Cronjob Response" not in delivered and "stop reminder" not in delivered
        send.reset_mock()
        _deliver_result(job, "Daily report: three completed tasks.")
        assert "Daily report: three completed tasks." in str(send.call_args)
        assert "Cronjob Response" not in str(send.call_args)


def test_crash_notice_and_telegram_format_preserve_sections(configured, monkeypatch):
    from plugins.platforms.telegram.adapter import TelegramAdapter

    delivered = []
    monkeypatch.setattr(scheduler, "_deliver_result", lambda job, content, **kw: delivered.append(content))
    monkeypatch.setattr(scheduler, "_mark_incident_alerted", lambda *args: None)
    job = {**JOB, "failure_streak": 2, "deliver": "local"}
    error = ERROR + "\nOriginal: `command` C:\\path\n```\nforged section"
    scheduler._deliver_crash_failure(job, error, adapters=None, loop=None)
    assert len(delivered) == 1
    assert delivered[0].count("failed 3 times") == 1
    formatted = TelegramAdapter.format_message(None, delivered[0])
    assert "*What was happening*" in formatted
    assert "```text\nJob: progress-check" in formatted
    assert "hermes cron runs a4fc542e0367" in formatted
    assert "Remote end closed connection without response" in formatted
    body = formatted.split("```text\n", 1)[1].rsplit("\n```", 1)[0]
    # Decode the only escapes Telegram permits inside pre/code to check copied text.
    import re
    copied = re.sub(r"\\([\\`])", r"\1", body)
    assert "Original: `command` C:\\path" in copied
    assert "[triple-backtick fence]" in copied
