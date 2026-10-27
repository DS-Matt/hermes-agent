"""Planned restart preserves a real Kanban run until completion or the finite drain cap."""
import json
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('mode', ['complete', 'cap'])
def test_real_kanban_receipt_client_survives_restart_wait(tmp_path, mode):
    from tests.gateway.fixtures.local_recovery_probe import child_env

    root = Path(__file__).resolve().parents[2]
    env = child_env()
    env.update(HOME=str(tmp_path / 'home'), USERPROFILE=str(tmp_path / 'home'),
               HERMES_HOME=str(tmp_path / 'state'), PYTHONPATH=str(root), KANBAN_RESTART_MODE=mode)
    result = subprocess.run([sys.executable, str(root / 'tests/gateway/fixtures/kanban_restart_probe.py')],
                            env=env, cwd=root, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=150, check=False)
    log = tmp_path / 'state/restart.log'
    assert result.returncode == 0, result.stdout + result.stderr + (log.read_text() if log.exists() else '')
    receipt = json.loads((tmp_path / 'state/receipt.json').read_text())
    assert receipt['gateway_exit'] == 75
    if mode == 'complete':
        assert receipt['client_exit'] == 0
