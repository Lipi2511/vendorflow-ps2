from collections import Counter
from datetime import datetime
import json
from statistics import median

from .policy import LABELS

REASONS = {
    "tax.document": "GST document needs review", "bank.document": "Bank document needs review",
    "register.bank_change": "Bank account changed", "register.shared_account": "Shared bank account",
    "register.duplicate": "Existing supplier record", "register.block": "Internal register hold",
    "form.pan_initial": "PAN name initial mismatch", "form.pan_type": "PAN entity type mismatch",
    "form.pan_match": "PAN does not match GSTIN", "form.pan": "PAN format",
    "form.state": "GSTIN state mismatch", "form.required": "Missing required fields",
    "form.bank_code": "Unknown IFSC bank code", "form.account_sanity": "Account looks like a placeholder",
    "case.repeated_corrections": "Repeated unsuccessful corrections",
}
RISK_CODES = {"register.bank_change", "register.shared_account", "form.pan_initial", "form.pan_type", "form.pan_match"}


def dashboard_metrics(rows):
    durations = [(datetime.fromisoformat(r["decided_at"]) - datetime.fromisoformat(r["started"])).total_seconds()
                 for r in rows if r["decided_at"]]
    return {
        "Applications": len(rows),
        "Approved": sum(r["decision"] == "Approved" for r in rows),
        "Waiting on vendor": sum((r["decision"], r["owner"]) == ("Pending", "Vendor") for r in rows),
        "Waiting on procurement": sum((r["decision"], r["owner"]) == ("Pending", "Procurement") for r in rows),
        "Rejected": sum(r["decision"] == "Rejected" for r in rows),
        "Median decision time": f"{median(durations):.2f} s" if durations else "—",
    }


def filter_cases(rows, status, owner, query):
    search = query.strip().casefold()
    return [r for r in rows
            if (status == "All statuses" or status == (r["decision"] or r["state"]))
            and (owner == "All owners" or owner == (r["owner"] or "Processing"))
            and (search in r["name"].casefold() or search in r["ref"].casefold())]


def reason_for(check):
    code = check["code"]
    if code.endswith(".document") and check.get("severity") == "procurement":
        return "Uncertain AI document reading"
    if code in REASONS:
        return REASONS[code]
    parts = code.split(".")
    label = LABELS.get(parts[1]) if len(parts) > 1 else None
    return f"{label} {'mismatch' if parts[0] == 'compare' else 'validation'}" if label else "Other review reason"


def pending_reasons(rows):
    counts = Counter()
    for row in rows:
        if row["decision"] == "Pending":
            failures = {check["code"]: reason_for(check) for check in json.loads(row["checks"])
                        if not check["passed"] and check.get("severity") != "info"}
            counts.update(failures.values())
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def case_flags(row):
    return list(dict.fromkeys(reason_for(check) for check in json.loads(row["checks"])
                              if not check["passed"] and (check["code"] in RISK_CODES or
                              (check["code"].endswith(".document") and check.get("severity") == "procurement"))))

def attempt_label(row):
    status = row["decision"] or row["state"]
    suffix = f" ({row['owner']})" if status == "Pending" and row["owner"] else ""
    return f"Attempt {row['attempt']} · {status}{suffix}"


def evidence_groups(checks):
    categories = dict.fromkeys(("Documents", "Form", "Cross-checks", "Fraud & register", "Soft flags"))
    groups = {key: [] for key in categories}
    for check in checks:
        code = check["code"]
        if check.get("severity") == "info":
            group = "Soft flags"
        elif code.endswith(".document"):
            group = "Documents"
        elif code.startswith("compare."):
            group = "Cross-checks"
        elif code.startswith("register.") or code.startswith("case.") or code == "form.pan_initial":
            group = "Fraud & register"
        else:
            group = "Form"
        groups[group].append(check)
    return groups


def preview_pdf(data, *, bank=False):
    import re

    import pymupdf

    try:
        with pymupdf.open(stream=data, filetype="pdf") as document:
            if document.needs_pass or not len(document):
                return None
            page = document[0]
            if bank:
                text = page.get_text()
                # Keep an image-only document's account digits out of the rendered preview.
                if not text.strip():
                    return None
                for block in page.get_text("blocks"):
                    if block[6] != 0 or any(len(re.sub(r"[^0-9]", "", line)) >= 6
                                             for line in block[4].splitlines()):
                        page.add_redact_annot(pymupdf.Rect(block[:4]), fill=(.92, .94, .93), cross_out=False)
                page.apply_redactions()
            scale = min(1.5, 1200 / max(1, page.rect.width, page.rect.height))
            return page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), annots=False).tobytes("png")
    except (RuntimeError, ValueError, pymupdf.FileDataError):
        return None
