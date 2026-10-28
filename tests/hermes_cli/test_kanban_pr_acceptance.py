"""Two lifecycle invariants, using real SQLite and a local GitHub HTTP contract."""
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from hermes_cli.kanban_pr_acceptance import collect_acceptance


@pytest.fixture
def github(tmp_path, monkeypatch):
    state = {"conclusion": "success", "head": "a" * 40, "reads": 0, "requests": [], "metadata_requests": [], "metadata_identities": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            sha = state["head"]
            query = parse_qs(urlsplit(self.path).query).get("query", [""])[0]
            if urlsplit(self.path).path == "/graphql" and "nodes(" in query:
                ids = json.loads(re.search(r"ids:\s*(\[.*?\])", query).group(1))
                state["metadata_requests"].append(ids)
                state["metadata_identities"].append(self.headers.get("X-Fixture-Identity"))
                if state.get("metadata_status"):
                    self.send_error(state["metadata_status"])
                    return
                nodes = [n for n in state["nodes"] if n is None or n.get("id") in ids]
                value = state.get("metadata_response", {"data": {"nodes": nodes}})
            elif urlsplit(self.path).path == "/graphql":
                value = {"data": {"repository": {"pullRequest": {
                    "headRefOid": sha, "baseRefName": "main", "state": "OPEN",
                    "baseRef": {"branchProtectionRule": {"requiredStatusChecks": [
                        {"context": "required", "app": {"databaseId": state.get("required_app", 1)}}]}}}}}}
            elif "/rules/branches/" in self.path:
                value = [[]]
            elif "/check-runs" in self.path:
                run = {"id": 42, "name": "required", "head_sha": sha,
                       "app": {"id": 1}, "status": "in_progress" if state["conclusion"] == "pending" else "completed", "conclusion": state["conclusion"],
                       "html_url": "https://github.com/acme/repo/actions/runs/42"}
                if state.get("stale"):
                    run["head_sha"] = "b" * 40
                runs = [] if state.get("missing") else state.get("runs", [run])
                value = [{"total_count": 100 + len(runs) + int(state.get("incomplete", False)), "check_runs": [
                    {**run, "id": 1000 + i, "name": "optional", "conclusion": "skipped"}
                    for i in range(100)]}, {"total_count": 100 + len(runs) + int(state.get("incomplete", False)), "check_runs": runs}]
                if state.get("race"):
                    state["race"]()
                if state.get("head_change"):
                    state["head"] = "b" * 40
            elif "/statuses" in self.path:
                value = [[]]
            elif "/pulls/" in self.path:
                value = {"head": {"sha": sha}, "base": {"ref": "main"}, "state": "open"}
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\nimport os,sys,urllib.request,urllib.parse,urllib.error\n"
                  f"u='http://127.0.0.1:{server.server_port}/'+sys.argv[2]\n"
                  "q=next((a[6:] for a in sys.argv if a.startswith('query=')),None)\n"
                  "if q is not None: u+='?'+urllib.parse.urlencode({'query':q})\n"
                  "request=urllib.request.Request(u,headers={'X-Fixture-Identity':os.environ.get('GH_TOKEN','')})\n"
                  "try: print(urllib.request.urlopen(request).read().decode())\n"
                  "except urllib.error.HTTPError as e:\n"
                  " print('HTTP '+str(e.code),file=sys.stderr); sys.exit(1)\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.platforms("linux")
def test_pr_completion_requires_current_required_evidence(github):
    with connect() as conn:
        for conclusion in ("failure", "pending", "cancelled", "timed_out", "action_required", "neutral", "skipped", None, "success"):
            github.update(conclusion=conclusion, head="a" * 40)
            tid = kb.create_task(conn, title="Publish", completion_contract="acme/repo")
            ok = kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert ok is (conclusion == "success")
            task = kb.get_task(conn, tid)
            assert (task.status == "done") is ok
            receipts = [json.loads(r[0]) for r in conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]
            assert receipts and receipts[-1]["head_sha"] == "a" * 40
            if not ok:
                assert task.status in {"running", "ready", "blocked", "review"}
                assert "retry" in receipts[-1]["recovery"]
                assert receipts[-1]["checks"][0]["id"] == 42
        for fault in ("missing", "stale", "head_change"):
            github.update(conclusion="success", head="a" * 40)
            github[fault] = True
            tid = kb.create_task(conn, title=fault, completion_contract="acme/repo")
            assert not kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).status != "done"
            github.pop(fault)
        # Omission and a sibling repository cannot downgrade the stored declaration.
        tid = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        assert not kb.complete_task(conn, tid, summary="local green")
        assert not kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/other/repo/pull/7"})
        before = len(github["requests"])
        local = kb.create_task(conn, title="local", completion_contract="local-only")
        assert kb.complete_task(conn, local, summary="https://github.com/acme/repo/pull/7 is background context")
        assert len(github["requests"]) == before


