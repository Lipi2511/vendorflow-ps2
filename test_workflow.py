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
    events = store.events(row["id"])
    assert tuple(e["stage"] for e in events) == workflow.STAGES
    assert all(e["finished"] and e["summary"] and e["state"] in ("Complete", "Issues") for e in events)


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

def test_bank_review_changes_only_workspace_register(store):
    hold = submit(store, "Changed bank account")
    store.review("local", hold["ref"], 1, "approve", "Demo Reviewer", "Verified via known supplier contact.", verified=True, vendor_instruction="Provide corrected evidence.")
    current = store.history("local", hold["ref"])[0]
    assert (current["decision"], current["automated_decision"], current["automated_owner"]) == ("Approved", "Pending", "Procurement")
    assert current["review"]["reviewer"] == "Demo Reviewer"
    assert store.events(hold["id"])[-1]["stage"] == "Procurement review"
    with closing(store.connect()) as db:
        assert db.execute("SELECT ending FROM supplier_history").fetchone()[0] == "1824"
        assert db.execute("SELECT ending FROM suppliers WHERE workspace='local'").fetchone()[0] == "8479"
        assert db.execute("SELECT ending FROM suppliers WHERE workspace='seed' AND reference='SUP-017'").fetchone()[0] == "1824"
    assert "register.duplicate" in submit(store, "Changed bank account")["checks"]
    with pytest.raises(CaseConflict):
        store.review("local", hold["ref"], 1, "reject", "Demo Reviewer", "Second click", verified=True, vendor_instruction="Provide corrected evidence.")
    with pytest.raises(CaseConflict):
        store.review("other", hold["ref"], 1, "approve", "Demo Reviewer", "Other workspace", verified=True, vendor_instruction="Provide corrected evidence.")


