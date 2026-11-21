#!/bin/sh
set -eu
umask 077

mkdir -p "$HOME" /task/sessions
export GIT_TERMINAL_PROMPT=0 GH_PROMPT_DISABLED=1
export PI_TELEMETRY=0
url="https://github.com/$AGENT_REPOSITORY.git"
branch="agent/issue-$AGENT_ISSUE"

git config --global credential.https://github.com.helper '!gh auth git-credential'
git config --global user.name 'Issue Coding Agent'
git config --global user.email 'issue-coding-agent@users.noreply.github.com'

if [ ! -d /task/repo ]; then
    stage=$(mktemp -d /task/checkout-XXXXXX)
    trap 'rm -rf -- "$stage"' EXIT
    git clone --quiet -- "$url" "$stage" </dev/null >&2
    git -C "$stage" switch --quiet -c "$branch" </dev/null >&2
    mv -- "$stage" /task/repo
    trap - EXIT
fi

cd /task/repo
[ "$(git config --local --get remote.origin.url)" = "$url" ] || {
    echo 'The workspace does not match the requested repository.' >&2
    exit 1
}

set -- --session-id "$AGENT_SESSION_ID"
if [ "$AGENT_REQUIRE_SESSION" = 1 ]; then
    # --session-id creates a session if missing, which would lose the question history.
    set -- /task/sessions/*_"$AGENT_SESSION_ID".jsonl
    [ "$#" -eq 1 ] && [ -f "$1" ] || {
        echo 'Cannot resume: expected one saved Pi session.' >&2
        exit 1
    }
    set -- --session "$1"
fi

# The GitHub comment arrives as piped stdin, never as flags or @file arguments.
exec pi --mode json --session-dir /task/sessions "$@" \
    --model "$AGENT_MODEL" \
    --offline --no-approve --no-mcp --no-extensions --no-skills --no-prompt-templates \
    --extension /opt/issue-agent/ask-user.ts \
    --append-system-prompt /opt/issue-agent/instructions.md