@pytest.mark.platforms("linux")
def test_acceptance_receipts_and_terminal_write_share_run_ownership(github):
    with connect() as conn:
        for conclusion in ("success", "failure"):
            tid = kb.create_task(conn, title="race", completion_contract="acme/repo")
            owner = kb.claim_task(conn, tid)
            run_id = owner.current_run_id
            def reclaim():
                with connect() as rival:
                    assert kb.block_task(rival, tid, reason="Reassigned during acceptance")
                    assert kb.unblock_task(rival, tid)
                    github["replacement"] = kb.claim_task(rival, tid).current_run_id
            github.update(conclusion=conclusion, race=reclaim)
            assert not kb.complete_task(conn, tid, result="done", expected_run_id=run_id,
                metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).current_run_id == github["replacement"]
            assert github["replacement"] != run_id
            assert kb.get_task(conn, tid).status != "done"
            assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0] == 0
            github.pop("race")


# --- #122689: acceptance must read the repo as the ASSIGNEE profile's gh login ---

@pytest.mark.platforms("posix")
def test_acceptance_runs_gh_as_the_assignee_profile(tmp_path, monkeypatch):
    """The gh child env carries the assignee's own GH credentials (its .env),
    never the ambient/launch residue, and an invisible repo is classified
    `auth` naming the repository — not a retryable `infra` failure."""
    from pathlib import Path

    launch_home = tmp_path / "home"
    launch_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    assignee_home = launch_home / "profiles" / "b"
    assignee_home.mkdir(parents=True)
    (assignee_home / ".env").write_text("GH_TOKEN=b-token\n", encoding="utf-8")
    # Ambient residue that must NOT decide the login.
    monkeypatch.setenv("GH_TOKEN", "launch-token")
    monkeypatch.setenv("GH_CONFIG_DIR", "/nonexistent/launch/gh")

    env_dump = tmp_path / "gh_env.json"
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\nimport json, os\n"
                  f"json.dump(dict(os.environ), open({str(env_dump)!r}, 'w'))\n"
                  "print(json.dumps({'data': {'repository': None}}))\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="as-b", completion_contract="acme/repo", assignee="b")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        assert kb.get_task(conn, tid).status != "done"
        receipts = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]
        assert receipts[-1]["classification"] == "auth"
        assert "acme/repo" in receipts[-1]["detail"]
    captured = json.loads(env_dump.read_text())
    assert captured["GH_TOKEN"] == "b-token"
    assert captured.get("GH_CONFIG_DIR") != "/nonexistent/launch/gh"
    assert "credentials" in (kb.get_task(conn, tid).last_failure_error or "")


