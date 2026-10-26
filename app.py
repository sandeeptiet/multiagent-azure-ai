"""Local walkthrough UI. Launch: streamlit run app.py"""
import json
import streamlit as st
from claims_triage.config import Settings
from claims_triage.tools import parse_claims
from claims_triage.workflow import TriageApplication

st.set_page_config(page_title="Claims triage", page_icon="📋", layout="wide")
st.title("Insurance claims triage")
st.caption("Four agents coordinate validation, grounded coverage checks and adjuster briefings. All data is synthetic.")

settings = Settings.load()
with st.sidebar:
    st.header("Demo controls")
    mode = st.selectbox("Execution mode", ["mock", "azure"], index=0 if settings.backend == "mock" else 1)
    st.caption("Mock mode simulates agent reasoning. Tools, retrieval, memory and review gates execute normally.")
    if st.button("New conversation"):
        st.session_state.pop("session_id", None)
        st.session_state.pop("batch_id", None)
    st.caption("A new conversation clears session context. Persistent claim history remains available.")

if st.session_state.get("mode") != mode:
    st.session_state.clear()
    st.session_state.mode = mode

@st.cache_resource
def application(backend, state_dir, endpoint, model):
    return TriageApplication(Settings(backend=backend, state_dir=state_dir,
                                     project_endpoint=endpoint, model_deployment=model, timeout=settings.timeout))

try:
    app = application(mode, settings.state_dir, settings.project_endpoint, settings.model_deployment)
except Exception as exc:
    st.error(str(exc))
    st.info("Complete .env and create the Azure agents from the terminal, or select mock mode.")
    st.stop()

if "session_id" not in st.session_state:
    st.session_state.session_id = app.store.session()
session_id = st.session_state.session_id
st.sidebar.caption(f"Conversation: {session_id}")
if st.sidebar.button("Load synthetic prior history"):
    app.store.seed(settings.data_dir / "history_seed.json")
    st.sidebar.success("Prior history is available across runs.")

st.info("Offline demo mode: no Azure or LLM calls." if mode == "mock" else "Azure mode: four Foundry agents run through the SDK. Tool execution and review gates remain local.")
tabs = st.tabs(["Triage a batch", "Ask about a claim", "Workflow trace", "Architecture"])
with tabs[0]:
    upload = st.file_uploader("Upload synthetic claims", type=["json", "csv"])
    left, right = st.columns([3, 1])
    with left:
        use_sample = st.checkbox("Use the supplied sample claims", value=True)
    with right:
        inject = st.checkbox("Simulate a tool failure")
    if st.button("Triage batch", type="primary"):
        try:
            if use_sample:
                claims = parse_claims((settings.data_dir / "claims.json").read_text())
            elif upload:
                claims = parse_claims(upload.getvalue().decode("utf-8-sig"), ".csv" if upload.name.lower().endswith(".csv") else ".json")
            else:
                raise ValueError("Choose sample data or upload a file.")
            with st.spinner("Running intake, coverage checks and briefings…"):
                batch = app.triage(claims, session_id, "check_coverage" if inject else None)
            st.session_state.batch_id = batch["batch_id"]
        except Exception as exc:
            st.error(str(exc))
    batch_id = st.session_state.get("batch_id") or app.store.context(session_id).get("last_batch_id")
    batch = app.store.batch(batch_id) if batch_id else None
    if batch:
        st.subheader(f"Batch status: {batch['status'].replace('_', ' ')}")
        if batch.get("error"):
            st.error(batch["error"])
        st.dataframe([{"Row": r["claim_key"], "Claim": str(r["claim"].get("claim_id", "Missing")),
                       "Next action": r["recommended_action"].replace("_", " "),
                       "Review": r["review_status"].replace("_", " ")} for r in batch["results"]], hide_index=True, width="stretch")
        st.download_button("Download full results", json.dumps(batch, indent=2), file_name="triage-results.json", mime="application/json")
        for r in batch["results"]:
            with st.expander(f"Row {r['claim_key']}: {r['claim'].get('claim_id', 'Missing ID')}"):
                st.write(r["briefing"])
                if r.get("error"):
                    st.error(r["error"])
                if r.get("briefing_agent_analysis"):
                    st.markdown("**Azure agent narrative (review alongside verified facts)**")
                    st.write(r["briefing_agent_analysis"])
                findings = r.get("coverage", {}).get("findings", [])
                if findings:
                    st.markdown("**Retrieved rule evidence**")
                    for f in findings:
                        st.write(f"{f['rule_id']}: {f['reason']}")
                        st.caption(f"{f['source']} — {f['evidence']}")
                if r.get("history", {}).get("records"):
                    st.markdown("**Earlier claim history**")
                    st.json(r["history"]["records"], expanded=False)
                if r["review_status"] == "pending_review":
                    with st.form(f"review_{batch_id}_{r['claim_key']}"):
                        st.markdown("**Human review checkpoint**")
                        reviewer = st.text_input("Reviewer", key=f"who_{r['claim_key']}")
                        allowed = ["request_more_documentation"] if not r["validation"]["valid"] else ["route_to_investigator", "request_more_documentation"]
                        if r["recommended_action"] == "auto_approve":
                            allowed.insert(0, "auto_approve")
                        decision = st.selectbox("Confirmed next action", allowed, format_func=lambda s: s.replace("_", " "), key=f"action_{r['claim_key']}")
                        notes = st.text_area("Review notes", key=f"notes_{r['claim_key']}")
                        if st.form_submit_button("Record review"):
                            try:
                                app.store.review(batch_id, r["claim_key"], reviewer, decision, notes)
                                st.rerun()
                            except ValueError as exc:
                                st.error(str(exc))
                elif r.get("review"):
                    st.success(f"Reviewed by {r['review']['reviewer']}: {r['review']['decision']}")
        st.caption("Review records a demonstration decision. This application does not pay, approve or reject a real insurance claim.")
with tabs[1]:
    st.caption('Try “Why was C-2031 flagged?”, then “What should I do next for that claim?”')
    for message in app.store.messages(session_id):
        with st.chat_message(message["role"]):
            st.write(message["text"])
    prompt = st.chat_input("Ask about a claim in this batch")
    if prompt:
        try:
            with st.spinner("Supervisor is checking the session context…"):
                app.ask(prompt, session_id)
            st.rerun()
        except Exception as exc:
            st.error(str(exc))
with tabs[2]:
    st.caption("The application enforces each stage and logs agent routing, tool calls, retrieval evidence, errors and human decisions.")
    st.json(app.store.events(session_id), expanded=False)
with tabs[3]:
    st.markdown('''**Azure AI Foundry** runs the supervisor, intake and validation, anomaly and coverage, and adjuster briefing agents.

**Local Python application** creates agents with the SDK, executes three custom tools, and enforces the ordered workflow and review gates.

**Conversation memory** preserves the current session and Foundry thread identifiers. **Persistent memory** stores earlier synthetic claims in SQLite across application restarts.

**Knowledge retrieval** uses a local sparse vector index over eight underwriting rules. Azure mode also attaches a Foundry File Search vector store to the anomaly agent.

The application validates facts and recommendations deterministically. Azure narratives support the adjuster’s review.''')
