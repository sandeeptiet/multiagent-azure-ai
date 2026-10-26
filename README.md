# Insurance claims triage on Azure AI Foundry

A four-agent assignment implementation with a local demo interface, an explicit Python workflow, three custom tools, retrieved underwriting rules, conversation context, persistent claim history, and recorded human review.

All supplied claims, policies and underwriting rules are synthetic. This demonstration recommends next actions and records a review. It never pays, approves or rejects a real claim.

## Current execution modes

- **Mock:** no LLM or Azure calls. Routing and narrative are deterministic stand-ins. Validation, coverage calculations, vector retrieval, SQLite memory, checkpoints, logs and reviews execute normally.
- **Azure:** the SDK creates four actual Foundry Agent Service agents and a File Search vector store. Each specialist must invoke its registered function tool. Azure narratives are displayed alongside deterministic evidence and recommendations. Errors never silently switch to mock mode.

Azure integration is implemented and covered by service contract tests. A real project endpoint, model deployment and authenticated account are needed for live verification. Live cloud execution has not yet been verified in this workspace.

This project intentionally pins **azure-ai-projects 1.0.0** and **azure-ai-agents 1.1.0**, matching the assignment's thread/run Agent Service API, now called **Foundry classic**. Projects SDK v2 and the newer Responses/agent-version API are different APIs. Do not upgrade the SDK major version without migrating the adapter. Use a Foundry resource project that supports classic Agent Service, not a legacy hub connection string or a model inference endpoint.

## Run locally

Python 3.11 or later is required. Commands below run from the project folder.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[azure,ui,dev]'
python -m claims_triage --backend mock --state-dir .runtime/demo demo
python -m streamlit run app.py
```

The supplied workspace already has its virtual environment installed. Open http://127.0.0.1:8501 when the server is running. `requirements.lock` records the verified environment's pinned third-party dependencies. To recreate that environment, install the lock file, then install the project with `python -m pip install --no-deps -e .`.

In the interface, select **mock**, click **Load synthetic prior history**, and then **Triage batch**. Expand a claim to see its brief, retrieved rules and human review form. Use **Ask about a claim** for a multi-turn conversation. Use **New conversation** to start fresh session context without erasing persistent history.

VS Code launch configurations are supplied for the demo and interface.

## Connect the real Azure agents

1. Create a Foundry resource project supporting classic Agent Service and deploy a chat model that supports function calling and File Search.
2. Copy `.env.example` to `.env`. Set the **project endpoint** and **model deployment name**. The deployment name is the name in your project, which may differ from the model family name.
3. Authenticate through `DefaultAzureCredential`. For local interactive setup, install Azure CLI and sign in yourself with `az login`. The selected identity needs permission to use the project/Agent Service. Never paste keys into source code.
4. Run the configuration check, then create the agents and retrieval source:

```bash
python -m claims_triage doctor
python -m claims_triage --backend azure provision
python -m claims_triage --backend azure --verbose triage data/claims.json --seed-history
```

The endpoint should resemble `https://<resource>.services.ai.azure.com/api/projects/<project>`. Never substitute the model inference endpoint.

`provision` writes IDs to `.runtime/azure-agents.json` after each creation. Re-running updates this application's agents and reuses the knowledge store when its content is unchanged. Changing prompts, tool schemas, model or rules requires another `provision`. Changing project endpoint requires a separate state directory. Cloud resource creation and model runs can incur Azure charges. The program does not provision subscriptions, projects or model deployments.

The current development machine did not have `az` or `azd` installed when implementation began. `azd` is not required for this local SDK client. The user is creating the Foundry project separately. Until that setup is complete, use the explicitly labelled mock mode, as the assignment permits for access/quota constraints.

If Azure reports insufficient quota, missing roles, unsupported File Search, or an inaccessible endpoint, the error is surfaced and logged. Correct the underlying setup or select mock explicitly. Do not remove the SDK integration or report mock output as cloud execution.

## Four agents and three tools

| Agent | Custom function | Responsibility |
|---|---|---|
| Supervisor | None | Route batch requests and contextual questions, coordinate the explicit routine |
| Intake & Validation | `validate_claims(batch_id)` | Required fields, IDs, policy numbers, dates and amounts |
| Anomaly & Coverage | `check_coverage(claim_key)` | Coverage, active dates, limits and retrieved underwriting controls |
| Adjuster Briefing | `get_claim_history(claim_key)` | Earlier policy history and a next-action briefing |

Each function has an explicit JSON schema. The dispatcher validates arguments, agent ownership and active batch/claim scope. Tools execute in the local application, not in the model. Azure mode adds the built-in File Search tool to the anomaly agent in addition to its custom function.

## Workflow and review gates

`supervisor_route → intake → validation_gate → coverage_anomalies → briefing → human_review`

- Invalid rows stop before coverage checks. They receive a correction briefing and can only be reviewed as a request for documentation. Correct the data and triage it again to resume evaluation.
- Valid rows receive checks and findings with rule IDs, source text, and a knowledge version hash.
- A clean claim receives `auto_approve` as a **recommendation**. A human must record a review before its state changes to `reviewed`.
- Risk/coverage flags cannot be approved through the demo review gate. They can be routed for investigation or documentation. This prototype does not implement an investigator override.
- A per-claim tool failure blocks that claim while the batch continues. Intake or supervisor failure stops the batch. Blocked claims must be rerun before review.
- Human review needs a named reviewer and notes. Repeat review of the same result is rejected and records persist transactionally.

## Both kinds of memory

