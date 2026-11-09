"""Doctor must describe the selected backend, including when hosted in a container."""

from argparse import Namespace
import importlib.util
import sys
from types import SimpleNamespace

import pytest

import hermes_constants
from hermes_cli import doctor
from hermes_cli import doctor_tools


@pytest.fixture
def container_doctor(monkeypatch):
    monkeypatch.setattr(hermes_constants, "is_container", lambda: True)
    monkeypatch.setattr(doctor_tools, "find_docker", lambda: None)
    monkeypatch.setitem(sys.modules, "model_tools", SimpleNamespace(
        check_tool_availability=lambda *args, **kwargs: ([], []),
        TOOLSET_REQUIREMENTS={},
    ))


def test_explicit_remote_backend_reports_missing_auth_inside_container(
    container_doctor, monkeypatch, capsys,
):
    monkeypatch.setenv("TERMINAL_ENV", "vercel_sandbox")
    monkeypatch.setenv("VERCEL_TOKEN", "test-private-token")
    monkeypatch.setenv("VERCEL_TEAM_ID", "team")
    monkeypatch.delenv("VERCEL_PROJECT_ID", raising=False)
    monkeypatch.delenv("VERCEL_OIDC_TOKEN", raising=False)
    original_find_spec = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *args, **kwargs:
                        object() if name == "vercel" else original_find_spec(name, *args, **kwargs))

    doctor.run_doctor(Namespace(fix=False))

    output = capsys.readouterr().out
    assert "VERCEL_PROJECT_ID" in output
    assert "test-private-token" not in output
    assert "using local terminal backend" not in output


@pytest.mark.parametrize(("configured_backend", "in_container", "unknown"), [
    (None, True, False), ("", True, False), (" ", True, False), ("local", True, False),
    ("", False, True), (" ", False, True),
])
def test_local_default_and_invalid_backend_keep_existing_host_diagnostics(
    container_doctor, monkeypatch, capsys, configured_backend, in_container, unknown,
):
    monkeypatch.setattr(hermes_constants, "is_container", lambda: in_container)
    if configured_backend is None:
        monkeypatch.delenv("TERMINAL_ENV", raising=False)
    else:
        monkeypatch.setenv("TERMINAL_ENV", configured_backend)

    doctor.run_doctor(Namespace(fix=False))

    output = capsys.readouterr().out
    assert ("using local terminal backend" in output) is in_container
    assert ("Unknown terminal backend" in output) is unknown
    if in_container:
        assert "Docker/Podman not found" not in output