@pytest.mark.platforms("posix")
def test_assignee_without_own_gh_login_never_falls_through_to_ambient_login(tmp_path, monkeypatch):
    """An assignee profile with no GH_TOKEN/GH_CONFIG_DIR of its own must not inherit the
    launch user's ~/.config/gh (HOME/XDG_CONFIG_HOME stay the launch process's): gh is pinned
    to a profile-owned config dir, its 'not logged in' exit is classified `auth` naming the profile."""
    launch_home = tmp_path / "home"
    launch_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    assignee_home = launch_home / "profiles" / "b"
    assignee_home.mkdir(parents=True)
    (assignee_home / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv("GH_TOKEN", "launch-token")
    monkeypatch.setenv("GH_CONFIG_DIR", "/nonexistent/launch/gh")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "launch-xdg"))

    env_dump = tmp_path / "gh_env.json"
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    # Real gh: GH_CONFIG_DIR wins; a config dir without hosts.yml means "not logged in" (exit 4).
    gh.write_text(f"#!{sys.executable}\nimport json, os, pathlib, sys\n"
                  f"pathlib.Path({str(env_dump)!r}).write_text(json.dumps(dict(os.environ)), encoding='utf-8')\n"
                  "if 'GH_CONFIG_DIR' in os.environ and not os.path.exists(os.environ['GH_CONFIG_DIR']):\n"
                  "    sys.exit(4)\n"
                  "print(json.dumps({'data': {'repository': None}}))\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="as-b", completion_contract="acme/repo", assignee="b")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
    assert receipt["classification"] == "auth"
    assert "'b'" in receipt["detail"] and "no login" in receipt["detail"]
    captured = json.loads(env_dump.read_text(encoding="utf-8-sig"))
    assert captured["GH_CONFIG_DIR"] == str(assignee_home / "gh")
    assert "GH_TOKEN" not in captured and "GITHUB_TOKEN" not in captured


def test_assigned_card_with_unresolvable_profile_is_auth_not_ambient(tmp_path, monkeypatch):
    """A card assigned to a profile that no longer exists must not run gh as the completing
    process's ambient login: classification `auth` naming the profile, gh never invoked."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))  # any gh spawn would fail as infra
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="as-ghost", completion_contract="acme/repo", assignee="ghost")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
    assert receipt["classification"] == "auth"
    assert "'ghost'" in receipt["detail"] and "cannot be resolved" in receipt["detail"]


def _check_pair(number, conclusion="success", *, started="2026-10-28T01:00:00Z", app=1):
    run = {"id": number, "node_id": f"CR_{number}", "name": "required", "head_sha": "a" * 40,
           "app": {"id": app}, "status": "in_progress" if conclusion == "pending" else "completed",
           "conclusion": conclusion, "html_url": f"https://github.com/acme/repo/checks/{number}"}
    node = {"id": run["node_id"], "databaseId": number, "startedAt": started,
            "checkSuite": {"workflowRun": {"event": "pull_request", "workflow": {"databaseId": 7}}}}
    return run, node


def _set_checks(github, pairs):
    github["runs"], github["nodes"] = map(list, zip(*pairs))


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("older", ["skipped", "failure"])
@pytest.mark.parametrize("reverse", [False, True])
def test_current_required_rerun_supersedes_old_outcome(github, older, reverse):
    pairs = [_check_pair(41, older), _check_pair(42, started="2026-10-28T02:00:00Z")]
    _set_checks(github, pairs[::-1] if reverse else pairs)
    github["nodes"].reverse()  # GraphQL response order is not REST identity.
    receipt = collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    assert receipt["ok"]
    assert [c["id"] for c in receipt["checks"]] == [42]
    assert github["metadata_requests"] and set(github["metadata_requests"][0]) == {"CR_41", "CR_42"}


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("latest,classification", [("failure", "failure"), ("pending", "pending"),
                                                   ("skipped", "infra"), ("neutral", "infra"),
                                                   ("cancelled", "infra"), (None, "infra")])
def test_current_required_rerun_keeps_strict_failure_policy(github, latest, classification):
    _set_checks(github, [_check_pair(41), _check_pair(42, latest, started="2026-10-28T02:00:00Z")])
    receipt = collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    assert not receipt["ok"] and receipt["classification"] == classification
    assert [c["id"] for c in receipt["checks"]] == [42]


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("separation", ["workflow", "event", "app", "head", "no_suite", "no_run", "no_workflow", "no_workflow_id", "blank_event", "missing_start", "tied_start"])
def test_unrelated_or_ambiguous_checks_cannot_hide_failure(github, separation):
    old, new = _check_pair(41, "failure"), _check_pair(42, started="2026-10-28T02:00:00Z")
    workflow_run = old[1]["checkSuite"]["workflowRun"]
    mutations = {
        "workflow": lambda: workflow_run["workflow"].update(databaseId=8),
        "event": lambda: workflow_run.update(event="push"),
        "app": lambda: (github.update(required_app=None), new[0]["app"].update(id=2)),
        "head": lambda: old[0].update(head_sha="b" * 40),
        "no_suite": lambda: old[1].update(checkSuite=None),
        "no_run": lambda: old[1]["checkSuite"].update(workflowRun=None),
        "no_workflow": lambda: workflow_run.update(workflow=None),
        "no_workflow_id": lambda: workflow_run["workflow"].update(databaseId=None),
        "blank_event": lambda: workflow_run.update(event=""),
        "missing_start": lambda: old[1].update(startedAt=None),
        "tied_start": lambda: old[1].update(startedAt=new[1]["startedAt"]),
    }
    mutations[separation]()
    _set_checks(github, [old, new])
    receipt = collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    assert not receipt["ok"]
    assert {c["id"] for c in receipt["checks"]} == {41, 42}
    assert receipt["classification"] == ("stale" if separation == "head" else "failure")


@pytest.mark.platforms("posix")
def test_older_timestamp_ties_do_not_block_a_unique_newest_success(github):
    _set_checks(github, [_check_pair(40, "failure"), _check_pair(41, "skipped"),
                         _check_pair(42, started="2026-10-28T02:00:00Z")])
    receipt = collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    assert receipt["ok"] and [c["id"] for c in receipt["checks"]] == [42]


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("fault", [
    "null", "missing", "duplicate", "foreign", "database_id", "container", "errors", "naive", "malformed",
    "rest_node_missing", "rest_node_duplicate", "suite_type", "run_type", "workflow_type",
    "workflow_id_type", "event_type", "ungroupable_timestamp",
])
def test_incomplete_duplicate_identity_fails_closed(github, fault):
    pairs = [_check_pair(41), _check_pair(42, started="2026-10-28T02:00:00Z")]
    _set_checks(github, pairs)
    nodes = github["nodes"]
    mutations = {
        "null": lambda: github.update(metadata_response={"data": {"nodes": [None, nodes[1]]}}),
        "missing": lambda: nodes.pop(),
        "duplicate": lambda: github.update(metadata_response={"data": {"nodes": [nodes[0], nodes[0]]}}),
        "foreign": lambda: github.update(metadata_response={"data": {"nodes": [
            {**nodes[0], "id": "CR_foreign"}, nodes[1]]}}),
        "database_id": lambda: nodes[0].update(databaseId=999),
        "container": lambda: github.update(metadata_response={"data": {"nodes": {}}}),
        "errors": lambda: github.update(metadata_response={"data": {"nodes": nodes},
                                                           "errors": [{"type": "FORBIDDEN"}]}),
        "naive": lambda: nodes[0].update(startedAt="2026-10-28T01:00:00"),
        "malformed": lambda: nodes[0].update(startedAt="yesterday"),
        "rest_node_missing": lambda: pairs[0][0].pop("node_id"),
        "rest_node_duplicate": lambda: pairs[1][0].update(node_id=pairs[0][0]["node_id"]),
        "suite_type": lambda: nodes[0].update(checkSuite="x"),
        "run_type": lambda: nodes[0]["checkSuite"].update(workflowRun=[]),
        "workflow_type": lambda: nodes[0]["checkSuite"]["workflowRun"].update(workflow=7),
        "workflow_id_type": lambda: nodes[0]["checkSuite"]["workflowRun"]["workflow"].update(databaseId="7"),
        "event_type": lambda: nodes[0]["checkSuite"]["workflowRun"].update(event=7),
        "ungroupable_timestamp": lambda: nodes[0].update(checkSuite=None, startedAt="yesterday"),
    }
    mutations[fault]()
    receipt = collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    assert not receipt["ok"] and receipt["classification"] == "infra"


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("fault", ["incomplete", "head_change", "metadata_status"])
def test_current_selection_preserves_pagination_head_and_auth_guards(github, fault):
    _set_checks(github, [_check_pair(41), _check_pair(42, started="2026-10-28T02:00:00Z")])
    github[fault] = 403 if fault == "metadata_status" else True
    receipt = collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    expected = {"incomplete": "infra", "head_change": "stale", "metadata_status": "auth"}[fault]
    assert not receipt["ok"] and receipt["classification"] == expected


@pytest.mark.platforms("posix")
def test_singleton_and_foreign_app_need_no_duplicate_metadata(github):
    _set_checks(github, [_check_pair(41, "failure"), _check_pair(42, app=2)])
    receipt = collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    assert not receipt["ok"] and [c["id"] for c in receipt["checks"]] == [41]
    assert not github["metadata_requests"]
    github["runs"] = [github["runs"][1]]
    github["required_app"] = 2
    assert collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")["ok"]
    assert not github["metadata_requests"]


@pytest.mark.platforms("posix")
def test_duplicate_metadata_batches_only_candidates_as_assignee(github, tmp_path, monkeypatch):
    home = tmp_path / "home"
    profile = home / "profiles/forge"
    profile.mkdir(parents=True)
    (profile / ".env").write_text("GH_TOKEN=fixture-assignee\n")
    monkeypatch.setenv("GH_TOKEN", "fixture-ambient")
    pairs = [_check_pair(i, "failure") for i in range(1, 101)]
    pairs.append(_check_pair(101, started="2026-10-28T02:00:00Z"))
    pairs.append(_check_pair(102, app=2))
    _set_checks(github, pairs)
    receipt = collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7", assignee="forge")
    assert receipt["ok"] and [c["id"] for c in receipt["checks"]] == [101]
    requested = github["metadata_requests"]
    assert len(requested) == 2 and sorted(map(len, requested)) == [1, 100]
    assert {node for batch in requested for node in batch} == {f"CR_{i}" for i in range(1, 102)}
    assert github["metadata_identities"] == ["fixture-assignee", "fixture-assignee"]


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("duplicates", [False, True])
def test_nullable_app_is_retained_without_metadata(github, duplicates):
    nullable = _check_pair(43, "failure" if duplicates else "success")
    nullable[0]["app"] = None
    pairs = ([_check_pair(41, "failure"), _check_pair(42, started="2026-10-28T02:00:00Z")]
             if duplicates else [])
    _set_checks(github, [*pairs, nullable])
    github["required_app"] = None
    receipt = collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    assert receipt["ok"] is (not duplicates)
    assert receipt["classification"] == ("failure" if duplicates else "success")
    assert {c["id"] for c in receipt["checks"]} == ({42, 43} if duplicates else {43})
    assert len(github["metadata_requests"]) == int(duplicates)
    assert {node for batch in github["metadata_requests"] for node in batch} == (
        {"CR_41", "CR_42"} if duplicates else set())
