"""No LLM: deterministic stand-in for routing and prose, real tools and state."""
import re


class MockBackend:
    name = "mock"

    def route(self, request, context, session_id, has_batch=False):
        if has_batch:
            return {"intent": "triage", "claim_id": None}
        match = re.search(r"\bC-?([A-Za-z]*\d+)\b", request, re.I)
        claim_id = "C-" + match.group(1).upper() if match else context.get("last_claim_id")
        return {"intent": "explain" if claim_id else "help", "claim_id": claim_id}

    def execute(self, role, payload, session_id, registry):
        name = {"intake": "validate_claims", "anomaly": "check_coverage", "briefing": "get_claim_history"}[role]
        arguments = {"batch_id": payload["batch_id"]} if role == "intake" else {"claim_key": payload["claim_key"]}
        result = registry.execute(role, name, arguments)
        return {"text": "", "tool_result": result, "backend": self.name, "citations": []}

    def explain(self, request, result, session_id):
        return result["briefing"]
