import asyncio
import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .queue import Run


@dataclass(frozen=True)
class PiSettings:
    model: str
    key_name: str
    workspaces: Path
    image: str
    network: str
    timeout: int
    # Rootless Docker maps container root to the host user; any other container UID maps
    # to a subordinate UID that cannot write the 0700 workspace.
    container_user: str = field(default_factory=lambda: f'{os.getuid()}:{os.getgid()}')

    @classmethod
    def from_env(cls) -> "PiSettings":
        model = os.environ.get("PI_MODEL", "").strip()
        if "/" not in model or any(char.isspace() for char in model) or model.startswith("-"):
            raise ValueError("Set PI_MODEL to a provider-qualified Pi model, e.g. openai/<model-id>")
        key_name = os.environ.get("PI_PROVIDER_KEY_ENV", "").strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*_(?:API_KEY|AUTH_TOKEN)", key_name) or not os.environ.get(key_name):
            raise ValueError("Set PI_PROVIDER_KEY_ENV to the name of a nonempty provider API credential")
        if key_name.startswith(("GITHUB_", "GH_", "AWS_", "AGENT_", "DOCKER_", "PI_")):
            raise ValueError("Use a Pi model provider API credential, not a GitHub, AWS or service credential")
        root = Path(os.environ.get("PI_WORKSPACES", "state/workspaces")).resolve()
        if "," in str(root) or '"' in str(root) or '\n' in str(root):
            raise ValueError("PI_WORKSPACES cannot contain mount option separators")
        timeout = int(os.environ.get("PI_TIMEOUT_SECONDS", "900"))
        if not 30 <= timeout <= 3000:
            raise ValueError("PI_TIMEOUT_SECONDS must be between 30 and 3000")
        image = os.environ.get("PI_RUNNER_IMAGE", "github-agent-pi:1.5.2")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_./:@-]+", image):
            raise ValueError("Invalid PI_RUNNER_IMAGE")
        network = os.environ.get("PI_DOCKER_NETWORK", "bridge")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", network) or network == "host":
            raise ValueError("Use a non-host Docker network")
        user = '0:0' if docker_rootless() else f'{os.getuid()}:{os.getgid()}'
        return cls(model, key_name, root, image, network, timeout, user)


def docker_rootless() -> bool:
    try:
        out = subprocess.run(['docker', 'info', '--format', '{{json .SecurityOptions}}'],
                             capture_output=True, text=True, timeout=30, check=True).stdout
        options = json.loads(out) or []
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise ValueError('Cannot query the Docker daemon; start it and check DOCKER_HOST or the docker context') from exc
    return any(option == 'name=rootless' or option.startswith('name=rootless,') for option in options)


class PiEvents:
    def __init__(self, session_id: str, on_question: Callable[[], None]):
        self.session_id = session_id
        self.on_question = on_question
        self.waiting = False
        self.saw_session = False
        self.settled = False
        self.error = False

    def accept(self, line: bytes) -> None:
        event = json.loads(line)
        if not self.saw_session:
            self.saw_session = event.get('type') == 'session' and event.get('id') == self.session_id
            if not self.saw_session:
                raise ValueError('Pi returned an unexpected session')
        if event.get('type') == 'tool_execution_end' and event.get('toolName') == 'ask_user' and not event.get('isError'):
            details = (event.get('result') or {}).get('details') or {}
            if details.get('waiting') is True and not self.waiting:
                self.on_question()
                self.waiting = True
        if event.get('type') == 'message_end':
            msg = event.get('message') or {}
            if msg.get('role') == 'assistant':
                self.error = msg.get('stopReason') in {'error', 'aborted'}
        if event.get('type') == 'agent_settled':
            self.settled = not event.get('aborted') or self.waiting


def command(cfg: PiSettings, run: Run, mention: str, workspace: Path) -> list[str]:
    assignments = {
        'AGENT_REPOSITORY': run.repository, 'AGENT_ISSUE': str(run.issue_number),
        'AGENT_SESSION_ID': run.session_id, 'AGENT_MENTION': mention, 'AGENT_MODEL': cfg.model,
        'AGENT_REQUIRE_SESSION': '1' if run.kind == 'reply' else '0',
    }
    flags = [flag for key, value in assignments.items() for flag in ('--env', f'{key}={value}')]
    return [
        'docker', 'run', '--rm', '--interactive', '--init', '--pull=never',
        '--name', f'github-issue-pi-{run.session_id}', '--user', cfg.container_user,
        '--network', cfg.network, '--cap-drop=ALL', '--security-opt=no-new-privileges',
        '--read-only', '--tmpfs', '/tmp:rw,nosuid,nodev,size=512m', '--log-driver=none',
        '--memory=2g', '--cpus=2', '--pids-limit=256',
        '--mount', f'type=bind,source={workspace},target=/task',
        '--env', 'GH_TOKEN', '--env', cfg.key_name, *flags,
        cfg.image, 'timeout', '--kill-after=10s', f'{cfg.timeout}s', 'sh', '/opt/issue-agent/run.sh',
    ]


def prompt_for(run: Run) -> bytes:
    heading = 'Authorized reply to your latest clarification questions' if run.kind == 'reply' else 'Authorized work request'
    return (f'Target: GitHub repository {run.repository}, issue #{run.issue_number}, branch agent/issue-{run.issue_number}.\n'
            f'{heading}:\n{run.task}\n').encode()


async def execute(cfg: PiSettings, run: Run, mention: str, token: str, on_question: Callable[[], None]) -> str:
    workspace = cfg.workspaces / run.session_id
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        *command(cfg, run, mention, workspace),
        env={**os.environ, 'GH_TOKEN': token}, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=4 * 1024 * 1024,
    )
    events = PiEvents(run.session_id, on_question)
    try:
        async with asyncio.timeout(cfg.timeout + 20):
            try:
                proc.stdin.write(prompt_for(run))
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass  # Exit status below covers runner startup failures.
            finally:
                proc.stdin.close()
            while line := await proc.stdout.readline():
                events.accept(line)
            code = await proc.wait()
        if code:
            raise RuntimeError(f'Pi container exited with status {code}; check runner image, GitHub permissions and Pi provider auth')
        if not events.saw_session or not events.settled or (events.error and not events.waiting):
            raise RuntimeError('Pi did not complete a turn; check the model and provider configuration')
        return 'waiting for reply' if events.waiting else 'turn completed'
    finally:
        if proc.returncode is None:
            # The Docker client is not the container: removing it is separate from killing the client.
            try:
                await asyncio.to_thread(subprocess.run,
                    ['docker', 'rm', '-f', f'github-issue-pi-{run.session_id}'], timeout=10, check=False,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.TimeoutExpired):
                # The timeout inside the container still applies.
                pass
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.stdout.read()
                await proc.wait()
