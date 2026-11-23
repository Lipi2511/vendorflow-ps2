from contextlib import closing
from hashlib import sha256
import json
import logging

from .ai import template_email, vendor_actions
from .policy import LABELS, Check, compare_documents, field_checks, normalized, read_document, valid_gst
from .store import CaseConflict, Store, fingerprint


def register_checks(db, workspace, form, tax):
    gst = normalized("gst", form["gst"])
    account = normalized("account", form["account"])
    record = Store.supplier_for(db, workspace, fingerprint(gst))
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
        shared = db.execute("""SELECT reference FROM suppliers AS supplier
                               WHERE supplier.workspace IN ('seed',?) AND supplier.account=? AND supplier.gst<>?
                               AND supplier.status='Active'
                               AND (supplier.workspace<>'seed' OR NOT EXISTS (
                                   SELECT 1 FROM suppliers AS local WHERE local.workspace=? AND local.gst=supplier.gst
                               )) ORDER BY reference LIMIT 1""",
                            (workspace, fingerprint(account), fingerprint(gst), workspace)).fetchone()
        if shared:
            checks.append(Check("register.shared_account", False,
                                f"Bank account already registered to {shared['reference']} (different GSTIN). Verify independently.",
                                severity="procurement"))
    return checks


def supplier_snapshot(db, workspace, form):
    gst, account, ifsc = (normalized(key, form[key]) for key in ("gst", "account", "ifsc"))
    if not valid_gst(gst) or not account or not ifsc:
        return {}
    gst_key = fingerprint(gst)
    existing = Store.supplier_for(db, workspace, gst_key)
    prior = [existing[key] for key in ("reference", "account", "ifsc")] if existing else None
    return dict(gst=gst_key, account=fingerprint(account), ending=account[-4:], ifsc=ifsc, prior=prior)


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


STAGES = ("Receive", "Read documents", "Validate form", "Cross-check evidence",
          "Fraud & register checks", "Decide & draft message")


def check_summary(checks):
    issues = sum(not check.passed for check in checks)
    if not checks:
        return "No additional comparisons were available."
    count = len(checks)
    label = "check" if count == 1 else "checks"
    observation = "observation" if issues == 1 else "observations"
    return f"{count} {label} · {issues} {observation}" if issues else f"{count} {label} passed."


def reading_summary(checks):
    entries = []
    for title, check, required in zip(("GST certificate", "Bank letter"), checks,
                                      (("name", "gst"), ("holder", "account", "ifsc"))):
        readings = check.readings or {}
        found = [readings[field] for field in required if field in readings]
        assisted = [entry["confidence"] for entry in found if entry["method"] != "regex"]
        method = f" · AI ({min(assisted):.2f})" if assisted else ""
        entries.append(f"{title}: {len(found)}/{len(required)} fields{method}")
    return " | ".join(entries)


def submission_digest(form, tax_pdf, bank_pdf):
    fields = {key: normalized(key, form.get(key, "")) for key in LABELS}
    uploads = [sha256(data).hexdigest() if data else None for data in (tax_pdf, bank_pdf)]
    return sha256(json.dumps([fields, uploads], sort_keys=True).encode()).hexdigest()


def raise_repeat_hold(db, workspace, row, checks):
    repeats = Store.vendor_attempts(db, workspace, row["ref"], row["attempt"]) if row["attempt"] > 3 else 0
    if repeats >= 3 and decide(checks)[:2] == ("Pending", "Vendor"):
        checks.append(Check("case.repeated_corrections", False,
                            "Repeated unsuccessful corrections. Procurement review needed.", severity="procurement"))


def execute(store, workspace, token, form, tax_pdf, bank_pdf, *, ref=None, previous=0, notify=None, ai=None):
    if not form.get("name", "").strip() or not (tax_pdf or bank_pdf):
        raise CaseConflict("Enter a legal name and attach at least one document before running the checks.")
    row, created = store.start(workspace, token, form["name"], ref, previous, submission_digest(form, tax_pdf, bank_pdf))
    if not created:
        return row["ref"]
    run = row["id"]
    notify = notify or (lambda stage, state, summary: None)
    checks = []

    def stage(name, work, summarize=check_summary):
        nonlocal active
        active = name
        store.stage(run, name, "Running")
        notify(name, "Running", "")
        before = len(checks)
        result = work()
        findings = checks[before:]
        summary = summarize(findings)
        state = "Issues" if any(not c.passed for c in findings) else "Complete"
        store.stage(run, name, state, summary)
        notify(name, state, summary)
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

    active = STAGES[0]
    try:
        notify(active, "Complete", "Application received and recorded.")
        tax, bank = stage(STAGES[1], documents, reading_summary)
        stage(STAGES[2], lambda: checks.extend(field_checks(form)))
        stage(STAGES[3], lambda: checks.extend(compare_documents(form, tax, bank)))

        def register():
            with closing(store.connect()) as db:
                checks.extend(register_checks(db, workspace, form, tax))

        before = len(checks)
        stage(STAGES[4], register)
        last = active = STAGES[5]
        store.stage(run, last, "Running")
        notify(last, "Running", "")
        # Prepare optional wording without holding a write lock. Recheck register atomically at enrollment.
        decision, owner, message = decide(checks)
        draft_actions = vendor_actions([c.data() for c in checks]) if owner == "Vendor" else []
        draft_codes = [c.code for c in checks if not c.passed and c.severity == "vendor"]
        if decision == "Rejected":
            draft_actions = ["Contact procurement with this reference to request a review."]
            draft_codes = ["onboarding.review_route"]
        draft = ai.draft(row["ref"], draft_codes, draft_actions) if ai and draft_actions else None
        with closing(store.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            checks[before:] = register_checks(db, workspace, form, tax)
            raise_repeat_hold(db, workspace, row, checks)
            risk_checks = checks[before:]
            db.execute("UPDATE events SET state=?,summary=? WHERE run=? AND stage=?",
                       ("Issues" if any(not c.passed for c in risk_checks) else "Complete",
                        check_summary(risk_checks), run, STAGES[4]))
            decision, owner, message = decide(checks)
            snapshot = supplier_snapshot(db, workspace, form) if owner == "Procurement" and decision == "Pending" else None
            if decision == "Approved":
                store.add_supplier(db, workspace, form, "Active", row["ref"])
            evidence = [c.data() for c in checks]
            actions = vendor_actions(evidence) if owner == "Vendor" else []
            if decision == "Rejected":
                actions = ["Contact procurement with this reference to request a review."]
            subject, body = (draft if draft and actions == draft_actions else
                             template_email(row["ref"], actions)) if actions else (None, None)
            method = draft.method if draft and actions == draft_actions else "template" if subject else None
            db.execute("""UPDATE runs SET state='Complete',decision=?,owner=?,message=?,checks=?,email_subject=?,email_body=?,supplier_snapshot=?,email_method=? WHERE id=?""",
                       (decision, owner, message, json.dumps(evidence), subject, body, json.dumps(snapshot) if snapshot is not None else None, method, run))
            summary = f"{decision} · {owner}" + (" · email draft prepared" if subject else "")
            store.finish_stage(db, run, last, "Complete", summary)
        notify(last, "Complete", summary)
    except Exception:
        logging.exception("Workflow interrupted for %s", row["ref"])
        store.fail(run, [c.data() for c in checks])
        notify(active, "Failed", "Processing failed. Retry this application.")
    return row["ref"]
