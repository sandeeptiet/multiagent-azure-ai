"""Inspectable orchestration with persisted validation and human review gates."""
import json
import logging
import uuid
from .config import Settings
from .knowledge import KnowledgeBase
from .storage import Store
from .tools import ToolRegistry, parse_claims
from .backends.mock import MockBackend

logger = logging.getLogger("claims_triage")
STEPS = ("supervisor_route", "intake", "validation_gate", "coverage_anomalies", "briefing", "human_review")


def recommendation(validation, coverage):
    if not validation["valid"]:
        return "request_more_documentation"
    actions = {f["action"] for f in coverage["findings"]}
    if "route_to_investigator" in actions:
        return "route_to_investigator"
    if "request_more_documentation" in actions:
        return "request_more_documentation"
    return "auto_approve"


def briefing(claim, validation, coverage, history, action):
    label = claim.get("claim_id", "Unidentified claim")
    lines = [f"{label}: {claim.get('claim_type', 'unknown type')} claim for {claim.get('claim_amount', 'unknown amount')} under {claim.get('policy_number', 'unknown policy')}."]
    if not validation["valid"]:
        lines.append("Intake requires correction: " + "; ".join(validation["errors"]) + ". Coverage has not been evaluated.")
    else:
        findings = coverage["findings"]
        lines.append(" ".join(f"[{f['rule_id']}] {f['reason']}." for f in findings) if findings else "All coverage checks passed and no configured risk indicators were triggered.")
    if history["records"]:
        lines.append("Earlier claim history: " + "; ".join(
            f"{h['claim_id']} ({h['claim_type']}, loss {h['loss_date']}, {h.get('review_status', 'unreviewed')}, source {h.get('source', 'persistent_store')}, prior recommendation {h.get('recommended_action', 'not recorded')})"
            for h in history["records"]) + ".")
    else:
        lines.append("No eligible earlier history was found, or lookup was skipped due to invalid intake.")
    lines.append(f"Recommended next action: {action}. Awaiting human review. No claim payment or final coverage decision has been executed.")
    return "\n\n".join(lines)


