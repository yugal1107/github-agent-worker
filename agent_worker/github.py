import time
from pathlib import Path

import httpx
import jwt


class AppTokens:
    def __init__(self, client: httpx.Client, app_id: int, key_file: Path):
        self.client = client
        self.app_id = app_id
        self.key = key_file.read_bytes()

    def mint(self, installation_id: int, repository_id: int) -> str:
        now = int(time.time())
        assertion = jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(self.app_id)}, self.key, algorithm="RS256")
        response = self.client.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {assertion}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
            json={"repository_ids": [repository_id],
                  "permissions": {"contents": "write", "issues": "write", "pull_requests": "write"}},
        )
        response.raise_for_status()
        token = response.json()["token"]
        if not isinstance(token, str) or not token.strip():
            raise ValueError("Invalid installation token response")
        return token

    def revoke(self, token: str) -> None:
        response = self.client.delete("https://api.github.com/installation/token",
                                      headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
