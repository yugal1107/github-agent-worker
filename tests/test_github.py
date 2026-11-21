import json

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from agent_worker.github import AppTokens


def test_token_is_scoped_to_only_the_requested_repository(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = tmp_path / 'test.pem'
    pem.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()))

    def api(request):
        if request.method == 'POST':
            assert request.url == 'https://api.github.com/app/installations/123/access_tokens'
            signed = request.headers['authorization'].removeprefix('Bearer ')
            assert jwt.decode(signed, key.public_key(), algorithms=['RS256'])['iss'] == '7'
            assert json.loads(request.content) == {
                'repository_ids': [456],
                'permissions': {'contents': 'write', 'issues': 'write', 'pull_requests': 'write'},
            }
            return httpx.Response(201, json={'token': 'fake-token'})
        assert request.method == 'DELETE' and request.url == 'https://api.github.com/installation/token'
        assert request.headers['authorization'] == 'Bearer fake-token'
        return httpx.Response(204)

    with httpx.Client(transport=httpx.MockTransport(api)) as client:
        tokens = AppTokens(client, 7, pem)
        assert tokens.mint(123, 456) == 'fake-token'
        tokens.revoke('fake-token')
