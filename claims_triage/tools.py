"""Three scoped custom tools with explicit JSON schemas and real local execution."""
import csv
import io
import json
import re
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
from jsonschema import validate

REQUIRED = ("claim_id", "policy_number", "claim_type", "loss_date", "report_date", "claim_amount", "description")
TOOL_SCHEMAS = {
    "validate_claims": {"type": "function", "function": {
        "name": "validate_claims", "description": "Validate the active batch. Returns an independent result per row, including duplicate IDs.",
        "parameters": {"type": "object", "properties": {"batch_id": {"type": "string"}}, "required": ["batch_id"], "additionalProperties": False}}},
    "check_coverage": {"type": "function", "function": {
        "name": "check_coverage", "description": "Check one validated row against policies and retrieved underwriting rules. Returns evidence and risk indicators.",
        "parameters": {"type": "object", "properties": {"claim_key": {"type": "string"}}, "required": ["claim_key"], "additionalProperties": False}}},
    "get_claim_history": {"type": "function", "function": {
        "name": "get_claim_history", "description": "Retrieve earlier same-policy claims for a row, excluding current claim ID and future losses.",
        "parameters": {"type": "object", "properties": {"claim_key": {"type": "string"}}, "required": ["claim_key"], "additionalProperties": False}}},
}
ROLE_TOOLS = {"supervisor": [], "intake": ["validate_claims"], "anomaly": ["check_coverage"], "briefing": ["get_claim_history"]}


def parse_claims(text, suffix=".json"):
    if len(text.encode()) > 2_000_000:
        raise ValueError("Input exceeds the 2 MB demonstration limit.")
    try:
        if suffix.lower() == ".csv":
            reader = csv.DictReader(io.StringIO(text))
            if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
                raise ValueError("CSV must have unique column headers.")
            claims = list(reader)
        else:
            claims = json.loads(text, parse_constant=lambda s: (_ for _ in ()).throw(ValueError(f"Invalid JSON number: {s}")))
    except (json.JSONDecodeError, csv.Error) as exc:
        raise ValueError(f"Malformed claim input: {exc}") from exc
    if not isinstance(claims, list) or not claims or len(claims) > 100:
        raise ValueError("Provide a nonempty JSON array or CSV with at most 100 claims.")
    if not all(isinstance(c, dict) for c in claims):
        raise ValueError("Every claim must be an object.")
    return claims


def parse_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Expected YYYY-MM-DD")
    return date.fromisoformat(value)


def money(value):
    if isinstance(value, (bool, dict, list)) or value is None:
        raise ValueError("Expected a numeric amount")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Expected a numeric amount") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError("Amount must be finite and greater than zero")
    if number.quantize(Decimal("0.01")) != number:
        raise ValueError("Amount must have at most two decimal places")
    return number


