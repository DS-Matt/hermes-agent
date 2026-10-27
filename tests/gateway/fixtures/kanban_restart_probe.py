"""Real Kanban receipt client and managed compute survive a disposable gateway's restart wait."""
import asyncio
from contextlib import closing
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

from kanban_owner_probe import Model
from local_recovery_probe import daemon, websocket, rpc
from tests.gateway.test_normal_runtime_boot import control
from tests.waiting import async_wait_until, wait_until


async def during_restart(home, desc, proc, peer, conn, task, client, mode):
    from hermes_cli import kanban_db as kb

    params = dict(task_id=task.id, board='owned', run_id=task.current_run_id,
                  claim_lock=task.claim_lock, db=str(kb.kanban_db_path(board='owned')))
    async with websocket(home, desc) as observer:
        accepted = (await rpc(observer, 'kanban.run', **params))['result']
        sid, admission = accepted['session_id'], accepted['receipt']['admission_id']
        assert hasattr(signal, 'SIGUSR1'), 'this probe requires POSIX restart signals'
        proc.send_signal(signal.SIGUSR1)  # windows-footgun: ok — POSIX-only test, guarded above
        await async_wait_until(lambda: control(home, 'identify').get('state') == 'draining',
                               timeout=10, what='native restart wait')
        receipt = await rpc(observer, 'prompt.receipt', session_id=sid, admission_id=admission)
        assert receipt['result']['status'] == 'started', receipt
        refused = await rpc(observer, 'kanban.run', **params)
        assert refused['error']['message'] == 'runtime_draining', refused
        assert client.poll() is None, 'original dispatcher submitter exited during restart wait'
        assert kb.get_task(conn, task.id).current_run_id == task.current_run_id
        if mode == 'complete':
            peer.release.set()  # work finishes DURING the wait, never after restart
        # The observer remains connected and does not hold the restart open after work finishes.
        await asyncio.to_thread(proc.wait, 20)
        await asyncio.to_thread(client.wait, 10)
        assert proc.returncode == 75, proc.returncode
        if mode == 'complete':
            assert client.returncode == 0, client.returncode
            current = kb.get_task(conn, task.id)
            runs = kb.list_runs(conn, task.id)
            admitted = [e for e in kb.list_events(conn, task.id) if e.kind == 'owner_admitted']
            assert (current.status, current.consecutive_failures, current.block_recurrences) == ('done', 0, 0)
            assert len(runs) == 1 and runs[0].id == task.current_run_id and runs[0].outcome == 'completed'
            assert len(admitted) == 1 and admitted[0].payload['session_id'] == sid
        else:
            assert not peer.release.is_set() and kb.get_task(conn, task.id).status != 'done'
        return dict(mode=mode, session_id=sid, run_id=task.current_run_id,
                    gateway_exit=proc.returncode, client_exit=client.returncode)


def main():
    from hermes_cli import kanban_db as kb, kanban_db_dispatch as dispatch
    from hermes_cli.kanban_db_connect import connect

    root = Path(__file__).resolve().parents[3]
    home = Path(os.environ['HERMES_HOME'])
    home.mkdir(parents=True)
    Path(os.environ['HOME']).mkdir(parents=True)
    os.environ['HERMES_KANBAN_HOME'] = str(home)
    mode = os.environ['KANBAN_RESTART_MODE']
    peer = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    peer.requests, peer.mode = [], 'complete'
    peer.blocked, peer.release = threading.Event(), threading.Event()
    threading.Thread(target=peer.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{peer.server_port}/v1'
    os.environ.update(OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=url)
    cfg = {'gateway': {'multiplex_profiles': False},
           'model': {'provider': 'custom', 'default': 'loop-model', 'base_url': url},
           'auxiliary': {'title_generation': {'enabled': False}},
           'platform_toolsets': {'cli': []}, 'kanban': {'dispatch_in_gateway': False},
           'agent': {'api_max_retries': 1, 'restart_after_turn_timeout': 30 if mode == 'complete' else 2,
                     'restart_drain_timeout': 0, 'signal_interrupt_grace_timeout': 0}}
    (home / 'config.yaml').write_text(json.dumps(cfg))
    dispatch._resolve_hermes_argv = lambda: [sys.executable, '-m', 'hermes_cli.main']
    kb.create_board('owned')
    clients, receipt = [], {}
    original_popen = subprocess.Popen

    def spawn(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        if 'hermes_cli.kanban_worker_client' in args[0]:
            clients.append(child)
        return child

    try:
        with closing(connect(board='owned')) as conn:
            tid = kb.create_task(conn, title='Finish during restart', assignee='default',
                                 workspace_kind='dir', workspace_path=str(home), goal_mode=True, goal_max_turns=2)
            with daemon(root, home, dict(os.environ), barrier=False) as (proc, desc):
                subprocess.Popen = spawn
                try:
                    launched = dispatch.dispatch_once(conn, board='owned', max_spawn=1)
                finally:
                    subprocess.Popen = original_popen
                assert [s[0] for s in launched.spawned] == [tid], launched
                assert peer.blocked.wait(55), 'managed worker did not reach the controlled provider'
                task = kb.get_task(conn, tid)
                wait_until(lambda: any(e.kind == 'worker_bound' for e in kb.list_events(conn, tid)), timeout=10)
                receipt = asyncio.run(during_restart(home, desc, proc, peer, conn, task, clients[0], mode))
    finally:
        peer.release.set()
        for child in clients:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)
        peer.shutdown()
        peer.server_close()
        (home / 'receipt.json').write_text(json.dumps(receipt))
        print(json.dumps(receipt))


if __name__ == '__main__':
    main()
