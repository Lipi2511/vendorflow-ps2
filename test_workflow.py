import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from uuid import uuid4

import pytest
from vendorflow import workflow
from vendorflow.examples import EXAMPLES, pdf, sample_files, sample_form
from vendorflow.policy import read_document
from vendorflow.store import CaseConflict, Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "history.sqlite")


def submit(store, example="New supplier", *, workspace="local", form=None, docs=None, token=None, **kwargs):
    fields = form or sample_form(example)
    tax, bank = docs if docs is not None else sample_files(example)[:2]
    ref = workflow.execute(store, workspace, token or uuid4().hex, fields, tax, bank, **kwargs)
    return store.history(workspace, ref)[0]


@pytest.mark.parametrize("example,decision,owner,rule", [
    ("New supplier", "Approved", "Complete", "register.new"),
    ("Wrong document", "Pending", "Vendor", "bank.document"),
    ("Different account holder", "Pending", "Vendor", "compare.name.Legal entity"),
    ("Changed bank account", "Pending", "Procurement", "register.bank_change"),
    ("Blocked supplier", "Rejected", "Procurement", "register.block"),
])
def test_cases(store, example, decision, owner, rule):
    row = submit(store, example)
    assert (row["decision"], row["owner"]) == (decision, owner)
    assert row["message"] and row["state"] == "Complete"
    checks = json.loads(row["checks"])
    assert any(c["code"] == rule and c["passed"] == (decision == "Approved") for c in checks)
    assert all(e["finished"] and e["state"] == "Complete" for e in store.events(row["id"]))


def test_correction_and_idempotency(store):
    first = submit(store, "Wrong document", token="initial")
    assert submit(store, "Wrong document", token="initial")["id"] == first["id"]
    tax, _, correct = sample_files("Wrong document")
    fixed = submit(store, "Wrong document", docs=(tax, correct), ref=first["ref"], previous=1)
    reopened = Store(store.path)
    assert [r["decision"] for r in reopened.history("local", first["ref"])] == ["Approved", "Pending"]
    assert len(reopened.latest("local")) == 1 and fixed["attempt"] == 2
    with pytest.raises(CaseConflict):
        submit(store, "Wrong document", ref=first["ref"], previous=1)


def test_cannot_replace_bank_hold_or_seed(store):
    hold = submit(store, "Changed bank account")
    with pytest.raises(CaseConflict):
        submit(store, ref=hold["ref"], previous=1)
    again = submit(store, "Changed bank account")
    assert "register.bank_change" in again["checks"]
    original = sample_form("Changed bank account") | {"account":"710000001824"}
    assert "register.duplicate" in submit(store, "Changed bank account", form=original)["checks"]


@pytest.mark.parametrize("data", [None, b"this is not a PDF", b"%PDF-invalid", pdf("Scanned image", [])])
def test_unusable_proof(store, data):
    row = submit(store, docs=(sample_files("New supplier")[0], data))
    assert row["decision"] == "Pending" and "bank" in row["message"].lower()


def test_confirmation_required_for_block(store):
    row = submit(store, "Blocked supplier", docs=(None, sample_files("Blocked supplier")[1]))
    assert (row["decision"], row["owner"]) == ("Pending", "Procurement")


def test_masked_data_and_workspace_isolation(store):
    form = sample_form("New supplier")
    row = submit(store)
    assert store.history("elsewhere", row["ref"]) == []
    assert submit(store, workspace="elsewhere")["decision"] == "Approved"
    with closing(store.connect()) as db:
        dump = "\n".join(db.iterdump())
    assert form["gst"] in row["checks"]
    for field in ["account", "email"]:
        assert form[field] not in dump


def test_concurrent_approvals(store):
    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(lambda n: submit(store)["decision"], range(2)))
    assert sorted(decisions) == ["Approved", "Pending"]


def test_error_and_retry(store, monkeypatch):
    def fail(*args):
        raise RuntimeError("test processing interruption")
    with monkeypatch.context() as m:
        m.setattr(workflow, "read_document", fail)
        row = submit(store)
    assert row["state"] == "Failed" and row["decision"] is None
    assert store.events(row["id"])[-1]["state"] == "Failed"
    assert submit(store, ref=row["ref"], previous=1)["decision"] == "Approved"


