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
