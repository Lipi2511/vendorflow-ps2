"""Workspace-isolated case history and internal supplier register."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from .ai import template_email, vendor_actions
from .examples import sample_form
from .policy import normalized


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def fingerprint(value):
    return hashlib.sha256(value.strip().upper().encode()).hexdigest()


class CaseConflict(ValueError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, workspace TEXT NOT NULL, ref TEXT NOT NULL, attempt INTEGER NOT NULL,
  token TEXT NOT NULL, name TEXT NOT NULL, started TEXT NOT NULL, state TEXT NOT NULL,
  decision TEXT, owner TEXT, message TEXT, checks TEXT NOT NULL DEFAULT '[]',
  UNIQUE(workspace,ref,attempt), UNIQUE(workspace,token)
);
CREATE INDEX IF NOT EXISTS case_history ON runs(workspace,ref,id);
CREATE TABLE IF NOT EXISTS events (
  run INTEGER NOT NULL REFERENCES runs(id), stage TEXT NOT NULL, started TEXT NOT NULL,
  finished TEXT, state TEXT NOT NULL, PRIMARY KEY(run,stage)
);
CREATE TABLE IF NOT EXISTS suppliers (
  workspace TEXT NOT NULL, gst TEXT NOT NULL, name TEXT NOT NULL, account TEXT NOT NULL,
  ending TEXT NOT NULL, ifsc TEXT NOT NULL, status TEXT NOT NULL, reference TEXT NOT NULL,
  PRIMARY KEY(workspace,gst)
);
CREATE INDEX IF NOT EXISTS supplier_accounts ON suppliers(account);
CREATE TABLE IF NOT EXISTS reviews (
  run INTEGER PRIMARY KEY REFERENCES runs(id), action TEXT NOT NULL, reviewer TEXT NOT NULL,
  note TEXT NOT NULL, timestamp TEXT NOT NULL, decision TEXT NOT NULL, owner TEXT NOT NULL,
  message TEXT NOT NULL, email_subject TEXT, email_body TEXT
);
CREATE TABLE IF NOT EXISTS supplier_history (
  review_run INTEGER PRIMARY KEY REFERENCES reviews(run), workspace TEXT NOT NULL, gst TEXT NOT NULL,
  reference TEXT NOT NULL, account TEXT NOT NULL, ending TEXT NOT NULL, ifsc TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get("VENDORFLOW_DB", ".runtime/vendorflow.sqlite3"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(runs)")}
            for column in ("email_subject", "email_body", "supplier_snapshot"):
                if column not in columns:
                    db.execute(f"ALTER TABLE runs ADD COLUMN {column} TEXT")
            for example, status, account, ref in [
                ("Changed bank account", "Active", "710000001824", "SUP-017"),
                ("Blocked supplier", "Blocked", "710000000580", "SUP-023"),
            ]:
                form = sample_form(example)
                self.add_supplier(db, "seed", form | {"account": account}, status, ref, seed=True)
            event_columns = {row["name"] for row in db.execute("PRAGMA table_info(events)")}
            if "summary" not in event_columns:
                db.execute("ALTER TABLE events ADD COLUMN summary TEXT")
            # Abandoned workers can be retried after a bounded processing window.
            stale = "state='Running' AND julianday(started)<julianday('now','-10 minutes')"
            db.execute(f"""UPDATE events SET state='Failed',finished=? WHERE state='Running'
                        AND run IN (SELECT id FROM runs WHERE {stale})""", (now(),))
            db.execute(f"""UPDATE runs SET state='Failed',owner='Operator',
                        message='Processing was interrupted. Retry the application.' WHERE {stale}""")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def add_supplier(db, workspace, form, status, ref, seed=False):
        command = "INSERT OR IGNORE" if seed else "INSERT"
        db.execute(f"{command} INTO suppliers VALUES(?,?,?,?,?,?,?,?)",
                   (workspace, fingerprint(form["gst"]), form["name"], fingerprint(normalized("account", form["account"])),
                    normalized("account", form["account"])[-4:], form["ifsc"].upper(), status, ref))

    def start(self, workspace, token, name, ref=None, previous=0):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM runs WHERE workspace=? AND token=?", (workspace, token)).fetchone()
            if prior:
                return prior, False
            if ref:
                last = db.execute("SELECT * FROM runs WHERE workspace=? AND ref=? ORDER BY id DESC LIMIT 1",
                                  (workspace, ref)).fetchone()
                if not last or last["attempt"] != previous:
                    raise CaseConflict("This application has changed. Open its latest attempt.")
                last = self.outcome(db, last)
                if last["state"] != "Failed" and (last["decision"], last["owner"]) != ("Pending", "Vendor"):
                    raise CaseConflict("This application does not accept vendor corrections. See the latest decision.")
            else:
                ref = "VF-" + uuid4().hex[:12].upper()
            received = now()
            cur = db.execute("""INSERT INTO runs(workspace,ref,attempt,token,name,started,state)
                             VALUES(?,?,?,?,?,?,'Running')""",
                             (workspace, ref, previous+1, token, name[:120] or "Unnamed applicant", received))
            db.execute("INSERT INTO events(run,stage,started,finished,state,summary) VALUES(?,?,?,?,?,?)",
                       (cur.lastrowid, "Receive", received, now(), "Complete", "Application received and recorded."))
            return db.execute("SELECT * FROM runs WHERE id=?", (cur.lastrowid,)).fetchone(), True

    def stage(self, run, name, state, summary=None):
        with closing(self.connect()) as db, db:
            if state == "Running":
                db.execute("INSERT INTO events(run,stage,started,state,summary) VALUES(?,?,?,?,?)", (run, name, now(), state, summary))
            else:
                self.finish_stage(db, run, name, state, summary)

    @staticmethod
    def finish_stage(db, run, name, state, summary):
        db.execute("UPDATE events SET state=?,finished=?,summary=? WHERE run=? AND stage=?",
                   (state, now(), summary, run, name))

    @staticmethod
    def outcome(db, row):
        result = dict(row)
        review = db.execute("SELECT * FROM reviews WHERE run=?", (row["id"],)).fetchone()
        result["automated_decision"] = row["decision"]
        result["automated_owner"] = row["owner"]
        result["review"] = dict(review) if review else None
        result["updated"] = review["timestamp"] if review else (
            db.execute("SELECT MAX(finished) FROM events WHERE run=?", (row["id"],)).fetchone()[0] or row["started"])
        if review:
            for field in ("decision", "owner", "message", "email_subject", "email_body"):
                result[field] = review[field]
        return result

    @staticmethod
    def supplier_for(db, workspace, gst):
        return db.execute("""SELECT * FROM suppliers WHERE workspace IN ('seed',?) AND gst=?
                             ORDER BY workspace='seed',workspace LIMIT 1""", (workspace, gst)).fetchone()

    def review(self, workspace, ref, attempt, action, reviewer, note):
        reviewer, note = reviewer.strip(), note.strip()
        if not reviewer or not note:
            raise CaseConflict("Enter a reviewer name and verification note.")
        options = {
            "approve": ("Approved", "Complete", "Procurement verified the application and approved internal onboarding."),
            "reject": ("Rejected", "Procurement", "Procurement declined the application. Contact the team with this reference to request a review."),
            "return": ("Pending", "Vendor", "Procurement returned the case for correction."),
        }
        if action not in options:
            raise CaseConflict("Select a valid review action.")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE workspace=? AND ref=? ORDER BY id DESC LIMIT 1",
                             (workspace, ref)).fetchone()
            if not row or row["attempt"] != attempt or row["state"] != "Complete":
                raise CaseConflict("This case changed. Open its latest attempt.")
            current = self.outcome(db, row)
            if (current["decision"], current["owner"]) != ("Pending", "Procurement") or current["review"]:
                raise CaseConflict("This case no longer has an open procurement review.")
            decision, owner, message = options[action]
            actions = vendor_actions(json.loads(row["checks"]))
            if action == "return":
                actions = actions or ["Contact procurement with this reference for the requested correction."]
                message = " ".join(actions)
            elif action == "reject":
                actions = ["Contact procurement with this reference to request a review."]
            elif actions:
                raise CaseConflict("Return the outstanding vendor corrections before approving this case.")
            subject, body = template_email(ref, actions) if actions else (None, None)
            timestamp = now()
            db.execute("INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (row["id"], action, reviewer[:120], note[:2000], timestamp, decision, owner, message, subject, body))
            if action == "approve":
                self.enroll_reviewed_supplier(db, row)
            db.execute("""INSERT INTO events(run,stage,started,finished,state,summary)
                          VALUES(?,?,?,?,?,?)""",
                       (row["id"], "Procurement review", timestamp, timestamp, "Complete", f"{decision} · {owner}"))

    def enroll_reviewed_supplier(self, db, row):
        if not row["supplier_snapshot"]:
            raise CaseConflict("Supplier details are unavailable for this older case. Ask the vendor to resubmit.")
        snapshot = json.loads(row["supplier_snapshot"])
        if not snapshot:
            raise CaseConflict("A valid GSTIN and bank account are required before enrollment.")
        workspace, gst = row["workspace"], snapshot["gst"]
        existing = self.supplier_for(db, workspace, gst)
        if existing:
            if existing["status"] == "Blocked":
                raise CaseConflict("The supplier has an active internal block.")
            if [existing["reference"], existing["account"], existing["ifsc"]] != snapshot["prior"]:
                raise CaseConflict("The supplier register changed after this application. Reconcile it before approval.")
            ref = existing["reference"]
            if (existing["account"], existing["ifsc"]) == (snapshot["account"], snapshot["ifsc"]):
                return
            db.execute("INSERT INTO supplier_history VALUES(?,?,?,?,?,?,?)",
                       (row["id"], workspace, gst, ref, existing["account"], existing["ending"], existing["ifsc"]))
        else:
            if snapshot["prior"]:
                raise CaseConflict("The previous supplier record is no longer available.")
            ref = row["ref"]
        db.execute("""INSERT INTO suppliers(workspace,gst,name,account,ending,ifsc,status,reference)
                      VALUES(?,?,?,?,?,?,'Active',?) ON CONFLICT(workspace,gst) DO UPDATE SET
                      account=excluded.account,ending=excluded.ending,ifsc=excluded.ifsc""",
                   (workspace, gst, row["name"], snapshot["account"], snapshot["ending"], snapshot["ifsc"], ref))

    def history(self, workspace, ref):
        with closing(self.connect()) as db:
            return [self.outcome(db, row) for row in db.execute(
                "SELECT * FROM runs WHERE workspace=? AND ref=? ORDER BY id DESC", (workspace, ref))]

    def latest(self, workspace):
        with closing(self.connect()) as db:
            return [self.outcome(db, row) for row in db.execute("""SELECT * FROM runs WHERE id IN (
                SELECT MAX(id) FROM runs WHERE workspace=? GROUP BY ref) ORDER BY id DESC""", (workspace,))]

    def events(self, run):
        with closing(self.connect()) as db:
            return list(db.execute("SELECT * FROM events WHERE run=? ORDER BY rowid", (run,)))

    def fail(self, run, checks):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE events SET state='Failed',finished=?,summary='Processing failed. Retry this application.' WHERE run=? AND state='Running'", (now(), run))
            db.execute("""UPDATE runs SET state='Failed',owner='Operator',message=?,checks=?
                        WHERE id=? AND state='Running'""",
                       ("Processing failed. Retry this application under the same reference.", json.dumps(checks), run))