def test_missing_fields_and_conflicting_evidence(store):
    form = sample_form("New supplier") | {"gst":"invalid", "email":""}
    row = submit(store, form=form)
    assert row["decision"] == "Pending"
    assert "GSTIN" in row["message"] and "email" in row["message"].lower()
    assert "form.email" not in row["checks"]
    data = pdf("Bank letter", [("Account Holder","A"),("Account Holder","B"),("Account Number","1234567"),("IFSC","DEMO0000123")])
    assert not read_document(data, "bank")[1].passed


def test_legal_and_trade_names(store):
    form = sample_form("New supplier")
    assert submit(store, form=form | {"name":form["name"].upper().replace(" ", "."), "trade":"Aster shop"})["decision"] == "Approved"

def test_intake_minimum_and_account_name_normalisation(store):
    fields = sample_form("New supplier")
    with pytest.raises(CaseConflict):
        submit(store, form=fields | {"name": ""})
    with pytest.raises(CaseConflict):
        submit(store, docs=(None, None))
    assert not store.latest("local")
    assert submit(store, form=fields | {"name": "M/s The Aster Looms Pvt. Ltd.",
                                        "account": "7100-0000 0146"})["decision"] == "Approved"

def test_shared_account_across_new_suppliers(store):
    assert submit(store)["decision"] == "Approved"
    form = sample_form("Wrong document") | {"account":sample_form("New supplier")["account"]}
    row = submit(store, "Wrong document", form=form)
    assert row["owner"] == "Procurement"
    assert "register.shared_account" in row["checks"]


@pytest.mark.parametrize("field,value,code", [
    ("pan", "AABCA1235F", "form.pan_match"), ("state", "29", "form.state"),
    ("ifsc", "ZZZZ0000123", "form.bank_code"), ("account", "123456789", "form.account_sanity"),
    ("account", "111111111", "form.account_sanity"),
])
def test_validation_rules(store, field, value, code):
    row = submit(store, form=sample_form("New supplier") | {field:value})
    assert row["owner"] == "Vendor"
    assert any(c["code"] == code and not c["passed"] for c in json.loads(row["checks"]))


def test_entity_initial_type_and_webmail():
    from vendorflow.examples import gst_check
    from vendorflow.policy import compare_documents, field_checks
    base = sample_form("New supplier")
    assert any(c.code == "email.domain_match" and c.passed for c in field_checks(base))
    checks = field_checks(base | {"name":"Saffron Farms Private Limited", "email":"demo@gmail.com"})
    assert any(c.code == "form.pan_initial" and not c.passed and c.severity == "procurement" for c in checks)
    assert any(c.code == "email.webmail" and c.severity == "info" for c in checks)
    prefix = "27AABPA1234F1Z"
    proprietor = base | {"gst":prefix+gst_check(prefix), "pan":prefix[2:12]}
    assert any(c.code == "form.pan_type" and not c.passed and c.severity == "vendor" for c in field_checks(proprietor))
    checks = compare_documents(proprietor, {}, dict(holder="Another Person", account=base["account"], ifsc=base["ifsc"]))
    assert checks[-1].severity == "procurement"

def test_ai_guard_and_draft(store):
    from types import SimpleNamespace
    from vendorflow.ai import Assistance

    class Fake:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)
        def create(self, **kwargs):
            schema = kwargs["response_format"]["json_schema"]["name"]
            if schema == "vendor_document":
                result = dict(document_type="bank_letter", fields={
                    "account_holder": dict(value="Imaginary Name", confidence=.99, evidence="letter"),
                    "account_number": dict(value="710000000146", confidence=.99, evidence="710000000146"),
                    "ifsc": dict(value="DEMO0000123", confidence=.98, evidence="DEMO0000123"),
                })
            else:
                result = dict(subject="Unsafe subject", body="Unrelated requirement")
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(result)))])

    ai = Assistance(client=Fake())
    data = pdf("Bank letter", [("Routing", "DEMO0000123"), ("Destination", "710000000146")])
    fields, check = read_document(data, "bank", ai)
    assert check.severity == "procurement" and "holder" not in fields
    assert check.readings["account"]["evidence"] == "•••• 0146"
    row = submit(store, "Wrong document", ai=ai)
    assert "cancelled cheque" in row["email_body"] and row["ref"] in row["email_body"]
