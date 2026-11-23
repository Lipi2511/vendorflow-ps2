"""Evidence extraction and explicit internal onboarding rules."""
import re
import unicodedata
from dataclasses import asdict, dataclass

import pymupdf

from .examples import gst_check

LABELS = {
    "name": "Legal name", "trade": "Trade name", "email": "Business email",
    "gst": "GSTIN", "holder": "Account holder", "account": "Account number", "ifsc": "IFSC",
}
PATTERNS = {
    "name": r"legal name(?: of (?:the )?business)?",
    "gst": r"GSTIN(?:/UIN)?|registration number",
    "holder": r"(?:bank )?account holder(?:'s)?(?: name)?|beneficiary name|name of account holder",
    "account": r"(?:bank )?account (?:number|no\.?)",
    "ifsc": r"IFSC(?: code)?",
}
GST = re.compile(r"(?:0[1-9]|[12][0-9]|3[0-8]|97)[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]")
MAX_BYTES = 5 * 1024 * 1024


def normalized(key, value):
    value = unicodedata.normalize("NFKC", value).strip()
    if key in ("name", "holder"):
        value = re.sub(r"^(?:(?:m\s*/\s*s|the)\b[\s.:-]*)+", "", value.casefold())
        aliases = {"pvt": "private", "ltd": "limited", "co": "company"}
        words = re.findall(r"[^\W_]+", value.replace("&", " and "))
        return "".join(aliases.get(word, word) for word in words)
    if key == "account":
        return re.sub(r"[\s-]+", "", value)
    return value.upper()


def valid_gst(value):
    return bool(GST.fullmatch(value) and gst_check(value[:-1]) == value[-1])


def display_value(key, value):
    if key == "account":
        value = normalized(key, value)
        return "•••• " + value[-4:] if len(value) >= 4 else "incomplete"
    return value[:110]


@dataclass
class Check:
    code: str
    passed: bool
    detail: str
    action: str = ""

    def data(self):
        return asdict(self)


def read_document(data, kind):
    label = "GST certificate" if kind == "tax" else "Bank letter"
    action = f"Upload a readable {label if kind == 'tax' else 'bank letter'} with the required fields."
    def issue(detail):
        return {}, Check(f"{kind}.document", False, detail, action)

    if not data:
        return issue(f"{label} is missing.")
    if len(data) > MAX_BYTES or b"%PDF-" not in data[:1024]:
        return issue(f"{label} must be a PDF under 5 MB.")
    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            if doc.needs_pass or not 1 <= len(doc) <= 10:
                return issue(f"{label} must be unlocked and contain 1–10 pages.")
            text = "\n".join(page.get_text("text", sort=True) for page in doc)
    except (RuntimeError, ValueError, pymupdf.FileDataError):
        return issue(f"{label} could not be read.")
    if not text.strip():
        return issue(f"{label} has no readable text. Image-only documents require a text-based copy.")
    fields = {}
    for key, pattern in PATTERNS.items():
        # Accept a labelled value on the same or immediately following line.
        matches = re.findall(rf"(?im)^[ \t]*(?:\d+\.[ \t]*)?(?:{pattern})[ \t]*(?:[:\-][ \t]*|\n[ \t]*|[ \t]+)(?:\n[ \t]*)?([^\r\n]+)$", text)
        unique = {normalized(key, m): m.strip() for m in matches if normalized(key, m)}
        if len(unique) == 1:
            fields[key] = next(iter(unique.values()))
    required = ("name", "gst") if kind == "tax" else ("holder", "account", "ifsc")
    if kind == "bank" and "gst" in fields and not any(k in fields for k in required):
        return issue("The bank upload contains tax evidence instead of bank account details.")
    missing = [LABELS[k] for k in required if k not in fields]
    if missing:
        return issue(f"{label} lacks unambiguous evidence for {', '.join(missing)}.")
    return fields, Check(f"{kind}.document", True, f"{label} contains the required labelled fields.")


def field_checks(form):
    missing = [LABELS[k] for k in LABELS if k != "trade" and not form[k].strip()]
    checks = [Check("form.required", not missing, "Required fields supplied." if not missing else
                    f"Missing fields: {', '.join(missing)}.", f"Complete {', '.join(missing)}.")]
    formats = [
        ("gst", valid_gst(form["gst"].strip().upper()), "GSTIN structure and checksum"),
        ("ifsc", re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", form["ifsc"].strip().upper()), "IFSC format"),
        ("account", re.fullmatch(r"[0-9]{6,20}", normalized("account", form["account"])), "Account number format (demo policy)"),
        ("email", re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", form["email"].strip()), "Business email format"),
    ]
    for key, ok, title in formats:
        if not form[key].strip():
            continue
        checks.append(Check(f"form.{key}", bool(ok), title + (" passed." if ok else " failed."),
                            f"Correct the {LABELS[key] if key in ('gst', 'ifsc') else LABELS[key].lower()} on the form."))
    return checks


def compare(key, left, right, source_a, source_b, action):
    ok = bool(normalized(key, left)) and normalized(key, left) == normalized(key, right)
    evidence = f"{source_a}: {display_value(key, left)} · {source_b}: {display_value(key, right)}"
    return Check(f"compare.{key}.{source_a}", ok, evidence, action)


def compare_documents(form, tax, bank):
    checks = []
    if tax:
        for key in ("name", "gst"):
            if not form[key].strip():
                continue
            checks.append(compare(key, form[key], tax[key], "Application", "GST certificate",
                                  f"Reconcile the {LABELS[key] if key in ('gst', 'ifsc') else LABELS[key].lower()} with the GST certificate."))
    if bank:
        for key in ("holder", "account", "ifsc"):
            if not form[key].strip():
                continue
            checks.append(compare(key, form[key], bank[key], "Application", "Bank letter",
                                  f"Correct the {LABELS[key] if key in ('gst', 'ifsc') else LABELS[key].lower()} or provide matching bank proof."))
        if form["name"].strip():
            checks.append(compare("name", form["name"], bank["holder"], "Legal entity", "Bank letter holder",
                                  "Provide bank proof in the applicant’s legal name or contact procurement for review."))
    return checks
