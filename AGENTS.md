# Project guidance

This project was built with the microsoft-foundry skill. Before working on or answering questions about foundry agents, read the microsoft-foundry skill first.

This assignment uses the classic Agent Service API with azure-ai-projects 1.0.0 and azure-ai-agents 1.1.0. Do not silently migrate to Projects v2 / Responses APIs. Keep mock mode explicitly labelled and never fall back silently from Azure.

Use only synthetic data. Keep domain decisions deterministic, retrieve rules from knowledge files, enforce review gates in code, and test persistent memory and failure paths. Never execute a payout. Do not commit .env, runtime memory, credentials, or generated logs.
