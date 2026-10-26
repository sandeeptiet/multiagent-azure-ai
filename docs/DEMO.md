# Interview walkthrough

Use a fresh state directory for a repeatable demonstration. Keep the execution-mode banner visible so the audience knows whether LLM calls are live or mocked.

1. Open the architecture slide. Explain the supervisor, three specialists, custom tools, retrieval and the two kinds of memory.
2. Show `agents.py` and `backends/azure.py`. Point out that the SDK creates all four agents, assigns instructions, registers schemas and attaches File Search to the anomaly agent. Show the provisioned agent IDs only after live provisioning succeeds.
3. Start the interface. Load synthetic prior history and triage the sample batch. Explain C-2033's new-policy and near-limit flags and C-2034's invalid intake.
4. Expand C-2031. Show the R06 evidence and synthetic prior water-damage history. Distinguish retrieved underwriting knowledge from remembered claims.
5. Ask “Why was C-2031 flagged?” and then “What should I do next for that claim?” Explain the reused session and, in Azure mode, remote thread.
6. Start a new conversation or restart the application. Re-triage to show persistent history remains while ambiguous conversation context does not carry over.
7. Open C-2035 and record a review with a reviewer name and note. Explain that this records a synthetic decision and does not execute a payout. Show why an invalid or flagged claim cannot bypass its gate.
8. Open the workflow trace. Show routing, tool calls, retrieved rule IDs and human review events.
9. Enable the tool-failure switch and run another batch. Explain that a failed claim is blocked while other claims continue. For malformed input or duplicate IDs, use `data/invalid_claims.json`.
10. Show the test results and README's limitations. If using mock mode, explain exactly what is mocked and the Azure access/quota reason. Do not claim live cloud verification unless it has actually succeeded.

## Questions to be ready to answer

- Why is the routine deterministic? It prevents skipped validation, missing controls and unreviewed state changes.
- Why separate conversation and persistent memory? A session reference should reset while relevant earlier claim facts survive a restart.
- Why retrieve all applicable rules? A relevance cutoff must not suppress a mandatory coverage control.
- Why retain the local sparse index when Azure File Search exists? Deterministic checks use versioned rule metadata and can be tested offline. File Search supplies additional managed retrieval for the Azure agent.
- Why use SQLite? Transactions keep result and review updates consistent for this local demonstration.
- Why no application hosting service? The assignment requires Foundry Agent Service. The Python client can remain on the laptop for the walkthrough.
- What would production add? Authentication, authorization, managed storage, data controls, formal policy interpretation, monitoring, resilience, human-review work queues and production evaluation.