**Conversation memory:** each conversation has a session ID, message history, the last batch ID and last discussed claim. Azure mode also keeps one remote thread per agent within that session. Asking “Why was C-2031 flagged?” followed by “What should I do next for that claim?” retains the reference. A new session cannot inherit that reference.

**Persistent memory:** `.runtime/memory.sqlite3` stores synthetic claim history across sessions and processes. The historical seed is explicit and idempotent. History excludes the current claim ID, future losses and other policies. Earlier valid submissions in the same batch are also visible as unreviewed observations, regardless of input order. Valid observations persist as `pending_review`, not as approved facts. Reviewed records retain reviewer, decision and notes. Re-running a claim does not duplicate its history record. SQLite replaces the optional JSON store in the initial high-level slide so reviews and state updates can use transactions.

## Retrieval and grounding

`knowledge/underwriting_rules.md` contains eight separate, retrievable rule documents. Rule metadata carries thresholds beside the prose; it is not embedded in agent instructions. The local index persists normalized token-frequency vectors and ranks applicable documents by cosine similarity. This is a **sparse lexical vector store**, not a semantic embedding service. It retrieves all applicable controls rather than dropping mandatory controls through top-k filtering.

`check_coverage` evaluates the structured rules and returns the retrieved source text, rule IDs, scores and source hash. Azure agents use that evidence to generate their explanations. Azure provisioning also uploads the rules into a Foundry File Search vector store and attaches it to the anomaly agent. Its annotations are retained in the result when returned. Local mock mode simulates generation only; it does not simulate retrieval.

## Demonstration outcomes

With the sample data and synthetic history loaded in a fresh state directory:

| Claim | Expected recommendation | Main evidence |
|---|---|---|
| C-2031 | Route to investigator | R06, earlier water-damage claims C-H100 and C-2032 |
| C-2032 | Auto-approve recommendation | Valid coverage, no configured flags |
| C-2033 | Route to investigator | R04, 3 days from inception; R05, 100% of limit |
| C-2034 | Request documentation | Missing loss date and nonpositive amount; coverage unknown |
| C-2035 | Auto-approve recommendation | Clean synthetic auto claim |

C-2034's policy end date does not prove the loss was uncovered because its loss date is missing. The application does not make that inference.

## Command-line walkthrough

```bash
python -m claims_triage --backend mock triage data/claims.json --seed-history --output .runtime/results.json
python -m claims_triage --backend mock ask 'Why was claim C-2031 flagged?' --session <session-id>
python -m claims_triage --backend mock ask 'What next for that claim?' --session <session-id>
python -m claims_triage --backend mock trace --session <session-id>
python -m claims_triage --backend mock review --batch <batch-id> --claim-key 5 --reviewer 'Demo reviewer' --decision auto_approve --notes 'Reviewed coverage and evidence'
python -m claims_triage --backend mock triage data/invalid_claims.json
python -m claims_triage --backend mock triage data/claims.json --fail-tool check_coverage
```

Add `--verbose` before the subcommand for console event logs. Session events always persist in SQLite. Batch and claim identifiers correlate the workflow. The UI exports full results as JSON. No secrets are written to event logs.

## Tests and implementation map

```bash
python -m pytest -q
```

Tests cover domain boundaries, duplicates, malformed inputs, CSV parsing, retrieved rule changes, missing controls, separate-process memory, session isolation, review gates, failure isolation, SDK schema serialization, agent creation/reuse, required tool calls, run-scoped messages, timeout cancellation, and UI loading. Azure service contract tests use a fake service with real SDK tool models, not a live Azure deployment.

| File | What to inspect in the interview |
|---|---|
| `claims_triage/agents.py` | Four agent definitions and role-specific instructions |
| `claims_triage/backends/azure.py` | SDK creation, File Search, run polling and tool submission |
| `claims_triage/tools.py` | Three JSON schemas and deterministic checks |
| `claims_triage/workflow.py` | Explicit sequence, gates and failure handling |
| `claims_triage/storage.py` | Session context, persistent history and review transactions |
| `claims_triage/knowledge.py` | Document ingestion and sparse vector retrieval |
| `app.py` | Demonstration interface |
| `docs/DEMO.md` | Meeting walkthrough |
| `docs/ARCHITECTURE.md` | Architecture and scope choices |
| `output/Claims_Triage_Architecture_v3.pptx` | Previously prepared single-slide architecture |

## Scope and operational limits

This is a single-user assignment application, not a production claims platform. It has no user authentication, payout system, production fraud model, retention policy or multi-user authorization. Serve the UI only on localhost. Policy matching uses explicitly declared claim types, not real policy wording or exclusions. Same-day history is excluded because the sample has dates without event times. The local routine has no distributed job queue or automatic resumption of failed remote runs. Review supports recording the next step; investigation itself remains outside scope.

Cloud provisioning tracks current application-owned agent/file/vector IDs. If rules are replaced, older uploaded files/vector stores may remain in the project; inspect the manifest and remove unneeded application resources deliberately in Foundry. No automatic deletion of cloud resources occurs.

## Official SDK references

- [Azure AI Agents Python SDK](https://learn.microsoft.com/en-us/python/api/overview/azure/ai-agents-readme?view=azure-python)
- [Azure AI Projects 1.0.0 SDK and classic agent integration](https://github.com/Azure/azure-sdk-for-python/tree/azure-ai-projects_1.0.0/sdk/ai/azure-ai-projects)
- [Microsoft function-calling sample](https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/ai/azure-ai-agents/samples/agents_tools/sample_agents_functions.py)
- [Foundry classic File Search](https://learn.microsoft.com/en-us/azure/ai-foundry/agents/how-to/tools/file-search-upload-files)
