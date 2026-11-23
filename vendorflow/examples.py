"""Fictional documents and editable inputs for the evaluator."""
from io import BytesIO
import json
from zipfile import ZipFile

import pymupdf


EXAMPLES = {
    "New supplier": ("Aster Looms Private Limited", "27AABCA1234F1Z", "710000000146"),
    "Wrong document": ("Kite Paper Private Limited", "27AABCK2345F1Z", "710000000257"),
    "Different account holder": ("Fern Studio Private Limited", "27AABCF3456F1Z", "710000000368"),
    "Changed bank account": ("Juniper Tools Private Limited", "27AABCJ4567F1Z", "710000008479"),
    "Blocked supplier": ("Vale Metals Private Limited", "27AABCV5678F1Z", "710000000580"),
}


def gst_check(base):
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    total = 0
    for i, character in enumerate(base):
        value = alphabet.index(character) * (1 + i % 2)
        total += value // 36 + value % 36
    return alphabet[-total % 36]


STATES = dict(zip(
    "01 02 03 04 05 06 07 08 09 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 26 27 29 30 31 32 33 34 35 36 37 38".split(),
    ("Jammu and Kashmir", "Himachal Pradesh", "Punjab", "Chandigarh", "Uttarakhand", "Haryana",
     "Delhi", "Rajasthan", "Uttar Pradesh", "Bihar", "Sikkim", "Arunachal Pradesh", "Nagaland",
     "Manipur", "Mizoram", "Tripura", "Meghalaya", "Assam", "West Bengal", "Jharkhand", "Odisha",
     "Chhattisgarh", "Madhya Pradesh", "Gujarat", "Dadra and Nagar Haveli and Daman and Diu",
     "Maharashtra", "Karnataka", "Goa", "Lakshadweep", "Kerala", "Tamil Nadu", "Puducherry",
     "Andaman and Nicobar Islands", "Telangana", "Andhra Pradesh", "Ladakh"), strict=True))


def sample_form(example):
    name, base, account = EXAMPLES[example]
    return dict(name=name, trade="", pan=base[2:12], state=base[:2], contact="Demo Procurement Contact", email=f"ops@{name.split()[0].lower()}.example",
                gst=base + gst_check(base), holder=name, account=account, ifsc="DEMO0000123")


def pdf(title, rows):
    with pymupdf.open() as doc:
        page = doc.new_page(width=595, height=740)
        page.insert_text((46, 52), "VENDORFLOW  /  FICTIONAL DEMO DOCUMENT", fontsize=10)
        page.insert_text((46, 102), title, fontsize=22)
        for i, (label, value) in enumerate(rows):
            page.insert_text((46, 162 + i * 34), f"{label}: {value}", fontsize=12)
        page.insert_text((46, 670), "Synthetic evidence for a procurement case study.", fontsize=10)
        return doc.tobytes(deflate=True, no_new_id=True)


def sample_files(example):
    form = sample_form(example)
    tax = pdf("GST Registration Certificate", [
        ("Legal Name of Business", form["name"]), ("GSTIN", form["gst"]),
        ("PAN", form["pan"]), ("State", STATES[form["state"]])])
    def bank_letter(holder):
        return pdf("Bank Account Confirmation Letter", [
            ("Account Holder", holder), ("Account Number", form["account"]), ("IFSC", form["ifsc"])])
    correct = bank_letter(form["name"])
    bank = correct
    if example == "Wrong document":
        bank = tax
    elif example == "Different account holder":
        bank = bank_letter("Fern Retail Private Limited")
    return tax, bank, correct


def sample_pack():
    out = BytesIO()
    with ZipFile(out, "w") as archive:
        for name in EXAMPLES:
            path = name.lower().replace(" ", "-")
            tax, bank, correct = sample_files(name)
            archive.writestr(f"{path}/form.json", json.dumps(sample_form(name), indent=2))
            archive.writestr(f"{path}/gst.pdf", tax)
            archive.writestr(f"{path}/bank.pdf", bank)
            if name in ("Wrong document", "Different account holder"):
                archive.writestr(f"{path}/corrected-bank.pdf", correct)
    return out.getvalue()
