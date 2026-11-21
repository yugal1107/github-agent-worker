import hmac
import json
import re
import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .config import Settings
from .queue import IssueJob, JobQueue

MAX_WEBHOOK_BYTES = 1024 * 1024
MAX_TASK_CHARS = 4096
REPOSITORY = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,99}/(?!\.{1,2}\Z)[a-zA-Z0-9_.-]{1,100}\Z")


def obj(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def github_id(value: object) -> int:
    return value if type(value) is int and 0 < value < 10**18 else 0


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings if settings is not None else Settings.from_env()
    queue = JobQueue(settings.queue_db)
    command = re.compile(rf"{re.escape(settings.mention)}[ \t]+(.+)", re.IGNORECASE | re.DOTALL)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await run_in_threadpool(queue.initialize)
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def health() -> dict:
        return {"status": "ok"}

    @app.post("/webhooks/github")
    async def webhook(request: Request):
        signature = request.headers.get("x-hub-signature-256", "")
        if re.fullmatch(r"sha256=[0-9a-f]{64}", signature) is None:
            raise HTTPException(401, "Invalid signature")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_WEBHOOK_BYTES:
                raise HTTPException(413, "Webhook too large")
            body.extend(chunk)
        expected = "sha256=" + hmac.digest(settings.secret, body, "sha256").hex()
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(401, "Invalid signature")

        event = request.headers.get("x-github-event")
        if event == "ping":
            return {"status": "pong"}
        if event != "issue_comment":
            return {"status": "ignored"}
        delivery = request.headers.get("x-github-delivery", "")
        if re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", delivery) is None:
            raise HTTPException(400, "Invalid delivery ID")
        try:
            data = json.loads(body.decode("utf-8"))
        except (ValueError, RecursionError):
            raise HTTPException(400, "Invalid JSON") from None
        if not isinstance(data, dict):
            raise HTTPException(400, "Invalid webhook payload")

        issue = obj(data.get("issue"))
        if data.get("action") != "created" or "pull_request" in issue:
            return {"status": "ignored"}
        installation = github_id(obj(data.get("installation")).get("id"))
        repository = obj(data.get("repository"))
        repo = github_id(repository.get("id"))
        comment = obj(data.get("comment"))
        author = obj(comment.get("user"))
        sender = obj(data.get("sender"))
        requester = github_id(author.get("id"))
        if (installation != settings.installation_id or repo not in settings.repository_ids
                or requester not in settings.requester_ids or requester != github_id(sender.get("id"))
                or author.get("type") != "User" or sender.get("type") != "User"):
            return {"status": "ignored"}

        text = comment.get("body")
        match = command.fullmatch(text.strip()) if isinstance(text, str) else None
        if match is None:
            return {"status": "ignored"}
        task = match[1].strip()
        kind = "implement"
        verb = re.fullmatch(r"(implement|reply)(?:\s+(.+))?", task, re.IGNORECASE | re.DOTALL)
        if verb:
            kind = verb[1].lower()
            task = (verb[2] or "").strip()
            if kind == "implement" and not task:
                task = "Implement the request in the issue description."
        if not task or len(task) > MAX_TASK_CHARS or "\x00" in task:
            raise HTTPException(422, "Invalid task")
        try:
            task.encode("utf-8")
        except UnicodeEncodeError:
            raise HTTPException(422, "Invalid task") from None
        issue_number, comment_id = github_id(issue.get("number")), github_id(comment.get("id"))
        if not issue_number or not comment_id:
            raise HTTPException(400, "Missing issue or comment ID")
        full_name = repository.get("full_name")
        if not isinstance(full_name, str) or REPOSITORY.fullmatch(full_name) is None:
            raise HTTPException(400, "Invalid repository name")
        job = IssueJob(delivery.lower(), installation, repo, issue_number, comment_id, requester, task, full_name, kind)
        try:
            status, job_id = await run_in_threadpool(queue.enqueue, job)
        except sqlite3.Error:
            raise HTTPException(503, "Job queue unavailable") from None
        if job_id is None:
            return {"status": status}
        return JSONResponse({"status": "queued", "job_id": job_id}, status_code=202)

    return app
