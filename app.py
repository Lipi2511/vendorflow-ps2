from datetime import datetime, timezone, timedelta
import json
import re
from uuid import uuid4

import streamlit as st

from vendorflow.examples import EXAMPLES, STATES, sample_files, sample_form, sample_pack
from vendorflow.policy import LABELS
from vendorflow.store import CaseConflict, Store
from vendorflow.workflow import execute

st.set_page_config(page_title="VendorFlow | Procurement", page_icon="📁", layout="wide")
st.markdown("""<style>
.stMainBlockContainer{max-width:1180px;padding-top:3rem;padding-bottom:4rem}
[data-testid="stSidebar"]{border-right:1px solid #d7e2de}
[data-testid="stMetric"]{border-top:3px solid #548576;padding:1rem 0 .65rem}
h1,h2,h3{letter-spacing:-.035em}
[data-testid="stCaptionContainer"]{color:#4b625b}
</style>""", unsafe_allow_html=True)
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
            stages = {}
            def notify(stage, state):
                if stage not in stages:
                    stages[stage] = live.empty()
                stages[stage].write(f"{stage} · {state}")
            try:
                result = execute(store, workspace, eid, form, tax, bank, ref=ref,
                                 previous=previous, notify=notify)
            except CaseConflict as exc:
                st.error(str(exc))
                return
            live.update(label="Run recorded", state="complete")
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
    with st.container(border=True):
        st.subheader(status)
        st.caption(f"Next action owner · {row['owner'] or 'Processing'}")
        st.markdown("**Vendor response**")
        st.text(row["message"] or "Processing is in progress. Refresh this case for an update.")
        if row["id"] == current["id"] and (status == "Failed" or row["owner"] == "Vendor"):
            if st.button("Correct and resubmit" if status!="Failed" else "Retry application", type="primary"):
                navigate("Correction", ref)
    checks = json.loads(row["checks"])
    st.subheader("Decision evidence")
    for check in sorted(checks, key=lambda c: c["passed"]):
        st.text(("✓  " if check["passed"] else "Review  ·  ") + check["detail"])
    with st.expander("Execution history", expanded=True):
        for event in store.events(row["id"]):
            st.text(f"{event['stage']}  ·  {event['state']}")
            finish = event["finished"]
            duration = (datetime.fromisoformat(finish) - datetime.fromisoformat(event["started"])).total_seconds() if finish else None
            detail = f"Started {timestamp(event['started'])}"
            if duration is not None:
                detail += f" · Finished {timestamp(finish)} · {duration * 1000:.0f} ms"
            st.caption(detail)
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
