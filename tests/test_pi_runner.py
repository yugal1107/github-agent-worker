import json
import os
import subprocess
from uuid import uuid4

import pytest

from agent_worker.pi_runner import PiEvents, PiSettings, command, prompt_for
from agent_worker.queue import Run


def test_waiting_signal_is_recorded_once_and_settles():
    notices = []
    pi = PiEvents("issue-session", lambda: notices.append(True))
    events = [
        {"type": "session", "id": "issue-session"},
        {"type": "tool_execution_end", "toolName": "ask_user", "isError": True,
         "result": {"details": {"waiting": True}}},
        {"type": "tool_execution_end", "toolName": "ask_user", "isError": False,
         "result": {"details": {"waiting": True}}},
        {"type": "tool_execution_end", "toolName": "ask_user", "isError": False,
         "result": {"details": {"waiting": True}}},
        {"type": "agent_settled", "aborted": True},
    ]
    for event in events:
        pi.accept(json.dumps(event).encode())
    assert pi.waiting and pi.settled
    assert notices == [True]


def test_unexpected_session_is_rejected():
    pi = PiEvents("issue-session", lambda: None)
    with pytest.raises(ValueError, match="unexpected session"):
        pi.accept(b'{"type":"session","id":"different-session"}')


def test_runner_mounts_only_issue_and_passes_prompt_via_stdin(tmp_path):
    run = Run(1, 10, 20, 42, 30, "--potential-flag @secret", "owner/project", "reply", uuid4().hex)
    cfg = PiSettings("openai/test", "OPENAI_API_KEY", tmp_path, "image:test", "bridge", 300)
    workspace = tmp_path / run.session_id
    argv = command(cfg, run, "@my-agent", workspace)
    assert argv.count("--mount") == 1
    assert f"type=bind,source={workspace},target=/task" in argv
    assert argv.count("GH_TOKEN") == 1
    assert all("WEBHOOK_SECRET" not in entry and run.task not in entry for entry in argv)
    assert b"Authorized reply" in prompt_for(run) and run.task.encode() in prompt_for(run)
    assert argv[argv.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"


@pytest.mark.parametrize(("options", "user"), [
    ('["name=seccomp,profile=builtin","name=rootless","name=cgroupns"]', "0:0"),
    ('["name=seccomp,profile=builtin","name=cgroupns"]', None),
])
def test_rootless_docker_runs_container_as_mapped_root(monkeypatch, tmp_path, options, user):
    monkeypatch.setenv("PI_MODEL", "openai/test")
    monkeypatch.setenv("PI_PROVIDER_KEY_ENV", "OPENAI_API_KEY")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("PI_WORKSPACES", str(tmp_path))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, options, ""))
    expected = user or f"{os.getuid()}:{os.getgid()}"
    assert PiSettings.from_env().container_user == expected


def test_unreachable_docker_fails_at_startup(monkeypatch, tmp_path):
    monkeypatch.setenv("PI_MODEL", "openai/test")
    monkeypatch.setenv("PI_PROVIDER_KEY_ENV", "OPENAI_API_KEY")
    monkeypatch.setenv("OPENAI_API_KEY", "x")

    def fail(*a, **k):
        raise subprocess.CalledProcessError(1, a[0])
    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(ValueError, match="Docker daemon"):
        PiSettings.from_env()
