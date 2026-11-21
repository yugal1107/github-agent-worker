# GitHub Agent Worker

Self-hosted FastAPI + SQLite automation. An allowlisted GitHub issue comment starts a **Pi** session on your VM. Pi edits, runs tests, commits, and opens or updates a draft PR through `git` and `gh`. Pi can post one batch of questions per run; an explicit reply resumes its saved session and checkout. You review and merge. This service uses no GitHub Actions runner; existing CI may still trigger on bot pushes or PRs.

## Install and test locally

```sh
uv sync --locked
uv run --locked pytest
```

Python 3.12 is pinned in `.python-version`. You do not need Pi, a model key, Docker, or GitHub for the unit tests. If Pi 1.5.2 is installed, `TEST_PI_CLI=1 uv run --locked pytest tests/test_pi_cli.py` exercises pause and resume with a fake model and `gh` (no credentials or network needed).

## Set up a manual test on the VM

1. **Start with a disposable private repository.** Create a private GitHub App, enable its webhook at `https://your-host.example/webhooks/github`, and subscribe to **Issue comment**. Generate a webhook secret with `openssl rand -hex 32`. Grant repository permissions **Contents: Read and write**, **Issues: Read and write**, **Pull requests: Read and write** (Metadata read is automatic). Install it only on the test repository. If adding permissions to an existing App, accept them on the installation. Generate the App's PEM private key in GitHub settings.
2. **Protect the default branch.** Require a reviewed PR and don't put the App on the bypass list. Do not give it Actions/workflow or administration permissions. The App token is scoped to one repository, not to a single issue or branch. The agent's instructions to use its own branch are not a security boundary. Required review alone may still let the App merge after approval. If only humans should be able to merge, also restrict updates to the protected branch to human maintainers where your GitHub plan supports it.
3. **Configure the VM.** Run as a dedicated non-root user with access to a local Linux Docker daemon (rootless recommended). Use an isolated Docker network or VM firewall policy that denies agent containers access to instance metadata, the Docker API, and private services they don't need. The runner needs outbound GitHub and model-provider access. Keep `QUEUE_DB` and `PI_WORKSPACES` on persistent local disk outside the source checkout if possible. The workspaces and sessions can contain private code and transcripts; monitor disk usage and keep them private.
4. **Copy `.env.example` to `.env`, restrict access to it and the PEM (`chmod 600`), and fill in the values.** Use the same webhook secret as GitHub. Set `GITHUB_APP_ID` to the numeric **App ID** (not the client ID). The App webhook delivery payload shows the numeric `installation.id`, `repository.id`, and `comment.user.id`. The repository and requester lists accept comma-separated IDs. Paths in `.env` should be absolute if the two services may start from different working directories.
5. **Configure Pi.** Set `PI_MODEL` to the provider-qualified model ID (e.g. `openai/<model-id>`), `PI_PROVIDER_KEY_ENV` to that provider's credential variable name, and put the actual credential in that variable. This version expects an API credential for a Pi-supported provider, not an OAuth profile or somebody's desktop login. Avoid changing the provider while an issue is waiting for a reply. Only use providers authorized for your code. Keep provider keys scoped and set a spending limit. The runner does not enable Athena or receive AWS credentials.

Build the pinned Pi runner image under the same Docker user that will run the worker:

```sh
docker build -t github-agent-pi:1.5.2 runner
docker run --rm --network=none github-agent-pi:1.5.2 pi --version
```

### Rootless Docker on Ubuntu

Do not add the worker user to the `docker` group or run the worker with `sudo`; both equal root on the VM. Use rootless Docker for the worker user:

```sh
sudo apt-get install -y uidmap dbus-user-session docker-ce-rootless-extras   # docker.io users: curl -fsSL https://get.docker.com/rootless | sh
grep "^<agent-user>:" /etc/subuid /etc/subgid                                 # both need a range
# Log in as the agent user directly (SSH or `sudo machinectl shell <agent-user>@`), not `sudo su`.
dockerd-rootless-setuptool.sh install      # on Ubuntu 24.04+ it prints an AppArmor profile to add first
systemctl --user enable --now docker
sudo loginctl enable-linger <agent-user>   # the real login name; check with `whoami`
docker context use rootless
docker info --format '{{.SecurityOptions}}'   # must include name=rootless
```

