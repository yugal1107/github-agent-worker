import asyncio
import fcntl
import logging
import os
import shutil
import time
from pathlib import Path

import httpx

from .config import Settings, positive_id
from .github import AppTokens
from .pi_runner import PiSettings, execute
from .queue import JobQueue

logger = logging.getLogger(__name__)


def serve() -> None:
    if os.getuid() == 0:
        raise ValueError('Run the worker as a dedicated non-root user')
    if not shutil.which('docker'):
        raise ValueError('Docker CLI not found')
    settings = Settings.from_env()
    pi = PiSettings.from_env()
    app_id = positive_id(os.environ.get('GITHUB_APP_ID', ''))
    key = os.environ.get('GITHUB_APP_PRIVATE_KEY_FILE')
    if not key:
        raise ValueError('Set GITHUB_APP_PRIVATE_KEY_FILE to the PEM file path')
    queue = JobQueue(settings.queue_db)
    queue.initialize()
    with settings.queue_db.resolve().with_suffix('.worker.lock').open('a') as lock, httpx.Client(timeout=15, trust_env=False) as client:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('A worker is already using this queue') from None
        tokens = AppTokens(client, app_id, Path(key))
        queue.recover_interrupted()
        logger.info('Pi worker listening for queued jobs')
        while True:
            run = queue.claim()
            if run is None:
                time.sleep(1)
                continue
            success, token, stage = False, None, 'authorization'
            try:
                if (run.installation_id != settings.installation_id or run.repository_id not in settings.repository_ids
                        or run.requester_id not in settings.requester_ids):
                    raise ValueError('Job is no longer allowlisted')
                stage = 'GitHub App token'
                token = tokens.mint(run.installation_id, run.repository_id)
                stage = 'Pi runner'
                result = asyncio.run(execute(pi, run, settings.mention, token, lambda: queue.mark_waiting(run)))
                success = True
                logger.info('Job %s: %s', run.id, result)
            except httpx.HTTPStatusError as exc:
                logger.warning('Job %s failed at %s (HTTP %s)', run.id, stage, exc.response.status_code)
            except RuntimeError as exc:
                logger.warning('Job %s failed at %s: %s', run.id, stage, exc)
            except Exception as exc:
                # Third-party errors can include credentials or model output.
                logger.warning('Job %s failed at %s (%s)', run.id, stage, type(exc).__name__)
            finally:
                queue.finish(run, success)
                if token:
                    try:
                        tokens.revoke(token)
                    except httpx.HTTPError:
                        logger.warning('Job %s: token revocation failed; token will expire', run.id)


if __name__ == '__main__':
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        serve()
    except KeyboardInterrupt:
        pass
    except (ValueError, OSError) as exc:
        raise SystemExit(f'Worker startup failed: {exc}') from None
