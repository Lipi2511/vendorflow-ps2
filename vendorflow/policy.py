"""Evidence extraction and explicit internal onboarding rules."""
import re
import unicodedata
from dataclasses import asdict, dataclass

import pymupdf

from .ai import FIELD_MAP
from .examples import STATES, gst_check

LABELS = {
    "name": "Legal name", "trade": "Trade name", "email": "Business email",
    "pan": "PAN", "state": "Registered address state", "contact": "Contact person",
    "gst": "GSTIN", "holder": "Account holder", "account": "Account number", "ifsc": "IFSC",
}
PATTERNS = {
    "name": r"legal name(?: of (?:the )?business)?",
    "gst": r"GSTIN(?:/UIN)?|registration number",
    "pan": r"PAN|permanent account number",
    "state": r"(?:address[ /]+)?state|registered address state",
    "holder": r"(?:bank )?account holder(?:'s)?(?: name)?|beneficiary name|name of account holder",
    "account": r"(?:bank )?account (?:number|no\.?)",
    "ifsc": r"IFSC(?: code)?",
}
GST = re.compile(r"(?:0[1-9]|[12][0-9]|3[0-8]|97)[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]")
PAN_TYPES = dict(C="company", P="individual or proprietor", F="firm", H="HUF",
                 A="association of persons", T="trust", B="body of individuals",
                 L="local authority", J="artificial juridical person", G="government")
BANK_CODES = dict(SBIN="State Bank of India", HDFC="HDFC Bank", ICIC="ICICI Bank",
                  UTIB="Axis Bank", KKBK="Kotak Mahindra Bank", PUNB="Punjab National Bank",
                  BARB="Bank of Baroda", CNRB="Canara Bank", UBIN="Union Bank of India",
                  IDIB="Indian Bank", YESB="Yes Bank", INDB="IndusInd Bank", DEMO="Fictional demo bank")
WEBMAIL = {"gmail", "yahoo", "outlook", "hotmail", "rediffmail", "icloud", "proton", "protonmail"}
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
    if key == "state":
        names = {name.casefold(): code for code, name in STATES.items()}
        return names.get(value.casefold(), value.upper())
    return value.upper()


def valid_gst(value):
    return bool(GST.fullmatch(value) and gst_check(value[:-1]) == value[-1])


def compact(value):
    return "".join(ch for ch in unicodedata.normalize("NFKC", value).casefold() if ch.isalnum())


def redact_numbers(value):
    return re.sub(r"[0-9](?:[ -]*[0-9]){5,}", lambda match: display_value("account", match[0]), value)


def display_value(key, value):
    if key == "account":
        value = normalized(key, value)
        return "•••• " + value[-4:] if len(value) >= 4 else "incomplete"
    if key == "state":
        return STATES.get(normalized(key, value), value)[:110]
    return value[:110]


@dataclass
class Check:
    code: str
    passed: bool
    detail: str
    action: str = ""
    severity: str = "vendor"
    readings: dict | None = None

    def data(self):
        return asdict(self)


