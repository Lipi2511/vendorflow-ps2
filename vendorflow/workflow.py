from contextlib import closing
import json
import logging

from .policy import Check, compare_documents, field_checks, normalized, read_document, valid_gst
from .store import fingerprint, now


def decide(form, tax, checks, record):
    gst = form["gst"].strip().upper()
    supported = valid_gst(gst) and gst == tax.get("gst", "").strip().upper()
    actions = list(dict.fromkeys(c.action for c in checks if not c.passed and c.action))
    if record and record["status"] == "Blocked":
        corroborated = (supported and normalized("name", tax.get("name", "")) ==
                        normalized("name", record["name"]) == normalized("name", form["name"]))
        checks.append(Check("register.block", False,
                            f"{record['reference']} · Internal block. " +
                            ("Certificate and legal identity match." if corroborated else "Identity requires confirmation.")))
        if corroborated:
            return "Rejected", "Procurement", "This application cannot proceed under the internal onboarding policy. Contact procurement with this reference to request a review."
        return "Pending", "Procurement", "Procurement needs to confirm the applicant’s identity. Please quote your application reference when contacting the team."
    if record:
        changed = (record["account"] != fingerprint(form["account"]) or
                   record["ifsc"] != form["ifsc"].strip().upper())
        if changed:
            detail = (f"{record['reference']} · Account on file •••• {record['ending']} / {record['ifsc']}; "
                      f"submitted •••• {form['account'][-4:]} / {form['ifsc']}. "
                      "Verify the change through the established supplier contact.")
            checks.append(Check("register.bank_change", False, detail))
            return "Pending", "Procurement", "Procurement is independently reviewing the banking details against an existing supplier record. No further upload is needed at this stage."
        checks.append(Check("register.duplicate", False, f"{record['reference']} · Existing supplier with matching banking details."))
        return "Pending", "Procurement", "An existing supplier record matches this application. Contact procurement with this reference to use or reconcile that record."
    checks.append(Check("register.new", True, "Supplier register searched. No matching tax identifier.") if valid_gst(gst)
                  else Check("register.unchecked", False, "A valid GSTIN is required to check the register."))
    if actions:
        return "Pending", "Vendor", " ".join(actions)
    return "Approved", "Complete", "Your application passed the internal onboarding checks and was recorded in the supplier register."


def execute(store, workspace, token, form, tax_pdf, bank_pdf, *, ref=None, previous=0, notify=None):
    row, created = store.start(workspace, token, form["name"], ref, previous)
    if not created:
        return row["ref"]
    run = row["id"]
    notify = notify or (lambda stage, state: None)
    checks = []

    def stage(name, work):
        store.stage(run, name, "Running")
        notify(name, "Running")
        result = work()
        store.stage(run, name, "Complete")
        notify(name, "Complete")
        return result

    def documents():
        tax, tax_check = read_document(tax_pdf, "tax")
        bank, bank_check = read_document(bank_pdf, "bank")
        checks.extend([tax_check, bank_check])
        return tax, bank

    try:
        notify("Receive application", "Complete")
        tax, bank = stage("Read uploaded documents", documents)
        stage("Validate form", lambda: checks.extend(field_checks(form)))
        stage("Compare evidence", lambda: checks.extend(compare_documents(form, tax, bank)))
        last = "Check supplier register and issue decision"
        store.stage(run, last, "Running")
        notify(last, "Running")
        # Serialize lookup with enrollment to prevent two simultaneous approvals for one GSTIN.
        with closing(store.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            record = db.execute("""SELECT * FROM suppliers WHERE workspace IN ('seed',?) AND gst=?
                                   ORDER BY workspace='seed' DESC LIMIT 1""",
                                (workspace, fingerprint(form["gst"]))).fetchone()
            decision, owner, message = decide(form, tax, checks, record)
            if decision == "Approved":
                store.add_supplier(db, workspace, form, "Active", row["ref"])
            db.execute("""UPDATE runs SET state='Complete',decision=?,owner=?,message=?,checks=? WHERE id=?""",
                       (decision, owner, message, json.dumps([c.data() for c in checks]), run))
            db.execute("UPDATE events SET state='Complete',finished=? WHERE run=? AND stage=?", (now(), run, last))
        notify(last, "Complete")
    except Exception:
        logging.exception("Workflow interrupted for %s", row["ref"])
        store.fail(run, [c.data() for c in checks])
    return row["ref"]
