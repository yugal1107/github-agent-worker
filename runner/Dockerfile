FROM node:24-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates gh git openssh-client ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global --ignore-scripts @earendil-works/pi-coding-agent@1.5.2

COPY run.sh ask-user.ts instructions.md /opt/issue-agent/
ENV HOME=/tmp/agent-home
WORKDIR /task