@pytest.mark.parametrize("action,decision,owner", [
    ("approve", "Approved", "Complete"), ("reject", "Rejected", "Procurement"),
    ("return", "Pending", "Vendor"),
])
def test_human_resolution_of_new_supplier(store, action, decision, owner):
    from vendorflow.examples import gst_check
    base = sample_form("New supplier")
    prefix = "27AABCZ1234F1Z"
    form = base | {"gst":prefix+gst_check(prefix), "pan":prefix[2:12]}
    tax = pdf("GST Registration Certificate", [("Legal Name", form["name"]), ("GSTIN", form["gst"])])
    bank = sample_files("New supplier")[1]
    hold = submit(store, form=form, docs=(tax, bank))
    assert hold["owner"] == "Procurement" and hold["email_body"] is None
    with pytest.raises(CaseConflict):
        store.review("local", hold["ref"], 1, action, "Demo Reviewer", "", verified=True, vendor_instruction="Provide corrected evidence.")
    store.review("local", hold["ref"], 1, action, "Demo Reviewer", "Verified registry evidence independently.", verified=True, vendor_instruction="Provide corrected evidence.")
    current = store.history("local", hold["ref"])[0]
    assert (current["decision"], current["owner"]) == (decision, owner)
    with closing(store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM suppliers WHERE workspace='local'").fetchone()[0] == (action == "approve")

def test_duplicate_correction_and_repeat_escalation(store):
    first = submit(store, "Wrong document")
    ref = first["ref"]
    same = sample_form("Wrong document") | {"name":"Kite Paper Pvt. Ltd.", "account":"710000-000257"}
    with pytest.raises(CaseConflict, match="Nothing changed since attempt 1"):
        submit(store, "Wrong document", form=same, ref=ref, previous=1)
    assert len(store.history("local", ref)) == 1
    for previous in range(1, 4):
        different = sample_form("Wrong document") | {"contact":f"Demo Contact {previous}"}
        result = submit(store, "Wrong document", form=different, ref=ref, previous=previous)
        assert result["owner"] == ("Vendor" if previous < 3 else "Procurement")
    assert "Repeated unsuccessful corrections" in result["checks"]
    assert result["email_body"] is None
    with pytest.raises(CaseConflict):
        submit(store, "Wrong document", ref=ref, previous=4)


def test_fourth_attempt_can_resolve_the_case(store):
    first = submit(store, "Wrong document")
    ref = first["ref"]
    for previous in (1, 2):
        submit(store, "Wrong document", form=sample_form("Wrong document") | {"contact":str(previous)},
               ref=ref, previous=previous)
    tax, _, correct = sample_files("Wrong document")
    result = submit(store, "Wrong document", docs=(tax, correct), ref=ref, previous=3)
    assert result["decision"] == "Approved"

def test_dashboard_counts_latest_cases_and_filters(store):
    from vendorflow.reporting import case_flags, dashboard_metrics, filter_cases, pending_reasons
    approved = submit(store)
    changed = submit(store, "Changed bank account")
    pending = submit(store, "Wrong document")
    rows = store.latest("local")
    metrics = dashboard_metrics(rows)
    assert [metrics[k] for k in ("Applications", "Approved", "Waiting on vendor", "Waiting on procurement")] == [3, 1, 1, 1]
    assert metrics["Median decision time"] != "—"
    assert filter_cases(rows, "Pending", "Procurement", "juniper")[0]["ref"] == changed["ref"]
    assert filter_cases(rows, "All statuses", "All owners", approved["ref"].lower())[0]["ref"] == approved["ref"]
    assert "Bank account changed" in case_flags(changed)
    assert pending_reasons(rows)["Bank document needs review"] == 1
    tax, _, correct = sample_files("Wrong document")
    submit(store, "Wrong document", docs=(tax, correct), ref=pending["ref"], previous=1)
    assert dashboard_metrics(store.latest("local"))["Applications"] == 3
    assert "Bank document needs review" not in pending_reasons(store.latest("local"))

def test_case_presentation_and_preview(store):
    from vendorflow.reporting import attempt_label, evidence_groups, preview_pdf
    row = submit(store, "Changed bank account")
    assert attempt_label(row) == "Attempt 1 · Pending (Procurement)"
    groups = evidence_groups(json.loads(row["checks"]))
    assert any(check["code"] == "register.bank_change" for check in groups["Fraud & register"])
    assert groups["Documents"] and groups["Cross-checks"] and groups["Soft flags"]
    tax, bank = sample_files("New supplier")[:2]
    assert preview_pdf(tax).startswith(b"\x89PNG")
    assert preview_pdf(bank, bank=True).startswith(b"\x89PNG")
    assert preview_pdf(bank, bank=True) != preview_pdf(bank)
    assert preview_pdf(b"bad input") is None

@pytest.mark.parametrize("example,decision,owner,rule", [
    ("Shared bank account", "Pending", "Procurement", "register.shared_account"),
    ("PAN mismatch", "Pending", "Vendor", "form.pan_type"),
    ("Scanned bank letter", "Pending", "Vendor", "bank.document"),
    ("Abbreviated name", "Approved", "Complete", "compare.name.Legal entity"),
])
def test_additional_samples_without_api_key(store, example, decision, owner, rule):
    row = submit(store, example)
    assert (row["decision"], row["owner"]) == (decision, owner)
    assert any(c["code"] == rule and c["passed"] == (decision == "Approved") for c in json.loads(row["checks"]))


@pytest.mark.parametrize("confidence,decision,owner", [(.94, "Approved", "Complete"), (.84, "Pending", "Procurement")])
def test_scanned_sample_with_mocked_vision(store, confidence, decision, owner):
    from types import SimpleNamespace
    import pymupdf
    from vendorflow.ai import Assistance
    from vendorflow.reporting import preview_pdf
    form = sample_form("Scanned bank letter")
    tax, bank, _ = sample_files("Scanned bank letter")
    with pymupdf.open(stream=bank, filetype="pdf") as document:
        assert not document[0].get_text().strip()
    assert preview_pdf(bank, bank=True) is None

    class Fake:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)
        def create(self, **kwargs):
            content = kwargs["messages"][-1]["content"]
            assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
            fields = {name: dict(value=form[key], confidence=confidence, evidence=form[key])
                      for name, key in (("account_holder","holder"), ("account_number","account"), ("ifsc","ifsc"))}
            result = dict(document_type="bank_letter", fields=fields)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(result)))])

    row = submit(store, "Scanned bank letter", docs=(tax, bank), ai=Assistance(client=Fake()))
    assert (row["decision"], row["owner"]) == (decision, owner)
    assert form["account"] not in row["checks"]
    if decision == "Approved":
        assert "llm_vision" in row["checks"]
        assert "AI (0.94)" in store.events(row["id"])[1]["summary"]
    else:
        assert "Human review needed" in row["checks"]


def test_kit_has_every_input_set():
    from io import BytesIO
    from zipfile import ZipFile
    from vendorflow.examples import sample_pack
    with ZipFile(BytesIO(sample_pack())) as archive:
        assert archive.testzip() is None
        assert {name.split("/")[0] for name in archive.namelist()} == {e.lower().replace(" ", "-") for e in EXAMPLES}
        for name in EXAMPLES:
            folder = name.lower().replace(" ", "-")
            assert json.loads(archive.read(f"{folder}/form.json")) == sample_form(name)
            assert archive.read(f"{folder}/bank.pdf") == sample_files(name)[1]

