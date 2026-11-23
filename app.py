from datetime import datetime, timezone, timedelta
import html
import json
import os
import re
from time import perf_counter, sleep
from uuid import uuid4
from urllib.parse import quote, urlencode

import streamlit as st

from vendorflow.ai import Assistance
from vendorflow.examples import EXAMPLES, STATES, sample_files, sample_form, sample_pack
from vendorflow.policy import LABELS
from vendorflow.store import CaseConflict, Store
from vendorflow.workflow import STAGES, execute

st.set_page_config(page_title="VendorFlow | Procurement", page_icon="📁", layout="wide")
st.markdown("""<style>
.stMainBlockContainer{max-width:1180px;padding-top:3rem;padding-bottom:4rem}
[data-testid="stSidebar"]{border-right:1px solid #d7e2de}
[data-testid="stMetric"]{border-top:3px solid #548576;padding:1rem 0 .65rem}
h1,h2,h3{letter-spacing:-.035em}
[data-testid="stCaptionContainer"]{color:#4b625b}
</style>""", unsafe_allow_html=True)
def api_key():
    try:
        return str(st.secrets.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")).strip()
    except FileNotFoundError:
        return os.environ.get("OPENAI_API_KEY", "").strip()


@st.cache_resource(show_spinner=False)
def assistance(key):
    return Assistance(key)


key_from_settings = api_key()
ai = assistance(key_from_settings) if key_from_settings else None
ss = st.session_state
if not re.fullmatch(r"[a-f0-9]{32}", st.query_params.get("w", "")):
    st.query_params["w"] = uuid4().hex
workspace = st.query_params["w"]
store = Store()
ss.setdefault("drafts", {})
ss.setdefault("editor", uuid4().hex)
ss.setdefault("screen", "Case" if st.query_params.get("case") else "Dashboard")


def navigate(screen, ref=None):
    ss.screen = screen
    if ref:
        st.query_params["case"] = ref
    elif "case" in st.query_params:
        del st.query_params["case"]
    if screen in ("New application", "Correction"):
        ss.editor = uuid4().hex
    st.rerun()


@st.cache_data(show_spinner=False)
def kit():
    return sample_pack()


def timestamp(value):
    return datetime.fromisoformat(value).astimezone(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b %Y · %H:%M:%S IST")


with st.sidebar:
    st.header("VendorFlow")
    st.caption("PROCUREMENT WORKSPACE")
    st.caption("AI extraction available" if ai else "AI extraction off")
    demo_pacing = st.toggle("Demo pacing", value=True)
    if st.button("Dashboard", icon=":material/dashboard:", width="stretch"):
        navigate("Dashboard")
    if st.button("New application", icon=":material/add:", width="stretch", type="primary"):
        navigate("New application")
    st.divider()
    st.download_button("Download sample kit", kit(), "vendorflow-examples.zip",
                       mime="application/zip", icon=":material/download:", width="stretch")
    with st.expander("Demo policy"):
        st.write("For GST-registered Indian companies. Use fictional information. Approval records a document-consistent supplier under this demo policy. Payment authorization requires separate bank verification.")
        st.write("The sample register includes one active supplier and one blocked supplier. New approvals join this workspace’s register. A fresh base URL opens an independent workspace.")
        st.caption("Bookmark this workspace URL to revisit its cases. Anyone with the workspace link can view its demo history. Local database retention depends on the host.")


def dashboard():
    st.title("Onboarding dashboard")
    st.caption("Supplier applications, decisions and next actions.")
    rows = store.latest(workspace)
    for col, label in zip(st.columns(4), ("Applications", "Approved", "Pending", "Rejected")):
        col.metric(label, len(rows) if label == "Applications" else sum(r["decision"] == label for r in rows))
    st.subheader("Application history")
    if not rows:
        with st.container(border=True):
            st.markdown("#### Your first supplier application")
            st.write("Use a sample company or upload a GST certificate and bank letter to start a review.")
            if st.button("Start application", type="primary"):
                navigate("New application")
        return
    f = st.selectbox("Filter by status", ["All statuses", "Approved", "Pending", "Rejected", "Failed", "Running"])
    filtered = [r for r in rows if f == "All statuses" or f in (r["decision"], r["state"])]
    if not filtered:
        st.info("No applications match this status.")
    for row in filtered:
        with st.container(border=True):
            a, b, c = st.columns([4, 2, 1.3], vertical_alignment="center")
            a.text(row["name"])
            a.caption(f"{row['ref']} · {timestamp(row['started'])}")
            b.write(row["decision"] or row["state"])
            b.caption(f"Owner · {row['owner'] or 'Processing'}")
            if c.button("Open case", key=f"open{row['id']}"):
                navigate("Case", row["ref"])


def intake(ref=None):
    previous = 0
    old = {}
    if ref:
        history = store.history(workspace, ref)
        if not history:
            st.error("Case unavailable in this workspace.")
            return
        previous = history[0]["attempt"]
        old = ss.drafts.get(ref, {})
    st.title("Correct application" if ref else "New supplier application")
    st.caption(f"{ref} · Submit the next attempt with updated evidence." if ref else
               "Submit company, tax and banking evidence for onboarding.")
    eid = ss.editor
    options = ["Custom submission", *EXAMPLES]
    example = st.selectbox("Input set", options, index=options.index(old.get("example", "New supplier" if not ref else "Custom submission")),
                           key=eid+"preset", disabled=bool(ref and old))
    use_example = example in EXAMPLES
    form = sample_form(example) if use_example else dict.fromkeys(LABELS, "")
    tax = bank = None
    if use_example:
        tax, bank, _ = sample_files(example)
    if ref and old:
        form, tax, bank = old["form"], old["tax"], old["bank"]
    key = eid + example
    form = dict(form)
    with st.container():
        st.subheader("Company details")
        a, b = st.columns(2)
        for col, field in [(a,"name"),(b,"trade"),(a,"gst"),(b,"pan"),(a,"contact"),(b,"email")]:
            form[field] = col.text_input(LABELS[field] + (" (optional)" if field=="trade" else ""), form.get(field, ""), max_chars=150, key=key+field)
        states = ["", *STATES]
        old_state = form.get("state", "")
        form["state"] = a.selectbox(LABELS["state"], states, index=states.index(old_state) if old_state in states else 0,
                                    format_func=lambda code: STATES.get(code, "Select a state"), key=key+"state")
        st.subheader("Banking details")
        a, b, c = st.columns([2, 1.5, 1.2])
        for col, field in [(a,"holder"),(b,"account"),(c,"ifsc")]:
            form[field] = col.text_input(LABELS[field], form[field], max_chars=120, key=key+field)
        st.subheader("Supporting documents")
        st.caption("Text-based PDFs · Up to 5 MB and 10 pages each. Uploaded files replace any attached examples.")
        a, b = st.columns(2)
        tax_up = a.file_uploader("GST certificate", type=["pdf"], key=key+"tax")
        bank_up = b.file_uploader("Bank letter", type=["pdf"], key=key+"bank")
        if tax_up is not None:
            a.caption(f"GST upload received · {tax_up.size:,} bytes")
        elif tax:
            a.caption("A GST-slot PDF is attached.")
        if bank_up is not None:
            b.caption(f"Bank upload received · {bank_up.size:,} bytes")
        elif bank:
            b.caption("A bank-slot PDF is attached.")
        repair = bool(ref and use_example and example in ("Wrong document", "Different account holder")
                      and st.checkbox("Attach the corrected sample bank letter"))
        submitted = st.button("Run onboarding checks", type="primary", key=key+"run")
    if submitted:
        if repair:
            bank = sample_files(example)[2]
        tax = tax_up.getvalue() if tax_up is not None else tax
        bank = bank_up.getvalue() if bank_up is not None else bank
        if not form["name"].strip():
            st.error("Enter the legal name before running the checks.")
        if not (tax or bank):
            st.error("Attach at least one supporting document before running the checks.")
        if not form["name"].strip() or not (tax or bank):
            return
        with st.status("Processing application", expanded=True) as live:
            stages = {stage: live.empty() for stage in STAGES}
            starts = {}
            for stage, slot in stages.items():
                slot.text(f"○  {stage}")

            def notify(stage, state, summary):
                if state == "Running":
                    starts[stage] = perf_counter()
                elif demo_pacing:
                    sleep(max(0., .7 - (perf_counter() - starts[stage])) if stage in starts else 0.)
                icon = {"Running": "⏳", "Complete": "✓", "Issues": "⚠", "Failed": "✕"}[state]
                stages[stage].text(f"{icon}  {stage}" + (f" · {summary}" if summary else " · Running"))
                if demo_pacing and stage == STAGES[0]:
                    sleep(.7)
            try:
                result = execute(store, workspace, eid, form, tax, bank, ref=ref,
                                 previous=previous, notify=notify, ai=ai)
            except CaseConflict as exc:
                st.error(str(exc))
                return
            failed = store.history(workspace, result)[0]["state"] == "Failed"
            live.update(label="Run failed" if failed else "Run recorded", state="error" if failed else "complete")
            if demo_pacing:
                sleep(1.5)
        ss.drafts[result] = dict(example=example, form=form, tax=tax, bank=bank)
        navigate("Case", result)


def case_detail(ref):
    history = store.history(workspace, ref)
    if not history:
        st.error("Case unavailable in this workspace.")
        return
    current = history[0]
    st.caption(f"CASE RECORD   /   {ref}")
    st.title(current["name"])
    options = [r["attempt"] for r in history]
    attempt = st.selectbox("Attempt history", options, format_func=lambda n: f"Attempt {n}" + (" · latest" if n==options[0] else ""))
    row = next(r for r in history if r["attempt"] == attempt)
    status = row["decision"] or row["state"]
    st.caption(timestamp(row["started"]))
    with st.expander("Execution history", expanded=True):
        for event in store.events(row["id"]):
            icon = {"Running": "⏳", "Complete": "✓", "Issues": "⚠", "Failed": "✕"}.get(event["state"], "○")
            st.text(f"{icon}  {event['stage']} · {event['state']}" + (f" · {event['summary']}" if event["summary"] else ""))
            finish = event["finished"]
            duration = (datetime.fromisoformat(finish) - datetime.fromisoformat(event["started"])).total_seconds() if finish else None
            detail = f"Started {timestamp(event['started'])}"
            if duration is not None:
                detail += f" · Finished {timestamp(finish)} · {duration * 1000:.0f} ms"
            st.caption(detail)
    with st.container(border=True):
        st.subheader(status)
        st.caption(f"Next action owner · {row['owner'] or 'Processing'}")
        st.markdown("**Vendor response**")
        st.text(row["message"] or "Processing is in progress. Refresh this case for an update.")
        if row["id"] == current["id"] and (status == "Failed" or row["owner"] == "Vendor"):
            if st.button("Correct and resubmit" if status!="Failed" else "Retry application", type="primary"):
                navigate("Correction", ref)
    if row["review"]:
        review = row["review"]
        with st.expander("Procurement review record", expanded=True):
            st.caption(f"Automated result · {row['automated_decision']} ({row['automated_owner']})")
            st.text(f"{review['reviewer']} · {timestamp(review['timestamp'])}")
            st.text(review["note"])
    elif row["id"] == current["id"] and (status, row["owner"]) == ("Pending", "Procurement"):
        with st.expander("Reviewer panel", expanded=True):
            st.caption("Verify the evidence independently and record the method or reference. This demo workspace uses a shared reviewer view.")
            with st.form(f"review-{row['id']}"):
                reviewer = st.text_input("Reviewer name", max_chars=120)
                note = st.text_area("Internal verification note", max_chars=2000)
                approve_col, reject_col, return_col = st.columns(3)
                approve = approve_col.form_submit_button("Verify & approve", type="primary")
                reject = reject_col.form_submit_button("Reject")
                send_back = return_col.form_submit_button("Send back to vendor")
            if approve or reject or send_back:
                action = "approve" if approve else "reject" if reject else "return"
                try:
                    store.review(workspace, ref, row["attempt"], action, reviewer, note)
                except CaseConflict as exc:
                    st.error(str(exc))
                else:
                    st.rerun()
    if row["email_body"]:
        with st.expander("Email to vendor", expanded=True):
            st.text(row["email_subject"])
            st.code(row["email_body"], language=None)
            recipient = ss.drafts.get(ref, {}).get("form", {}).get("email", "")
            email_query = urlencode(dict(subject=row["email_subject"], body=row["email_body"]), quote_via=quote)
            link = f"mailto:{quote(recipient, safe='@.')}?{email_query}"
            st.markdown(f'<a href="{html.escape(link, quote=True)}">Open in your email app</a>', unsafe_allow_html=True)
    elif row["owner"] == "Procurement" and status == "Pending":
        st.info("Internal note · Procurement will verify the evidence before the vendor is asked to act.")
    checks = json.loads(row["checks"])
    st.subheader("Decision evidence")
    for check in sorted(checks, key=lambda c: c["passed"]):
        st.text(("✓  " if check["passed"] else "Review  ·  ") + check["detail"])
        for field, reading in (check.get("readings") or {}).items():
            badge = "Read by rules" if reading["method"] == "regex" else f"Read by AI · {reading['confidence']:.2f}"
            st.caption(f"{LABELS[field]} · {badge} · {reading['evidence']}")
    if st.button("Return to dashboard"):
        navigate("Dashboard")


if ss.screen == "Dashboard":
    dashboard()
elif ss.screen == "New application":
    intake()
elif ss.screen == "Correction":
    intake(st.query_params.get("case"))
else:
    case_detail(st.query_params.get("case", ""))
