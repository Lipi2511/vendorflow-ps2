from contextlib import closing
import json
import logging

from .ai import template_email, vendor_actions
from .policy import Check, compare_documents, field_checks, normalized, read_document, valid_gst
from .store import CaseConflict, fingerprint, now


def register_checks(db, workspace, form, tax):
    gst = normalized("gst", form["gst"])
    account = normalized("account", form["account"])
    record = db.execute("""SELECT * FROM suppliers WHERE workspace IN ('seed',?) AND gst=?
                           ORDER BY workspace='seed' DESC LIMIT 1""",
                        (workspace, fingerprint(gst))).fetchone()
    checks = []
    if record and record["status"] == "Blocked":
        corroborated = (valid_gst(gst) and gst == normalized("gst", tax.get("gst", "")) and
                        normalized("name", tax.get("name", "")) ==
                        normalized("name", record["name"]) == normalized("name", form["name"]))
        checks.append(Check("register.block", False,
                            f"{record['reference']} · Internal block. " +
                            ("Certificate and legal identity match." if corroborated else "Identity requires confirmation."),
                            severity="block" if corroborated else "procurement"))
    elif record:
        changed = record["account"] != fingerprint(account) or record["ifsc"] != normalized("ifsc", form["ifsc"])
        if changed and account and form["ifsc"].strip():
            detail = (f"{record['reference']} · Account on file •••• {record['ending']} / {record['ifsc']} · "
                      f"Submitted •••• {account[-4:]} / {form['ifsc']}. Verify through the established supplier contact.")
            checks.append(Check("register.bank_change", False, detail, severity="procurement"))
        else:
            checks.append(Check("register.duplicate", False, f"{record['reference']} · Existing supplier record.",
                                severity="procurement"))
    elif valid_gst(gst):
        checks.append(Check("register.new", True, "Supplier register searched. No matching tax identifier."))
    if account:
        shared = db.execute("""SELECT reference FROM suppliers WHERE workspace IN ('seed',?) AND account=? AND gst<>?
                               AND status='Active' ORDER BY reference LIMIT 1""",
                            (workspace, fingerprint(account), fingerprint(gst))).fetchone()
        if shared:
            checks.append(Check("register.shared_account", False,
                                f"Bank account already registered to {shared['reference']} (different GSTIN). Verify independently.",
                                severity="procurement"))
    return checks


def decide(checks):
    failures = {check.severity for check in checks if not check.passed}
    if "block" in failures:
        return "Rejected", "Procurement", "This application cannot proceed under the internal onboarding policy. Contact procurement with this reference to request a review."
    if "procurement" in failures:
        return "Pending", "Procurement", "Procurement needs to review this application. Please quote your application reference when contacting the team."
    if "vendor" in failures:
        actions = dict.fromkeys(c.action for c in checks if not c.passed and c.severity == "vendor" and c.action)
        return "Pending", "Vendor", " ".join(actions)
    return "Approved", "Complete", "Approved for internal onboarding. Bank ownership verification is required before payment."


def execute(store, workspace, token, form, tax_pdf, bank_pdf, *, ref=None, previous=0, notify=None, ai=None):
    if not form.get("name", "").strip() or not (tax_pdf or bank_pdf):
        raise CaseConflict("Enter a legal name and attach at least one document before running the checks.")
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
        if ai:
            tax, tax_check = read_document(tax_pdf, "tax", ai)
            bank, bank_check = read_document(bank_pdf, "bank", ai)
        else:
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
            checks.extend(register_checks(db, workspace, form, tax))
            decision, owner, message = decide(checks)
            if decision == "Approved":
                store.add_supplier(db, workspace, form, "Active", row["ref"])
            evidence = [c.data() for c in checks]
            actions = vendor_actions(evidence) if owner == "Vendor" else []
            if decision == "Rejected":
                actions = ["Contact procurement with this reference to request a review."]
            subject, body = template_email(row["ref"], actions) if actions else (None, None)
            db.execute("""UPDATE runs SET state='Complete',decision=?,owner=?,message=?,checks=?,email_subject=?,email_body=? WHERE id=?""",
                       (decision, owner, message, json.dumps(evidence), subject, body, run))
            db.execute("UPDATE events SET state='Complete',finished=? WHERE run=? AND stage=?", (now(), run, last))
        if ai and actions:
            codes = [c.code for c in checks if not c.passed and c.severity == "vendor"] if owner == "Vendor" else ["onboarding.review_route"]
            subject, body = ai.draft(row["ref"], codes, actions)
            with closing(store.connect()) as db, db:
                db.execute("UPDATE runs SET email_subject=?,email_body=? WHERE id=? AND owner=?",
                           (subject, body, run, owner))
        notify(last, "Complete")
    except Exception:
        logging.exception("Workflow interrupted for %s", row["ref"])
        store.fail(run, [c.data() for c in checks])
    return row["ref"]
