import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
import pytest
from claims_triage.agents import AGENTS
from claims_triage.config import Settings, ROOT
from claims_triage.knowledge import KnowledgeBase
from claims_triage.storage import Store
from claims_triage.tools import ToolRegistry, parse_claims
from claims_triage.workflow import TriageApplication, STEPS

@pytest.fixture
def settings(tmp_path):
    return Settings(state_dir=tmp_path / "state")

@pytest.fixture
def app(settings):
    return TriageApplication(settings)

@pytest.fixture
def clean():
    return {"claim_id":"C-T100","policy_number":"POL-7710","claim_type":"collision","loss_date":"2026-07-20","report_date":"2026-07-21","claim_amount":2200,"description":"Synthetic collision"}


def test_four_agents_three_registered_tools():
    assert set(AGENTS) == {"supervisor", "intake", "anomaly", "briefing"}
    assert sum(len(a.tools) for a in AGENTS.values()) == 3
    assert AGENTS["supervisor"].tools == []


def test_sample_outcomes_and_rule_citations(app):
    app.store.seed(app.settings.data_dir / "history_seed.json")
    batch = app.triage(json.loads((app.settings.data_dir / "claims.json").read_text()))
    assert batch["status"] == "pending_review"
    results = {r["claim"]["claim_id"]: r for r in batch["results"]}
    assert results["C-2031"]["recommended_action"] == "route_to_investigator"
    assert "C-H100" in results["C-2031"]["briefing"]
    assert results["C-2032"]["recommended_action"] == "auto_approve"
    assert {f["rule_id"] for f in results["C-2033"]["coverage"]["findings"]} == {"R04", "R05"}
    assert results["C-2034"]["coverage"]["skipped"]
    assert results["C-2035"]["recommended_action"] == "auto_approve"
    for result in batch["results"]:
        assert result["review_status"] == "pending_review"
    stages = [e["stage"] for e in app.store.events(batch["session_id"])]
    assert all(step in stages for step in STEPS)


@pytest.mark.parametrize("updates", [
    {"claim_amount":0}, {"claim_amount":-1}, {"claim_amount":True}, {"claim_amount":"NaN"},
    {"claim_amount":"Infinity"}, {"claim_amount":"1.234"}, {"claim_amount":{}},
    {"loss_date":"2026-02-30"}, {"loss_date":"20260720"}, {"loss_date":"2026-07-22"},
    {"policy_number":"POL-999999"}, {"policy_number":[]}, {"description":" "}, {"claim_id":[]},
])
def test_invalid_claims_never_reach_coverage(app, clean, updates):
    result = app.triage([{**clean, **updates}])["results"][0]
    assert not result["validation"]["valid"]
    assert result["recommended_action"] == "request_more_documentation"
    assert result["coverage"]["skipped"]


def test_duplicate_ids_preserve_both_rows(app, clean):
    batch = app.triage([clean, clean.copy()])
    assert len(batch["results"]) == 2
    assert {r["claim_key"] for r in batch["results"]} == {"1", "2"}
    assert all(not r["validation"]["valid"] for r in batch["results"])
    assert app.store.history("POL-7710", "2027-01-01", "other") == []
    assert "more than once" in app.ask("Why was C-T100 flagged?", batch["session_id"])


@pytest.mark.parametrize("text", ["{broken", "{}", "[]", '[1]', '[{"claim_amount": NaN}]'])
def test_malformed_inputs(text):
    with pytest.raises(ValueError):
        parse_claims(text)


def test_csv_parsing(app):
    claims = parse_claims("claim_id,policy_number,claim_type,loss_date,report_date,claim_amount,description\nC-CSV1,POL-7710,collision,2026-07-20,2026-07-21,2300,Example\n", ".csv")
    assert app.triage(claims)["results"][0]["validation"]["valid"]


def test_coverage_boundaries_and_exclusions(app, clean):
    cases = [
        ({"loss_date":"2025-01-01","report_date":"2025-01-10"}, True),
        ({"loss_date":"2027-12-31","report_date":"2028-01-01"}, True),
        ({"loss_date":"2024-12-31","report_date":"2025-01-01"}, False),
        ({"loss_date":"2028-01-01","report_date":"2028-01-02"}, False),
    ]
    for i, (update, expected) in enumerate(cases):
        result = app.triage([{**clean, "claim_id": f"C-BOUND{i}", **update}])["results"][0]
        assert result["coverage"]["checks"]["loss_in_active_period"] is expected
    result = app.triage([{**clean, "claim_type":"fire", "claim_amount":30001}])["results"][0]
    assert not result["coverage"]["checks"]["claim_type_covered"]
    assert not result["coverage"]["checks"]["within_policy_limit"]


@pytest.mark.parametrize("amount,flag", [(29399.99,False),(29400,True),(30000,True),(30000.01,False)])
def test_near_limit_threshold(app, clean, amount, flag):
    r = app.triage([{**clean,"claim_amount":amount}])["results"][0]
    assert ("R05" in {f["rule_id"] for f in r["coverage"]["findings"]}) is flag


def test_conversation_memory_and_session_isolation(app, clean):
    batch = app.triage([clean])
    first = app.ask("Explain claim C-T100", batch["session_id"])
    assert "C-T100" in first
    assert "C-T100" in app.ask("What next for that claim?", batch["session_id"])
    other = app.store.session()
    assert "Upload or triage" in app.ask("What next for that claim?", other)
    assert app.store.context(other).get("last_claim_id") is None


