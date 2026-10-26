"""SQLite separates session context, persistent claim history and review records."""
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "memory.sqlite3"
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, context TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, text TEXT, at TEXT);
            CREATE TABLE IF NOT EXISTS history(claim_id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, session_id TEXT, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, session_id TEXT, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY, batch_id TEXT, claim_key TEXT, body TEXT NOT NULL);
            ''')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def session(self, session_id=None):
        session_id = session_id or str(uuid.uuid4())
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO sessions VALUES (?,?)", (session_id, "{}"))
        return session_id

    def context(self, session_id):
        with self.connect() as db:
            row = db.execute("SELECT context FROM sessions WHERE id=?", (session_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def set_context(self, session_id, **values):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT context FROM sessions WHERE id=?", (session_id,)).fetchone()
            current = json.loads(row[0]) if row else {}
            current.update(values)
            db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?)", (session_id, json.dumps(current)))

    def message(self, session_id, role, text):
        with self.connect() as db:
            db.execute("INSERT INTO messages(session_id,role,text,at) VALUES (?,?,?,?)", (session_id, role, text, now()))

    def messages(self, session_id):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT role,text,at FROM messages WHERE session_id=? ORDER BY id", (session_id,))]

    def event(self, session_id, **event):
        event = {"at": now(), **event}
        with self.connect() as db:
            db.execute("INSERT INTO events(session_id,body) VALUES (?,?)", (session_id, json.dumps(event)))
        return event

    def events(self, session_id):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT body FROM events WHERE session_id=? ORDER BY id", (session_id,))]

    def seed(self, path):
        records = json.loads(Path(path).read_text())
        with self.connect() as db:
            for record in records:
                record = {**record, "source": "synthetic_seed"}
                db.execute("INSERT OR IGNORE INTO history VALUES (?,?)", (record["claim_id"], json.dumps(record)))

    def history(self, policy_number, before_date, exclude_claim_id):
        with self.connect() as db:
            records = [json.loads(r[0]) for r in db.execute("SELECT body FROM history")]
        return sorted([r for r in records if r.get("policy_number") == policy_number
                       and r.get("claim_id") != exclude_claim_id
                       and r.get("loss_date") and r["loss_date"] < before_date], key=lambda r: r["loss_date"], reverse=True)

    def save_batch(self, session_id, batch):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO batches VALUES (?,?,?)", (batch["batch_id"], session_id, json.dumps(batch)))
            # Persist valid observations, explicitly marked as awaiting review. Never store duplicate/invalid claims.
            for result in batch["results"]:
                if not result["validation"]["valid"] or result.get("error"):
                    continue
                record = {**result["claim"], "recommended_action": result["recommended_action"],
                          "review_status": result["review_status"], "source": "triage", "batch_id": batch["batch_id"]}
                db.execute("INSERT OR REPLACE INTO history VALUES (?,?)", (record["claim_id"], json.dumps(record)))

    def batch(self, batch_id):
        with self.connect() as db:
            row = db.execute("SELECT body FROM batches WHERE id=?", (batch_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def review(self, batch_id, claim_key, reviewer, decision, notes):
        if not reviewer.strip() or not notes.strip():
            raise ValueError("Reviewer and review notes are required.")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT body FROM batches WHERE id=?", (batch_id,)).fetchone()
            if not row:
                raise ValueError("Unknown batch.")
            batch = json.loads(row[0])
            result = next((r for r in batch["results"] if r["claim_key"] == claim_key), None)
            if not result:
                raise ValueError("Unknown claim row.")
            if result.get("error"):
                raise ValueError("A failed claim must be rerun before review.")
            if result["review_status"] != "pending_review":
                raise ValueError("This result has already been reviewed.")
            allowed = {"request_more_documentation", "route_to_investigator"}
            if result["recommended_action"] == "auto_approve" and result["validation"]["valid"]:
                allowed.add("auto_approve")
            if not result["validation"]["valid"]:
                allowed = {"request_more_documentation"}
            if decision not in allowed:
                raise ValueError("This decision violates the validation or anomaly review gate.")
            review = {"reviewer": reviewer.strip(), "decision": decision, "notes": notes.strip(), "at": now()}
            result.update(review_status="reviewed", review=review)
            result["briefing"] = result["briefing"].replace("Awaiting human review.", "Human review recorded.")
            batch["status"] = ("partial_failure" if any(r.get("error") for r in batch["results"]) else
                               "reviewed" if all(r["review_status"] == "reviewed" for r in batch["results"]) else "pending_review")
            db.execute("UPDATE batches SET body=? WHERE id=?", (json.dumps(batch), batch_id))
            db.execute("INSERT INTO reviews(batch_id,claim_key,body) VALUES (?,?,?)", (batch_id, claim_key, json.dumps(review)))
            previous = db.execute("SELECT body FROM history WHERE claim_id=?", (result["claim"].get("claim_id"),)).fetchone()
            if previous:
                hist = json.loads(previous[0])
                if hist.get("batch_id") == batch_id:
                    hist.update(review_status="reviewed", review=review)
                    db.execute("UPDATE history SET body=? WHERE claim_id=?", (json.dumps(hist), result["claim"]["claim_id"]))
        self.event(batch["session_id"], stage="human_review", batch_id=batch_id, claim_key=claim_key, **review)
        return result
