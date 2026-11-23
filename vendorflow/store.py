"""Workspace-isolated case history and internal supplier register."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

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
"""


class Store:
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get("VENDORFLOW_DB", ".runtime/vendorflow.sqlite3"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(runs)")}
            for column in ("email_subject", "email_body"):
                if column not in columns:
                    db.execute(f"ALTER TABLE runs ADD COLUMN {column} TEXT")
            for example, status, account, ref in [
                ("Changed bank account", "Active", "710000001824", "SUP-017"),
                ("Blocked supplier", "Blocked", "710000000580", "SUP-023"),
            ]:
                form = sample_form(example)
                self.add_supplier(db, "seed", form | {"account": account}, status, ref, seed=True)
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
                if last["state"] != "Failed" and (last["decision"], last["owner"]) != ("Pending", "Vendor"):
                    raise CaseConflict("This application does not accept vendor corrections. See the latest decision.")
            else:
                ref = "VF-" + uuid4().hex[:12].upper()
            received = now()
            cur = db.execute("""INSERT INTO runs(workspace,ref,attempt,token,name,started,state)
                             VALUES(?,?,?,?,?,?,'Running')""",
                             (workspace, ref, previous+1, token, name[:120] or "Unnamed applicant", received))
            db.execute("INSERT INTO events VALUES(?,?,?,?,?)",
                       (cur.lastrowid, "Receive application", received, now(), "Complete"))
            return db.execute("SELECT * FROM runs WHERE id=?", (cur.lastrowid,)).fetchone(), True

    def stage(self, run, name, state):
        with closing(self.connect()) as db, db:
            if state == "Running":
                db.execute("INSERT INTO events VALUES(?,?,?,NULL,?)", (run, name, now(), state))
            else:
                db.execute("UPDATE events SET state=?,finished=? WHERE run=? AND stage=?",
                           (state, now(), run, name))

    def history(self, workspace, ref):
        with closing(self.connect()) as db:
            return list(db.execute("SELECT * FROM runs WHERE workspace=? AND ref=? ORDER BY id DESC",
                                   (workspace, ref)))

    def latest(self, workspace):
        with closing(self.connect()) as db:
            return list(db.execute("""SELECT * FROM runs WHERE id IN (
                         SELECT MAX(id) FROM runs WHERE workspace=? GROUP BY ref) ORDER BY id DESC""", (workspace,)))

    def events(self, run):
        with closing(self.connect()) as db:
            return list(db.execute("SELECT * FROM events WHERE run=? ORDER BY rowid", (run,)))

    def fail(self, run, checks):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE events SET state='Failed',finished=? WHERE run=? AND state='Running'", (now(), run))
            db.execute("""UPDATE runs SET state='Failed',owner='Operator',message=?,checks=?
                        WHERE id=? AND state='Running'""",
                       ("Processing failed. Retry this application under the same reference.", json.dumps(checks), run))