def test_persistence_in_separate_process(settings, app, clean):
    app.triage([clean])
    code = "from pathlib import Path; from claims_triage.storage import Store; import json; s=Store(Path(__import__('sys').argv[1])); print(json.dumps(s.history('POL-7710','2027-01-01','C-NEW')))"
    process = subprocess.run([sys.executable,"-c",code,str(settings.state_dir)],cwd=ROOT,capture_output=True,text=True)
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)[0]["claim_id"] == clean["claim_id"]


def test_history_excludes_self_future_and_other_policy(app, clean):
    app.triage([clean])
    assert app.store.history("POL-7710", "2026-07-20", "C-OTHER") == []
    assert app.store.history("POL-7710", "2027-01-01", "C-T100") == []
    assert app.store.history("POL-5521", "2027-01-01", "C-OTHER") == []
    assert len(app.store.history("POL-7710", "2027-01-01", "C-OTHER")) == 1
    app.triage([clean])
    assert len(app.store.history("POL-7710", "2027-01-01", "C-OTHER")) == 1


def test_retrieval_reloads_rule_changes(settings, tmp_path, clean):
    custom = tmp_path / "kb"
    custom.mkdir()
    text = (settings.knowledge_dir / "underwriting_rules.md").read_text()
    (custom / "underwriting_rules.md").write_text(text.replace('"fraction":0.02', '"fraction":0.1'))
    app = TriageApplication(replace(settings, knowledge_dir=custom))
    r = app.triage([{**clean,"claim_amount":27500}])["results"][0]
    assert "R05" in {f["rule_id"] for f in r["coverage"]["findings"]}
    assert r["coverage"]["retrieved_rules"]
    assert app.knowledge.digest != KnowledgeBase(settings.knowledge_dir, settings.state_dir).digest


def test_missing_required_rule_fails_closed(settings, tmp_path, clean):
    kb = tmp_path / "badkb"
    kb.mkdir()
    text = (settings.knowledge_dir / "underwriting_rules.md").read_text()
    text = text[:text.index("## R03")] + text[text.index("## R04"):]
    (kb / "underwriting_rules.md").write_text(text)
    app = TriageApplication(replace(settings, knowledge_dir=kb))
    result = app.triage([clean])["results"][0]
    assert result["review_status"] == "blocked"


def test_tool_failure_isolated_and_logged(app, clean):
    batch = app.triage([clean, {**clean,"claim_id":"C-T101"}], fail_tool="check_coverage")
    assert batch["status"] == "partial_failure"
    assert batch["results"][0]["review_status"] == "blocked"
    assert batch["results"][1]["recommended_action"] == "auto_approve"
    assert any(e["stage"] == "error" for e in app.store.events(batch["session_id"]))


def test_intake_failure_returns_failed_batch(app, clean):
    batch = app.triage([clean], fail_tool="validate_claims")
    assert batch["status"] == "failed"
    assert batch["results"] == []
    assert app.store.batch(batch["batch_id"])["error"]


def test_human_review_required_and_persisted(app, clean):
    batch = app.triage([clean])
    assert batch["results"][0]["review_status"] == "pending_review"
    with pytest.raises(ValueError):
        app.store.review(batch["batch_id"], "1", "", "auto_approve", "checked")
    reviewed = app.store.review(batch["batch_id"], "1", "Reviewer", "auto_approve", "All controls checked")
    assert reviewed["review_status"] == "reviewed"
    assert app.store.batch(batch["batch_id"])["status"] == "reviewed"
    with pytest.raises(ValueError, match="already"):
        app.store.review(batch["batch_id"], "1", "Reviewer", "auto_approve", "again")


def test_flagged_or_invalid_claim_cannot_be_approved(app, clean):
    for update in ({"claim_amount":30000}, {"loss_date":""}):
        batch = app.triage([{**clean, **update}])
        with pytest.raises(ValueError, match="gate"):
            app.store.review(batch["batch_id"], "1", "Reviewer", "auto_approve", "override")


def test_tool_schema_and_role_scope(app, clean):
    registry = ToolRegistry("batch", [clean], app.policies, app.knowledge, app.store, app.store.session())
    with pytest.raises(ValueError):
        registry.execute("supervisor", "validate_claims", {"batch_id":"batch"})
    with pytest.raises(ValueError):
        registry.execute("intake", "validate_claims", {"batch_id":"another"})
    from jsonschema import ValidationError
    with pytest.raises(ValidationError):
        registry.execute("intake", "validate_claims", {"batch_id":"batch", "arbitrary":"bad"})


def test_ui_loads_without_errors():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20).run()
    assert not at.exception
    assert at.title[0].value == "Insurance claims triage"


def test_earlier_claim_in_same_unsorted_batch_is_a_visible_observation(app, clean):
    later = {**clean, "claim_id":"C-LATE1", "loss_date":"2026-08-01", "report_date":"2026-08-02"}
    batch = app.triage([later, clean])
    result = batch["results"][0]
    assert result["recommended_action"] == "route_to_investigator"
    assert result["history"]["records"][0]["review_status"] == "unreviewed_observation"
    assert batch["results"][1]["history"]["records"] == []


def test_review_keeps_partial_failure_and_updates_brief(app, clean):
    batch = app.triage([clean, {**clean, "claim_id":"C-T102"}], fail_tool="check_coverage")
    app.store.review(batch["batch_id"], "2", "Reviewer", "auto_approve", "Verified")
    saved = app.store.batch(batch["batch_id"])
    assert saved["status"] == "partial_failure"
    assert "Awaiting human review" not in saved["results"][1]["briefing"]
