# MultiAgent AI System

A separate multi-agent assistant with a Main Orchestrator, specialist agents, persistent task history, and a QA reviewer.

## Current capabilities
- Main orchestrator creates a task plan.
- Specialist agents: Research, Writer, Coding, and Data.
- QA Reviewer consolidates and checks agent outputs.
- SQLite task and agent-message history.
- Responsive web dashboard and JSON API.
- OpenAI-compatible model endpoint (OpenRouter by default).

## Safety and current scope
This first version is text-only. Agents can plan and draft work but do not control a phone/browser, publish content, access private accounts, spend money, or modify external systems. Add explicit user approval and narrowly scoped integrations before enabling side effects.

## Run locally
1. Python 3.11+ recommended.
2. Create and activate a virtual environment with Python's venv module.
3. Install dependencies with: pip install -r requirements.txt
4. Set OPENAI_API_KEY in your environment.
5. Start with: uvicorn app.main:app --reload
6. Open http://127.0.0.1:8000

## Environment variables
- OPENAI_API_KEY (required)
- OPENAI_BASE_URL (default https://openrouter.ai/api/v1)
- AI_MODEL (default openrouter/auto)
- APP_DB_PATH (default ./data/multiagent.db)

Never commit API keys. Configure secrets in the hosting provider's environment settings.

## Render
A render.yaml Blueprint is included. Configure OPENAI_API_KEY in Render before the service can run AI tasks. SQLite on an ephemeral filesystem may reset on redeploy; attach persistent storage or migrate to a managed database for durable hosted history.
