# MultiAgent AI System

A separate multi-agent assistant with a Main Orchestrator, specialist agents, task history, and a QA reviewer.

## Current capabilities
- Main orchestrator creates a task plan.
- Specialist agents: Research, Writer, Coding, and Data.
- QA Reviewer consolidates and checks agent outputs.
- Task and agent-message history.
- SQLite for local development; PostgreSQL for durable hosted history.
- Responsive web dashboard and JSON API.
- OpenAI-compatible model endpoint (OpenRouter by default).
- Password-protected dashboard and task APIs; fails closed until APP_PASSWORD is configured.

## Database configuration
- If `DATABASE_URL` is set, the app uses PostgreSQL (including Supabase Postgres).
- If `DATABASE_URL` is empty, the app uses SQLite at `APP_DB_PATH`.
- Tables and indexes are initialized automatically at startup.
- For Render, use a Supabase **Shared Pooler / Session mode** connection string (port 5432). Render's IPv4 networking cannot rely on Supabase's IPv6-only direct endpoint on the free project tier.
- Keep the connection string private and add it only in Render Environment as `DATABASE_URL`. SSL is required by the app.

## Environment variables
- `OPENROUTER_API_KEY` (or `OPENAI_API_KEY`) for model requests
- `OPENAI_BASE_URL` (default `https://openrouter.ai/api/v1`)
- `AI_MODEL` (default `openrouter/auto`)
- `APP_PASSWORD` for dashboard/API authentication
- `DATABASE_URL` optional PostgreSQL connection string
- `APP_DB_PATH` SQLite fallback path (default `./data/multiagent.db`)

Never commit API keys, passwords, or database connection strings.

## Run locally
1. Python 3.11+ recommended.
2. Create and activate a virtual environment.
3. Install dependencies: `pip install -r requirements.txt`
4. Set `OPENROUTER_API_KEY` and `APP_PASSWORD` in your environment.
5. Optionally set `DATABASE_URL` to a PostgreSQL connection string.
6. Start: `uvicorn app.main:app --reload`
7. Open `http://127.0.0.1:8000`

## Safety and current scope
This version is text-only. Agents can plan and draft work but do not control a phone/browser, publish content, access private accounts, spend money, or modify external systems. Add explicit user approval and narrowly scoped integrations before enabling side effects.