class ToolRegistry:
    def __init__(self, batch_id, claims, policies, knowledge, store, session_id, fail_tool=None):
        self.batch_id, self.claims, self.policies = batch_id, claims, policies
        self.knowledge, self.store, self.session_id = knowledge, store, session_id
        self.fail_tool = fail_tool
        self.calls = []
        self.validations = {}
        self.scope_key = None

    def execute(self, role, name, arguments):
        if name not in ROLE_TOOLS.get(role, []):
            raise ValueError(f"Tool {name} is not registered for {role}.")
        validate(arguments, TOOL_SCHEMAS[name]["function"]["parameters"])
        if self.scope_key and arguments.get("claim_key") != self.scope_key:
            raise ValueError("Tool arguments refer to a different claim row.")
        self.store.event(self.session_id, stage="tool_call", agent=role, tool=name, arguments=arguments)
        if name == self.fail_tool:
            self.fail_tool = None
            raise RuntimeError(f"Simulated {name} failure")
        result = getattr(self, name)(**arguments)
        self.calls.append({"role": role, "tool": name, "arguments": arguments, "result": result})
        return result

    def validate_claims(self, batch_id):
        if batch_id != self.batch_id:
            raise ValueError("Tool cannot access a different batch.")
        ids = Counter(c.get("claim_id") for c in self.claims if isinstance(c.get("claim_id"), str))
        results = []
        for index, claim in enumerate(self.claims):
            errors = []
            for field in REQUIRED:
                if claim.get(field) is None or claim.get(field) == "":
                    errors.append(f"Missing required field: {field}")
                elif field != "claim_amount" and (not isinstance(claim[field], str) or not claim[field].strip()):
                    errors.append(f"{field} must be nonempty text")
            cid, policy = claim.get("claim_id"), claim.get("policy_number")
            if isinstance(cid, str) and ids[cid] > 1:
                errors.append(f"Duplicate claim ID: {cid}")
            if not isinstance(policy, str) or not re.fullmatch(r"POL-\d+", policy) or policy not in self.policies:
                errors.append("Invalid or unknown policy number")
            dates = {}
            for field in ("loss_date", "report_date"):
                try:
                    dates[field] = parse_date(claim.get(field))
                except ValueError:
                    errors.append(f"Invalid {field}: use a real YYYY-MM-DD date")
            if len(dates) == 2 and dates["loss_date"] > dates["report_date"]:
                errors.append("Loss date occurs after report date")
            try:
                money(claim.get("claim_amount"))
            except (ValueError, InvalidOperation):
                errors.append("Claim amount must be a positive finite number with at most two decimal places")
            result = {"claim_key": str(index + 1), "valid": not errors, "errors": errors}
            self.validations[result["claim_key"]] = result
            results.append(result)
        return {"batch_id": batch_id, "claims": results}

    def claim(self, claim_key):
        if claim_key not in self.validations:
            raise ValueError("Claim must pass through intake first.")
        return self.claims[int(claim_key) - 1]

    def get_claim_history(self, claim_key):
        claim = self.claim(claim_key)
        if not self.validations[claim_key]["valid"]:
            return {"records": [], "reason": "Invalid intake; history lookup skipped"}
        records = self.store.history(claim["policy_number"], claim["loss_date"], claim["claim_id"])
        by_id = {r["claim_id"]: r for r in records}
        # Earlier valid submissions in this same batch are observations, not reviewed decisions.
        # Use their dates rather than the order in which the CSV/JSON happened to arrive.
        for key, validation in self.validations.items():
            if not validation["valid"]:
                continue
            earlier = self.claims[int(key) - 1]
            if (earlier["claim_id"] != claim["claim_id"] and earlier["policy_number"] == claim["policy_number"]
                    and earlier["loss_date"] < claim["loss_date"] and earlier["claim_id"] not in by_id):
                by_id[earlier["claim_id"]] = {**earlier, "source": "current_batch",
                                              "review_status": "unreviewed_observation"}
        return {"records": sorted(by_id.values(), key=lambda r: r["loss_date"], reverse=True)}

    def check_coverage(self, claim_key):
        claim = self.claim(claim_key)
        if not self.validations[claim_key]["valid"]:
            raise ValueError("Coverage gate: invalid claims cannot be evaluated.")
        policy = self.policies[claim["policy_number"]]
        amount, limit = money(claim["claim_amount"]), money(policy["policy_limit"])
        loss, report = parse_date(claim["loss_date"]), parse_date(claim["report_date"])
        start, end = parse_date(policy["active_from"]), parse_date(policy["active_to"])
        history = self.get_claim_history(claim_key)["records"]
        documents = self.knowledge.retrieve(claim["claim_type"], f"{claim['claim_type']} coverage active policy limit inception repeat loss late report")
        findings, checks = [], {}
        for document in documents:
            rule = document["metadata"]
            kind = rule["kind"]
            action, reason = None, None
            if kind == "covered_type":
                checks["claim_type_covered"] = claim["claim_type"] in policy["covered_claim_types"]
                if not checks["claim_type_covered"]:
                    action, reason = "route_to_investigator", "Claim type is not covered by the policy"
            elif kind == "active_period":
                checks["loss_in_active_period"] = start <= loss <= end
                if not checks["loss_in_active_period"]:
                    action, reason = "route_to_investigator", "Loss falls outside the active policy period"
            elif kind == "limit":
                checks["within_policy_limit"] = amount <= limit
                if not checks["within_policy_limit"]:
                    action, reason = "route_to_investigator", "Claim amount exceeds the policy limit"
            elif kind == "early_report" and 0 <= (report - start).days <= rule["days"]:
                action, reason = "route_to_investigator", f"Claim reported {(report - start).days} days after policy inception"
            elif kind == "near_limit" and limit * (1 - Decimal(str(rule["fraction"]))) <= amount <= limit:
                action, reason = "route_to_investigator", f"Claim amount is {amount / limit:.1%} of the policy limit"
            elif kind == "repeat_loss":
                prior = [h for h in history if h["claim_type"] == claim["claim_type"] and 0 < (loss - parse_date(h["loss_date"])).days <= rule["days"]]
                if prior:
                    action, reason = "route_to_investigator", "Similar prior loss: " + ", ".join(h["claim_id"] for h in prior)
            elif kind == "late_report" and (report - loss).days > rule["days"]:
                action, reason = "request_more_documentation", f"Reported {(report - loss).days} days after loss"
            elif kind not in {"early_report", "near_limit", "repeat_loss", "late_report", "invalid_input"}:
                raise ValueError(f"Unknown knowledge rule kind: {kind}")
            if reason:
                findings.append({"rule_id": document["rule_id"], "reason": reason, "action": action, "source": document["source"], "evidence": document["text"]})
        if set(checks) != {"claim_type_covered", "loss_in_active_period", "within_policy_limit"}:
            raise ValueError("Required coverage controls are missing from the knowledge base.")
        self.store.event(self.session_id, stage="retrieval", claim_key=claim_key, rule_ids=[d["rule_id"] for d in documents], index_sha256=self.knowledge.digest)
        return {"checks": checks, "findings": findings, "retrieved_rules": documents, "policy": policy, "knowledge_sha256": self.knowledge.digest}
