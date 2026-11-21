import os
import re
from dataclasses import dataclass
from pathlib import Path


def positive_id(value: str) -> int:
    if re.fullmatch(r"[1-9][0-9]{0,17}", value.strip()) is None:
        raise ValueError("GitHub IDs must be positive integers (up to 18 digits)")
    return int(value)


@dataclass(frozen=True)
class Settings:
    secret: bytes
    mention: str
    installation_id: int
    repository_ids: frozenset[int]
    requester_ids: frozenset[int]
    queue_db: Path

    @classmethod
    def from_env(cls) -> "Settings":
        secret = os.environ.get("WEBHOOK_SECRET", "").encode()
        if len(secret) < 32 or secret.startswith(b"replace-with-"):
            raise ValueError("Set WEBHOOK_SECRET to a strong random value")
        mention = os.environ.get("AGENT_MENTION", "@my-agent")
        if re.fullmatch(r"@[a-zA-Z0-9][a-zA-Z0-9-]{0,62}(?:\[bot\])?", mention) is None:
            raise ValueError("Set AGENT_MENTION to a GitHub-style @mention")
        return cls(
            secret=secret,
            mention=mention,
            installation_id=positive_id(os.environ.get("GITHUB_INSTALLATION_ID", "")),
            repository_ids=frozenset(map(positive_id, os.environ.get("ALLOWED_REPOSITORY_IDS", "").split(","))),
            requester_ids=frozenset(map(positive_id, os.environ.get("ALLOWED_REQUESTER_IDS", "").split(","))),
            queue_db=Path(os.environ.get("QUEUE_DB", "state/jobs.sqlite3")),
        )