def read_document(data, kind, ai=None):
    label = "GST certificate" if kind == "tax" else "Bank letter"
    action = f"Upload a readable {label if kind == 'tax' else 'bank letter'} with the required fields."
    readings = {}
    def issue(detail, remedy=action, severity="vendor", fields=None):
        return fields or {}, Check(f"{kind}.document", False, detail, remedy, severity, readings)

    if not data:
        return issue(f"{label} is missing.")
    if len(data) > MAX_BYTES or b"%PDF-" not in data[:1024]:
        return issue(f"{label} must be a PDF under 5 MB.")
    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            if doc.needs_pass or not 1 <= len(doc) <= 10:
                return issue(f"{label} must be unlocked and contain 1–10 pages.")
            text = "\n".join(page.get_text("text", sort=True) for page in doc)
            images = []
            if not text.strip() and ai:
                for page in list(doc)[:3]:
                    scale = min(150 / 72, 2200 / max(page.rect.width, page.rect.height, 1))
                    images.append(page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("png"))
    except (RuntimeError, ValueError, pymupdf.FileDataError):
        return issue(f"{label} could not be read.")
    if not text.strip() and not ai:
        return issue(f"{label} has no readable text. Image-only documents require a text-based copy.",
                     f"Upload a text-based copy of the {label if kind == 'tax' else 'bank letter'}.")
    expected_type = "gst_certificate" if kind == "tax" else "bank_letter"
    required = ("name", "gst") if kind == "tax" else ("holder", "account", "ifsc")
    candidates = (*required, "pan", "state") if kind == "tax" else required
    fields = {}
    ambiguous = set()
    for key, pattern in PATTERNS.items():
        # Accept a labelled value on the same or immediately following line.
        matches = re.findall(rf"(?im)^[ \t]*(?:\d+\.[ \t]*)?(?:{pattern})[ \t]*(?:[:\-][ \t]*|\n[ \t]*|[ \t]+)(?:\n[ \t]*)?([^\r\n]+)$", text)
        unique = {normalized(key, m): m.strip() for m in matches if normalized(key, m)}
        if len(unique) == 1:
            fields[key] = next(iter(unique.values()))
        elif unique:
            ambiguous.add(key)
    def wrong_slot():
        if kind == "bank":
            return issue("The bank upload contains tax evidence instead of bank account details.",
                         "The file in the bank letter slot is a GST certificate. Upload a bank letter or cancelled cheque showing the account holder, account number and IFSC.")
        return issue("The GST upload contains banking evidence.",
                     "Upload the GST certificate in the tax document slot.")

    if ((kind == "bank" and "gst" in fields and not any(k in fields for k in required))
            or (kind == "tax" and all(k in fields for k in ("holder", "account", "ifsc")) and "gst" not in fields)):
        return wrong_slot()
    fields = {key: fields[key] for key in candidates if key in fields}
    for key, value in fields.items():
        readings[key] = dict(method="regex", confidence=1.0, evidence=display_value(key, value))
    if ai and any(key not in fields for key in required):
        result = ai.extract(text[:60000], images)
        doc_type = result.get("document_type")
        doc_type = doc_type if isinstance(doc_type, str) else None  # JSON-object fallback has no schema enforcement.
        if doc_type in {"gst_certificate", "bank_letter"} and doc_type != expected_type:
            # On text PDFs the classification must also be corroborated by recognizable content.
            marker = r"gst|registration certificate" if kind == "bank" else r"bank|ifsc|account holder"
            if not text.strip() or re.search(marker, text, re.I):
                return wrong_slot()
        proposed = result.get("fields")
        if doc_type == expected_type and isinstance(proposed, dict):
            for key in candidates:
                item = proposed.get(FIELD_MAP[key], {})
                if key in fields or key in ambiguous or not isinstance(item, dict):
                    continue
                value, confidence, evidence = item.get("value"), item.get("confidence"), item.get("evidence")
                if not isinstance(value, str) or not value.strip() or not isinstance(evidence, str):
                    continue
                if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
                    continue
                if text.strip():
                    # Normalize whitespace and punctuation without reordering the document.
                    needle = compact(value)
                    snippet = compact(evidence)
                    if not needle or needle not in snippet or snippet not in compact(text):
                        continue
                elif confidence < 0.85:
                    continue
                fields[key] = value.strip()
                # Preserve safe evidence only. Account numbers and surrounding text never enter the audit.
                readings[key] = dict(method="llm_text" if text.strip() else "llm_vision", confidence=confidence,
                                     evidence=display_value(key, value) if kind == "bank" else redact_numbers(evidence[:160]))
    missing = [LABELS[k] for k in required if k not in fields]
    if missing and ai:
        return issue(f"Automated reading uncertain for {', '.join(missing)}. Human review needed.",
                     "", "procurement", fields)
    if missing:
        return issue(f"{label} lacks unambiguous evidence for {', '.join(missing)}.")
    if kind == "bank" and any(readings.get(key, {}).get("method") == "llm_vision" for key in required):
        return fields, Check("bank.vision_confirmation", False,
                             "Bank details were read from a scanned image by AI. Confirm against the original before approval.",
                             severity="procurement", readings=readings)
    methods = {field["method"] for field in readings.values()}
    suffix = " using AI-assisted extraction" if methods - {"regex"} else ""
    return fields, Check(f"{kind}.document", True, f"{label} fields read{suffix}.", readings=readings)


def valid_email(value):
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value))


