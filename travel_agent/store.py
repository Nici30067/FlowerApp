"""SQLite persistence, compare-and-swap revisions, idempotent jobs and scoped worker capabilities."""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from travel_agent.schemas import Proposal, TripEvent, TripSnapshot


class Conflict(RuntimeError):
    pass


class NotFound(RuntimeError):
    pass


class Store:
    def __init__(self, path: str):
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS trips(id TEXT PRIMARY KEY, revision INTEGER, data TEXT);
                CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY, trip_id TEXT, base INTEGER, status TEXT, data TEXT);
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, trip_id TEXT, base INTEGER, status TEXT,
                    error TEXT, proposal_id TEXT, created REAL, input_data TEXT, token_hash TEXT, expires REAL,
                    claimed INTEGER DEFAULT 0, external INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS triggers(trip_id TEXT, event_id TEXT, digest TEXT, job_id TEXT,
                    PRIMARY KEY(trip_id,event_id));
                CREATE TABLE IF NOT EXISTS events(job_id TEXT, seq INTEGER, event_type TEXT, data TEXT,
                    PRIMARY KEY(job_id,seq));
            ''')
        try:
            Path(self.path).chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def create_trip(self, snapshot: TripSnapshot):
        with self.connection() as db:
            db.execute("INSERT INTO trips VALUES(?,?,?)", (snapshot.id, snapshot.revision, snapshot.model_dump_json()))

    def trip(self, trip_id: str) -> TripSnapshot:
        with self.connection() as db:
            row = db.execute("SELECT data FROM trips WHERE id=?", (trip_id,)).fetchone()
        if not row:
            raise NotFound("Trip not found")
        return TripSnapshot.model_validate_json(row[0])

    def trip_list(self) -> list[dict]:
        with self.connection() as db:
            rows = db.execute("SELECT id,revision,data FROM trips ORDER BY rowid DESC LIMIT 30").fetchall()
        return [{"id": r["id"], "revision": r["revision"], "title": json.loads(r["data"])["request"]["title"]} for r in rows]

    def enqueue(self, snapshot: TripSnapshot, event: TripEvent | None, external: bool) -> tuple[dict, str | None]:
        event_id = event.id if event else f"plan-{snapshot.revision}"
        payload = {"snapshot": snapshot.model_dump(mode="json"),
                   "event": event.model_dump(mode="json") if event else None}
        digest = hashlib.sha256(json.dumps(payload["event"], sort_keys=True).encode()).hexdigest()
        token = secrets.token_urlsafe(32)
        job_id = uuid4().hex
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT digest,job_id FROM triggers WHERE trip_id=? AND event_id=?",
                             (snapshot.id, event_id)).fetchone()
            if old:
                if old["digest"] != digest:
                    raise Conflict("An event ID was reused with different content")
                row = db.execute("SELECT * FROM jobs WHERE id=?", (old["job_id"],)).fetchone()
                return self._public_job(row), None
            current = db.execute("SELECT revision FROM trips WHERE id=?", (snapshot.id,)).fetchone()
            if not current or current[0] != snapshot.revision:
                raise Conflict("The trip revision changed before job submission")
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, snapshot.id, snapshot.revision, "queued", None, None, time.time(), json.dumps(payload),
                 hashlib.sha256(token.encode()).hexdigest(), time.time() + 1800, 0, int(external)))
            db.execute("INSERT INTO triggers VALUES(?,?,?,?)", (snapshot.id, event_id, digest, job_id))
        return self.job(job_id), token

    @staticmethod
    def _public_job(row) -> dict:
        return {k: row[k] for k in ("id", "trip_id", "base", "status", "error", "proposal_id", "external")}

    def job(self, job_id: str) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise NotFound("Job not found")
        return self._public_job(row)

    def job_input(self, job_id: str) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT input_data FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise NotFound("Job not found")
        return json.loads(row[0])

    def start_job(self, job_id: str):
        with self.connection() as db:
            cur = db.execute("UPDATE jobs SET status='running' WHERE id=? AND status='queued'", (job_id,))
            if cur.rowcount != 1:
                raise Conflict("Job is no longer queued")

    def append_event(self, job_id: str, event_type: str, data: dict):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE job_id=?", (job_id,)).fetchone()[0]
            db.execute("INSERT INTO events VALUES(?,?,?,?)", (job_id, seq, event_type, json.dumps(data)))

    def events(self, job_id: str, since: int = 0) -> list[dict]:
        with self.connection() as db:
            rows = db.execute("SELECT seq,event_type,data FROM events WHERE job_id=? AND seq>? ORDER BY seq LIMIT 200",
                              (job_id, since)).fetchall()
        return [{"seq": r["seq"], "type": r["event_type"], "data": json.loads(r["data"])} for r in rows]

    def finish_job(self, job_id: str, proposal: Proposal, auto_accept: bool = False):
        from travel_agent.planning.engine import validate
        expected = self.job_input(job_id)
        original = TripSnapshot.model_validate(expected["snapshot"])
        if proposal.trip_id != original.id or proposal.base_revision != original.revision:
            raise Conflict("Proposal does not match the assigned trip and base revision")
        if proposal.proposed.id != original.id or proposal.proposed.revision != original.revision + 1:
            raise Conflict("Invalid proposed revision")
        from travel_agent.coordinator import apply_event
        expected_state = apply_event(original, TripEvent.model_validate(expected["event"])) if expected["event"] else original
        if (proposal.proposed.request != expected_state.request or proposal.proposed.progress != expected_state.progress
                or proposal.proposed.closed_place_ids != expected_state.closed_place_ids):
            raise Conflict("Worker changed user constraints or progress outside the assigned event")
        if proposal.proposed.data_mode != original.data_mode:
            raise Conflict("Worker changed the assigned data provenance mode")
        # Worker output is independently checked before entering the review queue.
        if proposal.proposed.itinerary and proposal.proposed.itinerary.validation.valid:
            check = validate(proposal.proposed, proposal.proposed.itinerary)
            if not check.valid:
                raise Conflict("Worker result failed independent validation")
        # Auto-accept (the initial build) is committed in the same transaction as finish_job so
        # that a concurrent poller never observes the transient 'awaiting_review' state.
        if auto_accept and not (proposal.proposed.itinerary and proposal.proposed.itinerary.validation.valid
                                 and validate(proposal.proposed, proposal.proposed.itinerary).valid):
            auto_accept = False
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job or job[0] != "running":
                raise Conflict("Job has already finished or was not started")
            current = db.execute("SELECT revision FROM trips WHERE id=?", (original.id,)).fetchone()
            if current[0] != original.revision:
                raise Conflict("The trip changed while this job was running")
            if auto_accept:
                updated = db.execute("UPDATE trips SET revision=?,data=? WHERE id=? AND revision=?",
                    (proposal.proposed.revision, proposal.proposed.model_dump_json(), proposal.trip_id, proposal.base_revision))
                if updated.rowcount != 1:
                    raise Conflict("Stale proposal: refresh the current trip before retrying")
                db.execute("INSERT INTO proposals VALUES(?,?,?,?,?)",
                    (proposal.id, proposal.trip_id, proposal.base_revision, "accepted", proposal.model_dump_json()))
                db.execute("UPDATE jobs SET status='completed',proposal_id=?,token_hash='' WHERE id=?",
                           (proposal.id, job_id))
            else:
                db.execute("INSERT INTO proposals VALUES(?,?,?,?,?)",
                    (proposal.id, proposal.trip_id, proposal.base_revision, "pending", proposal.model_dump_json()))
                db.execute("UPDATE jobs SET status='awaiting_review',proposal_id=?,token_hash='' WHERE id=?",
                           (proposal.id, job_id))

    def fail_job(self, job_id: str, message: str):
        with self.connection() as db:
            db.execute("UPDATE jobs SET status='failed',error=?,token_hash='' WHERE id=?",
                       (message[:500], job_id))
        self.append_event(job_id, "job.failed", {"error": message[:500]})

    def proposal(self, proposal_id: str) -> Proposal:
        with self.connection() as db:
            row = db.execute("SELECT data,status FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        if not row:
            raise NotFound("Proposal not found")
        result = Proposal.model_validate_json(row["data"])
        result.status = row["status"]
        return result

    def proposals(self, trip_id: str) -> list[dict]:
        with self.connection() as db:
            rows = db.execute("SELECT id FROM proposals WHERE trip_id=? AND status='pending' ORDER BY rowid DESC LIMIT 10",
                              (trip_id,)).fetchall()
        return [self.proposal(row[0]).model_dump(mode="json") for row in rows]

    def accept(self, proposal_id: str) -> TripSnapshot:
        from travel_agent.planning.engine import validate
        proposal = self.proposal(proposal_id)
        proposed = proposal.proposed
        if not proposed.itinerary or not proposed.itinerary.validation.valid or not validate(proposed, proposed.itinerary).valid:
            raise Conflict("An infeasible proposal cannot be committed")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            if row[0] != "pending":
                raise Conflict("Proposal is no longer pending")
            updated = db.execute("UPDATE trips SET revision=?,data=? WHERE id=? AND revision=?",
                (proposed.revision, proposed.model_dump_json(), proposal.trip_id, proposal.base_revision))
            if updated.rowcount != 1:
                raise Conflict("Stale proposal: refresh the current trip before retrying")
            db.execute("UPDATE proposals SET status='accepted' WHERE id=?", (proposal_id,))
            db.execute("UPDATE jobs SET status='completed' WHERE proposal_id=?", (proposal_id,))
        return proposed

    def reject(self, proposal_id: str):
        with self.connection() as db:
            changed = db.execute("UPDATE proposals SET status='rejected' WHERE id=? AND status='pending'", (proposal_id,))
            if changed.rowcount != 1:
                raise Conflict("Proposal is no longer pending")
            db.execute("UPDATE jobs SET status='completed' WHERE proposal_id=?", (proposal_id,))

    def verify_worker(self, job_id: str, token: str, claim: bool = False):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if (not row or not row["external"] or not row["token_hash"] or row["expires"] < time.time()
                or not secrets.compare_digest(row["token_hash"], hashlib.sha256(token.encode()).hexdigest())):
                raise PermissionError("Invalid or expired job capability")
            if claim:
                if row["claimed"] or row["status"] != "queued":
                    raise Conflict("Job was already claimed")
                db.execute("UPDATE jobs SET claimed=1,status='running' WHERE id=?", (job_id,))
            elif row["status"] != "running" or not row["claimed"]:
                raise Conflict("Claim the job before publishing worker output")
