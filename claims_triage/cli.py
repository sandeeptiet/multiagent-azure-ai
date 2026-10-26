import argparse
import json
import logging
import sys
from pathlib import Path
from .config import ROOT, Settings
from .tools import parse_claims
from .workflow import TriageApplication


def main(argv=None):
    parser = argparse.ArgumentParser(description="Synthetic insurance claims triage")
    parser.add_argument("--backend", choices=["mock", "azure"])
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    triage = sub.add_parser("triage", help="Triage a JSON/CSV batch")
    triage.add_argument("path", type=Path)
    triage.add_argument("--session")
    triage.add_argument("--seed-history", action="store_true")
    triage.add_argument("--fail-tool", choices=["validate_claims", "check_coverage", "get_claim_history"])
    triage.add_argument("--output", type=Path)
    ask = sub.add_parser("ask", help="Ask a follow-up in an existing session")
    ask.add_argument("question")
    ask.add_argument("--session", required=True)
    review = sub.add_parser("review", help="Record a human decision, never a payout")
    review.add_argument("--batch", required=True)
    review.add_argument("--claim-key", required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--decision", required=True, choices=["auto_approve", "route_to_investigator", "request_more_documentation"])
    review.add_argument("--notes", required=True)
    sub.add_parser("seed-history", help="Idempotently insert synthetic prior history")
    sub.add_parser("provision", help="Create/update the four Azure agents and file-search index")
    sub.add_parser("doctor", help="Report configuration presence without exposing credentials")
    trace = sub.add_parser("trace", help="Show persisted events for a session")
    trace.add_argument("--session", required=True)
    sub.add_parser("demo", help="Demonstrate batch, follow-up memory, cross-run history and review")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(message)s")
    settings = Settings.load(args.backend, args.state_dir)
    if args.command == "doctor":
        import importlib.metadata as metadata
        print(json.dumps({"backend": settings.backend, "project_endpoint_configured": bool(settings.project_endpoint),
                          "model_deployment_configured": bool(settings.model_deployment), "state_dir": str(settings.state_dir),
                          "azure_ai_projects": metadata.version("azure-ai-projects") if _installed("azure-ai-projects") else None,
                          "azure_ai_agents": metadata.version("azure-ai-agents") if _installed("azure-ai-agents") else None}, indent=2))
        return 0
    app = None
    try:
        if args.command == "provision" and settings.backend != "azure":
            raise ValueError("Use --backend azure provision to create Azure resources.")
        app = TriageApplication(settings)
        if args.command == "provision":
            result = app.backend.provision()
        elif args.command == "seed-history":
            app.store.seed(settings.data_dir / "history_seed.json")
            result = {"status": "seeded", "synthetic": True}
        elif args.command == "triage":
            if args.seed_history:
                app.store.seed(settings.data_dir / "history_seed.json")
            result = app.triage(parse_claims(args.path.read_text(), args.path.suffix), args.session, args.fail_tool)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(result, indent=2))
        elif args.command == "ask":
            print(app.ask(args.question, args.session))
            return 0
        elif args.command == "review":
            result = app.store.review(args.batch, args.claim_key, args.reviewer, args.decision, args.notes)
        elif args.command == "trace":
            result = app.store.events(args.session)
        elif args.command == "demo":
            app.store.seed(settings.data_dir / "history_seed.json")
            batch = app.triage(json.loads((settings.data_dir / "claims.json").read_text()))
            if batch["status"] in {"failed", "partial_failure"}:
                print(json.dumps(batch, indent=2))
                return 1
            session = batch["session_id"]
            first = app.ask("Why was claim C-2031 flagged?", session)
            followup = app.ask("What should I do next for that claim?", session)
            reviewed = app.store.review(batch["batch_id"], "5", "Demo reviewer", "auto_approve", "Synthetic demo: verified all deterministic controls and no risk flags.")
            app.close()
            app = TriageApplication(settings)
            new_session = app.store.session()
            persistent = app.store.history("POL-5521", "2026-08-15", "C-2031")
            result = {"backend": settings.backend, "batch_id": batch["batch_id"], "session_id": session,
                      "outcomes": [{"claim_id": r["claim"].get("claim_id"), "action": r["recommended_action"]} for r in batch["results"]],
                      "first_answer": first, "contextual_followup": followup,
                      "reviewed_claim": reviewed["claim"]["claim_id"], "new_session": new_session,
                      "persistent_history_after_restart": [h["claim_id"] for h in persistent]}
        print(json.dumps(result, indent=2))
        return 1 if isinstance(result, dict) and result.get("status") in {"failed", "partial_failure"} else 0
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 1
    finally:
        if app:
            app.close()


def _installed(package):
    import importlib.metadata as metadata
    try:
        metadata.version(package)
        return True
    except metadata.PackageNotFoundError:
        return False