class TriageApplication:
    def __init__(self, settings=None, backend=None):
        self.settings = settings or Settings.load()
        self.store = Store(self.settings.state_dir)
        self.knowledge = KnowledgeBase(self.settings.knowledge_dir, self.settings.state_dir)
        policies = json.loads((self.settings.data_dir / "policy_coverage.json").read_text())
        self.policies = {p["policy_number"]: p for p in policies}
        if len(self.policies) != len(policies):
            raise ValueError("Policy numbers must be unique")
        if backend is not None:
            self.backend = backend
        elif self.settings.backend == "mock":
            self.backend = MockBackend()
        elif self.settings.backend == "azure":
            from .backends.azure import AzureBackend
            self.backend = AzureBackend(self.settings, self.store)
        else:
            raise ValueError("Backend must be mock or azure")

    def event(self, session_id, stage, **detail):
        value = self.store.event(session_id, stage=stage, **detail)
        logger.info(json.dumps(value))

    def triage(self, claims, session_id=None, fail_tool=None):
        # Revalidate programmatic input too, not just file uploads.
        claims = parse_claims(json.dumps(claims))
        session_id = self.store.session(session_id)
        batch_id = str(uuid.uuid4())
        batch = {"batch_id": batch_id, "session_id": session_id, "backend": self.backend.name,
                 "status": "running", "results": [], "steps": list(STEPS)}
        registry = ToolRegistry(batch_id, claims, self.policies, self.knowledge, self.store, session_id, fail_tool)
        self.store.message(session_id, "user", f"Triage batch {batch_id} with {len(claims)} claims")
        try:
            route = self.backend.route("Triage this batch of claims", self.store.context(session_id), session_id, has_batch=True)
            self.event(session_id, "supervisor_route", batch_id=batch_id, route=route)
            if route["intent"] != "triage":
                raise ValueError("Supervisor did not select triage for the supplied batch")
            intake = self.backend.execute("intake", {"batch_id": batch_id}, session_id, registry)
            self.event(session_id, "intake", batch_id=batch_id, count=len(claims))
            for validation in intake["tool_result"]["claims"]:
                key = validation["claim_key"]
                registry.scope_key = key
                claim = claims[int(key) - 1]
                result = {"claim_key": key, "claim": claim, "validation": validation, "review_status": "pending_review"}
                self.event(session_id, "validation_gate", claim_key=key, passed=validation["valid"])
                try:
                    coverage = {"checks": {}, "findings": [], "retrieved_rules": [], "skipped": True}
                    if validation["valid"]:
                        answer = self.backend.execute("anomaly", {"claim_key": key, "claim": claim}, session_id, registry)
                        coverage = answer["tool_result"]
                        result["anomaly_agent_analysis"] = answer["text"]
                        result["azure_citations"] = answer["citations"]
                    self.event(session_id, "coverage_anomalies", claim_key=key, skipped=not validation["valid"])
                    action = recommendation(validation, coverage)
                    answer = self.backend.execute("briefing", {"claim_key": key, "claim": claim, "validation": validation,
                        "coverage": coverage, "recommended_action": action}, session_id, registry)
                    history = answer["tool_result"]
                    result.update(coverage=coverage, history=history, recommended_action=action,
                                  briefing=briefing(claim, validation, coverage, history, action),
                                  briefing_agent_analysis=answer["text"])
                    self.event(session_id, "briefing", claim_key=key, recommendation=action)
                except Exception as exc:
                    result.update(error=f"{type(exc).__name__}: {exc}", recommended_action="manual_review_required",
                                  review_status="blocked", briefing="Processing failed. Correct the error and rerun this claim before review.")
                    self.event(session_id, "error", claim_key=key, error=result["error"])
                batch["results"].append(result)
            batch["status"] = "partial_failure" if any(r.get("error") for r in batch["results"]) else "pending_review"
            self.event(session_id, "human_review", batch_id=batch_id, status=batch["status"], gate="awaiting_recorded_decision")
        except Exception as exc:
            batch.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            self.event(session_id, "error", batch_id=batch_id, error=batch["error"])
        self.store.save_batch(session_id, batch)
        self.store.set_context(session_id, last_batch_id=batch_id, last_claim_id=None)
        self.store.message(session_id, "assistant", f"Batch {batch_id}: {batch['status']}")
        return batch

    def ask(self, request, session_id):
        if not request.strip():
            raise ValueError("Enter a question")
        context = self.store.context(session_id)
        self.store.message(session_id, "user", request)
        route = self.backend.route(request, context, session_id)
        self.event(session_id, "supervisor_route", route=route)
        batch = self.store.batch(context.get("last_batch_id", ""))
        cid = route.get("claim_id")
        if route["intent"] == "explain" and cid and batch:
            normalized = cid.upper().replace("-", "")
            matches = [r for r in batch["results"] if str(r["claim"].get("claim_id", "")).upper().replace("-", "") == normalized]
            if len(matches) > 1:
                answer = f"{cid} occurs more than once in this batch. Resolve duplicate IDs before requesting a unique claim explanation."
            elif matches:
                result = matches[0]
                self.store.set_context(session_id, last_claim_id=result["claim"].get("claim_id"))
                answer = self.backend.explain(request, result, session_id)
                if result.get("review"):
                    answer += "\n\nRecorded human review: " + json.dumps(result["review"])
            else:
                answer = f"Claim {cid} is not present in this session's latest batch."
        else:
            answer = "Upload or triage a batch, then ask about a specific claim ID. Follow-up questions reuse the most recently discussed claim in this session."
        self.store.message(session_id, "assistant", answer)
        return answer

    def close(self):
        if hasattr(self.backend, "close"):
            self.backend.close()