def field_checks(form):
    missing = [label for key, label in LABELS.items() if key != "trade" and not form.get(key, "").strip()]
    checks = [Check("form.required", not missing, "Required fields supplied." if not missing else
                    f"Missing fields: {', '.join(missing)}.", f"Complete {', '.join(missing)}.")]
    formats = [
        ("gst", valid_gst(form["gst"].strip().upper()), "GSTIN structure and checksum"),
        ("ifsc", re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", form["ifsc"].strip().upper()), "IFSC format"),
        ("account", re.fullmatch(r"[0-9]{6,20}", normalized("account", form["account"])), "Account number format (demo policy)"),
        ("email", valid_email(form["email"].strip()), "Business email format"),
    ]
    for key, ok, title in formats:
        if not form[key].strip():
            continue
        checks.append(Check(f"form.{key}", bool(ok), title + (" passed." if ok else " failed."),
                            f"Correct the {LABELS[key] if key in ('gst', 'ifsc', 'pan') else LABELS[key].lower()} on the form."))
    gst = normalized("gst", form["gst"])
    pan = normalized("pan", form.get("pan", ""))
    state = normalized("state", form.get("state", ""))
    name = normalized("name", form["name"])
    if pan:
        pan_ok = bool(re.fullmatch(r"[A-Z]{5}[0-9]{4}[A-Z]", pan))
        checks.append(Check("form.pan", pan_ok, "PAN has the expected format." if pan_ok else "PAN format is invalid.",
                            "Enter a valid PAN."))
    if state and valid_gst(gst):
        checks.append(Check("form.state", state == gst[:2],
                            f"GSTIN state code {gst[:2]} · selected {display_value('state', state)}.",
                            "Select the registered state that matches the GSTIN."))
    if valid_gst(gst):
        embedded = gst[2:12]
        if re.fullmatch(r"[A-Z]{5}[0-9]{4}[A-Z]", pan):
            checks.append(Check("form.pan_match", pan == embedded, "Entered PAN matches the PAN in the GSTIN." if pan == embedded else
                                f"Entered PAN {pan} does not match the PAN in the GSTIN ({embedded}).",
                                "Enter the PAN matching the GSTIN."))
        entity = embedded[3]
        if name.endswith("limited"):
            type_label = PAN_TYPES.get(entity, "different entity type")
            article = "an" if type_label[0].lower() in "aeiou" else "a"
            checks.append(Check("form.pan_type", entity == "C",
                                "GSTIN indicates a company." if entity == "C" else
                                f"GSTIN indicates {article} {type_label}, but the legal name indicates a company.",
                                "Provide the GSTIN registered to this company."))
        if entity != "P" and name:
            checks.append(Check("form.pan_initial", embedded[4] == name[0].upper(),
                                f"PAN name initial {embedded[4]} · legal name initial {name[0].upper()}.",
                                severity="procurement"))
    ifsc = normalized("ifsc", form["ifsc"])
    if re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", ifsc):
        bank_code = ifsc[:4]
        checks.append(Check("form.bank_code", bank_code in BANK_CODES,
                            BANK_CODES.get(bank_code, f"IFSC bank code {bank_code} is outside the demo allowlist."),
                            "Provide an IFSC supported by this onboarding demo."))
    account = normalized("account", form["account"])
    if re.fullmatch(r"[0-9]{6,20}", account):
        pattern = len(set(account)) == 1 or account in "0123456789" * 2 or account in "9876543210" * 2
        checks.append(Check("form.account_sanity", not pattern,
                            "Account number resembles a placeholder." if pattern else "Account number sanity check passed.",
                            "Enter the bank account number shown on the bank proof."))
    email = form["email"].strip().lower()
    if valid_email(email):
        first_label = email.rsplit("@", 1)[1].split(".")[0]
        words = re.findall(r"[^\W_]+", re.sub(r"^(?:(?:m\s*/\s*s|the)\b[\s.:-]*)+", "", form["name"].lower()))
        if first_label in WEBMAIL:
            checks.append(Check("email.webmail", False, "Non-corporate email domain.", severity="info"))
        elif words and first_label == words[0]:
            checks.append(Check("email.domain_match", True, "Email domain aligns with the first word of the legal name.", severity="info"))
    return checks


def compare(key, left, right, source_a, source_b, action):
    ok = bool(normalized(key, left)) and normalized(key, left) == normalized(key, right)
    evidence = f"{source_a}: {display_value(key, left)} · {source_b}: {display_value(key, right)}"
    return Check(f"compare.{key}.{source_a}", ok, evidence, action)


def compare_documents(form, tax, bank):
    checks = []
    if tax:
        for key in ("name", "gst", "pan", "state"):
            if key not in tax or not form.get(key, "").strip():
                continue
            gst = normalized("gst", form["gst"])
            if key == "pan" and valid_gst(gst) and normalized("pan", form["pan"]) != gst[2:12]:
                continue
            checks.append(compare(key, form[key], tax[key], "Application", "GST certificate",
                                  f"Reconcile the {LABELS[key] if key in ('gst', 'ifsc', 'pan') else LABELS[key].lower()} with the GST certificate."))
    if bank:
        for key in ("holder", "account", "ifsc"):
            if key not in bank or not form[key].strip():
                continue
            checks.append(compare(key, form[key], bank[key], "Application", "Bank letter",
                                  f"Correct the {LABELS[key] if key in ('gst', 'ifsc', 'pan') else LABELS[key].lower()} or provide matching bank proof."))
        if form["name"].strip() and "holder" in bank:
            check = compare("name", form["name"], bank["holder"], "Legal entity", "Bank letter holder",
                            "Provide bank proof in the applicant’s legal name or contact procurement for review.")
            gst = normalized("gst", form["gst"])
            if not check.passed and valid_gst(gst) and gst[5] == "P":
                check.severity = "procurement"
                check.action = ""
                check.detail += " · Proprietor-held account. Confirm the proprietor’s identity."
            checks.append(check)
    return checks
