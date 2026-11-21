import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_worker.pi_runner import PiEvents


@pytest.mark.skipif(os.environ.get('TEST_PI_CLI') != '1', reason='set TEST_PI_CLI=1 to run the real Pi CLI smoke test')
def test_pi_pause_and_resume(tmp_path):
    pi = shutil.which('pi')
    assert pi, 'Install Pi 1.5.2 before running this smoke test'
    repo = Path(__file__).resolve().parents[1]
    posted = tmp_path / 'posted.txt'
    gh = tmp_path / 'gh'
    gh.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$FAKE_GH_LOG"\n')
    gh.chmod(0o700)
    env = {
        'HOME': str(tmp_path), 'PI_CODING_AGENT_DIR': str(tmp_path / 'config'),
        'PATH': f'{tmp_path}:{os.environ["PATH"]}', 'FAKE_GH_LOG': str(posted),
        'AGENT_ISSUE': '42', 'AGENT_REPOSITORY': 'owner/test', 'AGENT_MENTION': '@my-agent', 'PI_TELEMETRY': '0',
    }
    sessions = tmp_path / 'sessions'
    args = [pi, '--offline', '--no-approve', '--no-extensions', '--no-mcp', '--mode', 'json',
            '--session-dir', str(sessions), '--model', 'worker-test/test',
            '-e', str(repo / 'tests/fixtures/pi-test-provider.ts'), '-e', str(repo / 'runner/ask-user.ts')]

    def invoke(session_options, text, waiting):
        result = subprocess.run(args + session_options, cwd=tmp_path, env=env, input=text,
                                text=True, capture_output=True, timeout=20)
        assert result.returncode == 0, result.stderr
        events = PiEvents('worker-smoke', lambda: None)
        for line in result.stdout.split('\n'):
            if line:
                events.accept(line.encode())
        assert events.settled and events.waiting == waiting
        return result.stdout

    invoke(['--session-id', 'worker-smoke'], 'Please ask the questions.', waiting=True)
    text = posted.read_text()
    assert '1. A or B?' in text and '2. Retain C?' in text and text.count('--body\n') == 1
    [session] = sessions.glob('*_worker-smoke.jsonl')
    second = invoke(['--session', str(session)], '1. A. 2. Keep C.', waiting=False)
    assert 'Resumed with question history.' in second
    assert posted.read_text() == text
