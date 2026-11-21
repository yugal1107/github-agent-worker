import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


@dataclass(frozen=True)
class IssueJob:
    delivery_id: str
    installation_id: int
    repository_id: int
    issue_number: int
    comment_id: int
    requester_id: int
    task: str
    repository: str
    kind: str


@dataclass(frozen=True)
class Run:
    id: int
    installation_id: int
    repository_id: int
    issue_number: int
    requester_id: int
    task: str
    repository: str
    kind: str
    session_id: str

    @property
    def issue_key(self) -> tuple[int, int]:
        return self.repository_id, self.issue_number


class JobQueue:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def initialize(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY,
                    delivery_id TEXT NOT NULL UNIQUE,
                    installation_id INTEGER NOT NULL,
                    repository_id INTEGER NOT NULL,
                    issue_number INTEGER NOT NULL,
                    comment_id INTEGER NOT NULL,
                    requester_id INTEGER NOT NULL,
                    task TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    repository TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT 'implement',
                    UNIQUE (repository_id, comment_id)
                )
            """)
            columns = {row['name'] for row in db.execute('PRAGMA table_info(jobs)')}
            if 'repository' not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN repository TEXT NOT NULL DEFAULT ''")
                # Webhook-only jobs lacked a clone target. Do not run them on upgrade.
                db.execute("UPDATE jobs SET status = 'failed' WHERE status = 'queued'")
            if 'kind' not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'implement'")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status, id)")
            db.execute("""
                CREATE TABLE IF NOT EXISTS issue_tasks (
                    repository_id INTEGER NOT NULL,
                    issue_number INTEGER NOT NULL,
                    session_id TEXT NOT NULL UNIQUE,
                    waiting INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (repository_id, issue_number)
                )
            """)

    def enqueue(self, job: IssueJob) -> tuple[str, int | None]:
        key = job.repository_id, job.issue_number
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM jobs WHERE delivery_id = ? OR (repository_id = ? AND comment_id = ?)",
                          (job.delivery_id, job.repository_id, job.comment_id)).fetchone():
                return "duplicate", None
            record = db.execute("SELECT waiting FROM issue_tasks WHERE repository_id = ? AND issue_number = ?", key).fetchone()
            waiting = bool(record and record[0])
            if job.kind == "reply" and not waiting:
                return "not_waiting", None
            if job.kind == "implement" and waiting:
                return "waiting_for_reply", None
            if db.execute("""SELECT 1 FROM jobs WHERE repository_id = ? AND issue_number = ?
                    AND (status = 'queued' OR (status = 'running' AND ? = 'implement'))""", (*key, job.kind)).fetchone():
                return "busy", None
            row = db.execute(
                """INSERT INTO jobs (delivery_id, installation_id, repository_id, issue_number,
                    comment_id, requester_id, task, repository, kind)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
                (job.delivery_id, job.installation_id, job.repository_id, job.issue_number,
                 job.comment_id, job.requester_id, job.task, job.repository, job.kind),
            ).fetchone()
            return "queued", row[0]

    def claim(self) -> Run | None:
        # Only a worker holding the process lock may claim a job.
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("""SELECT id, installation_id, repository_id, issue_number, requester_id,
                                   task, repository, kind FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1""").fetchone()
            if job is None:
                return None
            task = db.execute("""INSERT INTO issue_tasks (repository_id, issue_number, session_id)
                       VALUES (?, ?, ?) ON CONFLICT(repository_id, issue_number) DO UPDATE SET waiting = 0
                       RETURNING session_id""", (job['repository_id'], job['issue_number'], uuid4().hex)).fetchone()
            db.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job['id'],))
            return Run(**dict(job), session_id=task['session_id'])

    def mark_waiting(self, run: Run) -> None:
        with closing(self.connect()) as db, db:
            db.execute("UPDATE issue_tasks SET waiting = 1 WHERE repository_id = ? AND issue_number = ?", run.issue_key)

    def finish(self, run: Run, success: bool) -> None:
        with closing(self.connect()) as db, db:
            db.execute("UPDATE jobs SET status = ? WHERE id = ?", ('done' if success else 'failed', run.id))
            if not success and run.kind == 'reply':
                db.execute("UPDATE issue_tasks SET waiting = 1 WHERE repository_id = ? AND issue_number = ?", run.issue_key)

    def recover_interrupted(self) -> None:
        with closing(self.connect()) as db, db:
            db.execute("""UPDATE issue_tasks SET waiting = 1 WHERE (repository_id, issue_number) IN
                         (SELECT repository_id, issue_number FROM jobs WHERE status = 'running' AND kind = 'reply')""")
            db.execute("UPDATE jobs SET status = 'failed' WHERE status = 'running'")