def test_streamlit_flow_and_optional_key(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("VENDORFLOW_DB", str(tmp_path / "streamlit.sqlite3"))
    app = AppTest.from_file("app.py", default_timeout=20).run()
    assert not app.exception
    assert any(c.value == "AI extraction off" for c in app.caption)
    app.toggle[0].set_value(False).run()

    def click(label):
        next(widget for widget in app.button if widget.label == label).click().run(timeout=20)
        assert not app.exception

    def application(example):
        click("New application")
        next(widget for widget in app.selectbox if widget.label == "Input set").set_value(example).run()
        click("Run onboarding checks")

    application("Wrong document")
    assert any(h.value == "Pending" for h in app.subheader)
    click("Correct and resubmit")
    app.checkbox[0].set_value(True).run()
    click("Run onboarding checks")
    assert any(h.value == "Approved" for h in app.subheader)
    application("Shared bank account")
    application("Changed bank account")
    assert any(h.value == "Pending" for h in app.subheader)
    next(widget for widget in app.text_input if widget.label == "Reviewer name").set_value("Demo Reviewer")
    app.text_area[0].set_value("Verified via the established contact.")
    app.checkbox[0].set_value(True).run()
    click("Verify & approve")
    assert any(h.value == "Approved" for h in app.subheader)
    click("Return to dashboard")
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["Applications"] == "3" and metrics["Waiting on procurement"] == "1"
    assert len(app.get("vega_lite_chart")) == 1

    def fake_openai(**kwargs):
        assert kwargs["api_key"] == "fictional-test-key"
        return object()

    monkeypatch.setattr("openai.OpenAI", fake_openai)
    monkeypatch.setenv("OPENAI_API_KEY", "fictional-test-key")
    app_with_key = AppTest.from_file("app.py", default_timeout=20).run()
    assert not app_with_key.exception
    assert any(c.value == "AI extraction available" for c in app_with_key.caption)

def test_reviewer_send_back_uses_public_instruction_only(store):
    hold = submit(store, "Changed bank account")
    request = "Supply a signed authorization from your finance contact."
    store.review("local", hold["ref"], 1, "return", "Reviewer", "Internal register SUP-017.",
                 vendor_instruction=request, verified=False)
    row = store.history("local", hold["ref"])[0]
    assert store.outstanding(row) == [request]
    assert request in row["email_body"] and "SUP-017" not in row["email_body"]


def test_migrate_database_before_upgrade(tmp_path):
    import sqlite3
    file = tmp_path / "legacy.sqlite3"
    with closing(sqlite3.connect(file)) as db, db:
        db.executescript("""
            CREATE TABLE runs (
              id INTEGER PRIMARY KEY, workspace TEXT NOT NULL, ref TEXT NOT NULL, attempt INTEGER NOT NULL,
              token TEXT NOT NULL, name TEXT NOT NULL, started TEXT NOT NULL, state TEXT NOT NULL,
              decision TEXT, owner TEXT, message TEXT, checks TEXT NOT NULL DEFAULT '[]',
              UNIQUE(workspace,ref,attempt), UNIQUE(workspace,token)
            );
            CREATE TABLE events (
              run INTEGER NOT NULL REFERENCES runs(id), stage TEXT NOT NULL, started TEXT NOT NULL,
              finished TEXT, state TEXT NOT NULL, PRIMARY KEY(run,stage)
            );
            INSERT INTO runs VALUES(1,'local','VF-OLD',1,'old','Demo Legacy Supplier',
              '2026-11-23T10:01:00.000+00:00','Complete','Pending','Vendor','Old request','[]');
            INSERT INTO events VALUES(1,'Receive application','2026-11-23T10:01:00.000+00:00',
                '2026-11-23T10:01:00.000+00:00','Complete');
        """)
    migrated = Store(file)
    assert migrated.history("local", "VF-OLD")[0]["message"] == "Old request"
    assert migrated.events(1)[0]["summary"] is None
    assert submit(migrated, ref="VF-OLD", previous=1)["decision"] == "Approved"

def test_review_requires_explicit_verification(store):
    hold = submit(store, "Changed bank account")
    with pytest.raises(CaseConflict, match="Confirm independent"):
        store.review("local", hold["ref"], 1, "approve", "Reviewer", "Checked", verified=False)
    assert store.history("local", hold["ref"])[0]["decision"] == "Pending"
    store.review("local", hold["ref"], 1, "reject", "Reviewer", "Declined", verified=False)
