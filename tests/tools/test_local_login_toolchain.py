"""HH454: login initialization keeps admitted tools without undoing later shell exports."""
from pathlib import Path

import pytest

from agent.session_client_env import client_env_scope
from tools.environments import local


@pytest.mark.platforms('linux', 'macos')
@pytest.mark.parametrize('explicit', [True, False])
def test_login_snapshot_preserves_toolchain_and_shell_state(tmp_path, monkeypatch, explicit):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('HERMES_HOME', str(home))
    client, daemon, rc_bin, launcher = [tmp_path / name for name in ('client * bin', 'daemon', 'rc', 'launcher')]
    for directory in (client, daemon, rc_bin, launcher):
        directory.mkdir()
    for directory, label in ((client, 'client'), (daemon, 'daemon')):
        executable = directory / 'whichclient'
        executable.write_text('#!/bin/sh\nprintf "%s\\n" ' + label + '\n')
        executable.chmod(0o700)
    monkeypatch.setenv('PATH', f'{daemon}:/usr/bin:/bin')
    monkeypatch.setattr(local, '_HERMES_BIN_DIR', str(launcher))
    monkeypatch.setattr(local, '_HERMES_BIN_DIR_IS_PAYLOAD', True)
    rc = tmp_path / 'init.sh'
    rc.write_text(f'export PATH="{rc_bin}:/usr/bin:/bin"\nexport NVM_DIR=rc\nIFS=,\nset -eu\n')
    monkeypatch.setattr(local, '_read_terminal_shell_init_config', lambda: ([str(rc)], False))
    attack = tmp_path / 'injected'
    admitted = {'PATH': f'{client}:/usr/bin:/bin', 'NVM_DIR': 'client-nvm',
                f'NODE_X; touch {attack}; #': 'invalid'} if explicit else None
    with client_env_scope(admitted):
        env = local.LocalEnvironment(cwd=str(tmp_path), timeout=10)
        try:
            first = env.execute('printf "PATH=%s\\n" "$PATH"; whichclient; '
                                'printf "NVM=%s\\n" "${NVM_DIR-}"; '
                                'env | grep "^_HERMES_LOGIN_" || true')
            assert first['returncode'] == 0, first
            output = first['output']
            paths = next(line.removeprefix('PATH=') for line in output.splitlines() if line.startswith('PATH=')).split(':')
            assert str(client if explicit else daemon) in paths
            assert paths[0] == str(launcher)
            assert str(rc_bin) in paths
            if explicit:
                assert paths.index(str(client)) < paths.index(str(rc_bin))
                assert str(daemon) not in paths
                assert 'NVM=client-nvm' in output
            else:
                assert paths.index(str(rc_bin)) < paths.index(str(daemon))
            assert ('\nclient\n' if explicit else '\ndaemon\n') in output
            assert not attack.exists()
            assert '_HERMES_LOGIN_' not in output
            assert '_HERMES_LOGIN_' not in Path(env._snapshot_path).read_text()
            changed = env.execute('export PATH="/session-later:$PATH"; printf changed')
            assert changed['returncode'] == 0
            later = env.execute('printf "%s" "$PATH"')
            assert later['returncode'] == 0 and later['output'].startswith('/session-later:')
            assert not attack.exists()
            assert '_HERMES_LOGIN_' not in Path(env._snapshot_path).read_text()
        finally:
            env.cleanup()
