"""Classic Azure AI Foundry Agent Service integration, pinned to SDK v1.

SDK operations are real. Local mock mode is a separate explicit backend.
"""
import hashlib
import json
import time
from pathlib import Path
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from azure.ai.agents.models import FilePurpose, FileSearchTool, FunctionDefinition, FunctionToolDefinition, ToolOutput
from ..agents import AGENTS
from ..tools import ROLE_TOOLS


def value(item):
    """Azure SDK models may expose str enums or plain wire strings."""
    return getattr(item, "value", item)


class AzureBackend:
    name = "azure"

    def __init__(self, settings, store, client=None):
        if not settings.project_endpoint or not settings.model_deployment:
            raise ValueError("Azure mode requires AZURE_AI_PROJECT_ENDPOINT and AZURE_AI_MODEL_DEPLOYMENT_NAME in .env.")
        if not settings.project_endpoint.startswith("https://") or "/api/projects/" not in settings.project_endpoint:
            raise ValueError("Use the HTTPS Foundry project endpoint, not a model inference endpoint.")
        self.settings, self.store = settings, store
        self.path = settings.state_dir / "azure-agents.json"
        self.project = None
        self.credential = None
        if client is None:
            self.credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
            self.project = AIProjectClient(endpoint=settings.project_endpoint, credential=self.credential,
                                           connection_timeout=10, read_timeout=20, retry_total=1)
            client = self.project.agents
        self.client = client
        self.manifest = json.loads(self.path.read_text()) if self.path.exists() else {}
        if self.manifest and self.manifest.get("endpoint") != settings.project_endpoint:
            raise ValueError("State belongs to another Azure project. Use a different TRIAGE_STATE_DIR.")

    def fingerprint(self):
        content = {"endpoint": self.settings.project_endpoint, "model": self.settings.model_deployment,
                   "knowledge": (self.settings.knowledge_dir / "underwriting_rules.md").read_text(),
                   "agents": {role: {"instructions": a.instructions, "tools": a.tools} for role, a in AGENTS.items()}}
        return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()

    def save_manifest(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.manifest, indent=2))
        temporary.replace(self.path)

    def provision(self):
        """Create or update only this application's four agents; retain IDs for reruns."""
        self.manifest.setdefault("endpoint", self.settings.project_endpoint)
        self.manifest.setdefault("agents", {})
        knowledge_hash = hashlib.sha256((self.settings.knowledge_dir / "underwriting_rules.md").read_bytes()).hexdigest()
        if self.manifest.get("knowledge_hash") != knowledge_hash:
            uploaded = self.client.files.upload_and_poll(file_path=str(self.settings.knowledge_dir / "underwriting_rules.md"), purpose=FilePurpose.AGENTS, timeout=self.settings.timeout)
            self.manifest["uploaded_file_id"] = uploaded.id
            self.save_manifest()
            vector = self.client.vector_stores.create_and_poll(file_ids=[uploaded.id], name="claims-underwriting-rules", timeout=self.settings.timeout)
            if value(vector.status) != "completed":
                raise RuntimeError(f"Knowledge indexing did not complete: {vector.status}")
            self.manifest.update(vector_store_id=vector.id, knowledge_hash=knowledge_hash)
            self.save_manifest()
        file_search = FileSearchTool(vector_store_ids=[self.manifest["vector_store_id"]])
        for role, agent in AGENTS.items():
            tools = [FunctionToolDefinition(function=FunctionDefinition(**t["function"])) for t in agent.tools]
            args = {"name": agent.name, "model": self.settings.model_deployment, "instructions": agent.instructions, "tools": tools}
            if role == "anomaly":
                args["tools"] = tools + file_search.definitions
                args["tool_resources"] = file_search.resources
            if role in self.manifest["agents"]:
                remote = self.client.update_agent(agent_id=self.manifest["agents"][role], **args)
            else:
                remote = self.client.create_agent(**args)
            self.manifest["agents"][role] = remote.id
            self.save_manifest()
        self.manifest.update(fingerprint=self.fingerprint(), model=self.settings.model_deployment)
        self.save_manifest()
        return self.manifest

    def thread(self, session_id, role):
        context = self.store.context(session_id)
        key = f"azure_thread_{role}"
        thread_id = context.get(key)
        if not thread_id:
            thread_id = self.client.threads.create().id
            self.store.set_context(session_id, **{key: thread_id})
        return thread_id

    def run(self, role, payload, session_id, registry=None):
        if self.manifest.get("fingerprint") != self.fingerprint():
            raise ValueError("Agents are missing or configuration changed. Run: python -m claims_triage provision")
        thread_id = self.thread(session_id, role)
        self.client.messages.create(thread_id=thread_id, role="user", content=json.dumps(payload))
        run = self.client.runs.create(thread_id=thread_id, agent_id=self.manifest["agents"][role])
        self.store.event(session_id, stage="azure_run", agent=role, thread_id=thread_id, run_id=run.id)
        deadline, tool_result, seen, tool_errors = time.monotonic() + self.settings.timeout, None, {}, []
        try:
            while value(run.status) in {"queued", "in_progress", "requires_action", "cancelling"}:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"{role} exceeded the configured run deadline")
                if value(run.status) == "requires_action":
                    action = run.required_action
                    if not action or not getattr(action, "submit_tool_outputs", None):
                        raise RuntimeError("Unsupported Azure run action")
                    outputs = []
                    for call in action.submit_tool_outputs.tool_calls:
                        if call.id not in seen:
                            if len(seen) >= 12:
                                raise RuntimeError("Agent exceeded the tool-call limit")
                            try:
                                if not registry or call.type != "function":
                                    raise ValueError("Unexpected tool call")
                                result = registry.execute(role, call.function.name, json.loads(call.function.arguments))
                                tool_result = result
                                seen[call.id] = json.dumps(result)
                            except Exception as exc:
                                tool_errors.append(str(exc))
                                seen[call.id] = json.dumps({"error": str(exc), "success": False})
                        outputs.append(ToolOutput(tool_call_id=call.id, output=seen[call.id]))
                    if not outputs:
                        raise RuntimeError("Run requested tools but supplied no calls")
                    run = self.client.runs.submit_tool_outputs(thread_id=thread_id, run_id=run.id, tool_outputs=outputs)
                else:
                    time.sleep(min(0.5, max(0, deadline - time.monotonic())))
                run = self.client.runs.get(thread_id=thread_id, run_id=run.id)
            if value(run.status) != "completed":
                raise RuntimeError(f"Azure {role} run ended with {run.status}: {run.last_error}")
            if tool_errors:
                raise RuntimeError("Agent tool failure: " + "; ".join(tool_errors))
            if ROLE_TOOLS[role] and tool_result is None:
                raise RuntimeError(f"{role} did not invoke its required custom tool")
            texts, citations = [], []
            for message in self.client.messages.list(thread_id=thread_id, run_id=run.id, order="asc"):
                if value(message.role) == "assistant":
                    for item in message.text_messages:
                        texts.append(item.text.value)
                        citations.extend([a.as_dict() for a in item.text.annotations or []])
            if not texts:
                raise RuntimeError("Azure run produced no assistant text")
            return {"text": "\n".join(texts), "tool_result": tool_result, "backend": self.name, "citations": citations}
        except Exception:
            # Best-effort cancellation prevents stale active runs from blocking later turns.
            try:
                self.client.runs.cancel(thread_id=thread_id, run_id=run.id)
            except Exception:
                pass
            raise

    def route(self, request, context, session_id, has_batch=False):
        result = self.run("supervisor", {"task": "route", "request": request, "has_batch": has_batch,
                                        "last_claim_id": context.get("last_claim_id")}, session_id)
        text = result["text"].strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        route = json.loads(text)
        if not isinstance(route, dict) or route.get("intent") not in {"triage", "explain", "help"}:
            raise ValueError("Supervisor returned an invalid route")
        cid = route.get("claim_id")
        if cid is not None and not isinstance(cid, str):
            raise ValueError("Supervisor returned an invalid claim reference")
        return route

    def execute(self, role, payload, session_id, registry):
        return self.run(role, payload, session_id, registry)

    def explain(self, request, result, session_id):
        return self.run("supervisor", {"task": "explain", "request": request, "verified_result": result}, session_id)["text"]

    def close(self):
        if self.project:
            self.project.close()
        if self.credential:
            self.credential.close()