The worker detects rootless Docker at startup and runs the container as `--user 0:0`. In rootless mode that is the worker's host user with no capabilities; any other container UID maps to a subordinate UID that cannot write the `0700` workspace. On rootful Docker the worker keeps the host UID/GID.

Check that the runner's resource limits and workspace mount work under this daemon:

```sh
mkdir -m 700 -p /tmp/agent-check && docker run --rm --user 0:0 --read-only --tmpfs /tmp \
  --cap-drop=ALL --security-opt=no-new-privileges --memory=2g --cpus=2 --pids-limit=256 \
  --mount type=bind,source=/tmp/agent-check,target=/task github-agent-pi:1.5.2 \
  sh -c 'touch /task/ok && pi --version' && ls -l /tmp/agent-check && rm -rf /tmp/agent-check
```

If `--cpus` fails, the user's systemd slice lacks the `cpu` cgroup controller. Add `Delegate=cpu cpuset io memory pids` under `[Service]` in `/etc/systemd/system/user@.service.d/delegate.conf`, then `sudo systemctl daemon-reload` and log in again. With rootless Docker, container traffic leaves as the worker user, so one rule blocks instance metadata: `sudo iptables -A OUTPUT -m owner --uid-owner <agent-user> -d 169.254.169.254 -j REJECT` (persist it with your firewall tooling).

The image includes Node.js, Pi, `git`, and `gh`. Add trusted project tooling to `runner/Dockerfile` if the test repository needs a different toolchain. The worker uses the local image and does not pull it on each job.

Run the webhook behind an HTTPS reverse proxy and the worker in a separate terminal. For unattended use, supervise both with systemd or an equivalent process manager. Run only one worker for the queue; the worker takes an exclusive lock. Use the same absolute `QUEUE_DB` path in both environments.

```sh
# Terminal 1
uv run --locked --no-dev --env-file .env uvicorn agent_worker.web:create_app --factory --host 127.0.0.1 --port 8000

# Terminal 2
uv run --locked --no-dev --env-file .env python -m agent_worker.worker
```

For an unattended install without test dependencies, run `uv sync --locked --no-dev` first. `GET /healthz` is the webhook server's liveness check. The web process needs no App private key or model credential; it can use a separate env file with just the webhook, allowlist, and database settings.

## Exercise pause and resume

1. Open an issue in the test repository. From an allowlisted account post a new issue comment:

   ```text
   @my-agent implement Update the README with a one-line example. Before editing, use ask_user to ask me two questions: what text to add and where to put it.
   ```

2. GitHub's webhook delivery log should show `202` with a job ID. Pi should post both questions in one issue comment. Confirm the worker logs `waiting for reply`. You can restart the worker now to test persistence.
3. Post **a new comment** with the answers (not an edit to the old comment):

   ```text
   @my-agent reply
   1. Add “Run npm test before opening a PR.”
   2. Put it under Development.
   ```

4. Verify Pi uses the same session and checkout under `PI_WORKSPACES`, pushes `agent/issue-N`, and creates a draft PR linked to issue `N`. Review the diff; the worker's `turn completed` means Pi finished responding, not that a PR is guaranteed to exist.

Issue creation alone does not start work. Edits, PR comments, bot senders, unrelated comments, and requests from outside the allowlists don't enqueue jobs. Duplicate webhook deliveries and comments are safely ignored. Only one batch can be pending per issue, and only allowlisted users can answer. A failed or interrupted job isn't silently retried: send a new `implement` comment, or a new `reply` if a posted question is still pending. For older installs, jobs queued by the webhook-only version are marked failed during migration; submit a new comment to run them.

### Troubleshooting

- No delivery: verify the reverse proxy route, GitHub App installation, and Issue comment subscription. Use GitHub's redelivery for failed requests.
- `ignored`: compare installation/repository/requester IDs and confirm this is a new comment on an **issue**, not a PR. `not_waiting` means there is no recorded question for that issue yet.
- Token request returns `403`/`422`: check the PEM and App ID, installation ID, and approved App permissions. The worker does not print tokens or provider transcripts.
- Container exits before a turn: verify Docker is running, the image was built, the provider key can use the selected model, and the App can read and write the repository. If a question was posted but its reply was rejected, check that the run reached `waiting for reply` before posting a new reply.

The runner can read its own short-lived GitHub token and model credential because it has shell access. Do not give this setup personal tokens, mount your home or Docker socket into the container, enable automerge, or expose it to untrusted repositories. The SQLite database stores submitted commands and replies; don't put credentials in issue comments.
