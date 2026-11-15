# MultiAgent AI System

A separate multi-agent assistant with a Main Orchestrator, specialist agents, task history, and a QA reviewer.

## Current capabilities
- Main orchestrator creates a task plan and delegates work.
- Specialist agents: Research, Writer, Coding, and Data.
- Independent specialists execute concurrently; the dashboard highlights every currently active agent.
- Cross-agent collaboration handoff compares specialist findings, dependencies, contradictions, and gaps before QA.
- QA Reviewer consolidates and checks agent outputs.
- Transient AI provider/network errors retry automatically up to three attempts with backoff.
- Tasks interrupted by a server restart are marked failed and can be retried from the dashboard.
- Task and agent-message history.
- SQLite for local development; PostgreSQL/Supabase for durable hosted history.
- Responsive web dashboard and JSON API.
- OpenAI-compatible model endpoint (OpenRouter by default).
- GitHub plugin: list repository workflows, trigger explicitly approved `workflow_dispatch` runs from the dashboard, monitor build status, and download workflow artifacts.
- The orchestrator can also dispatch an Android/APK workflow when the user explicitly requests an APK build and supplies a GitHub repository; it asks for missing repository/workflow details first.
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
- `AI_MODEL` (Render configuration uses `openrouter/free`)
- `APP_PASSWORD` for dashboard/API authentication
- `DATABASE_URL` optional PostgreSQL connection string
- `APP_DB_PATH` SQLite fallback path (default `./data/multiagent.db`)
- `GITHUB_TOKEN` optional GitHub fine-grained token with Contents: read and Actions: read/write on the target repositories

Never commit API keys, passwords, or database connection strings.

## Run locally
1. Python 3.11+ recommended.
2. Create and activate a virtual environment.
3. Install dependencies: `pip install -r requirements.txt`
4. Set `OPENROUTER_API_KEY` and `APP_PASSWORD` in your environment.
5. Optionally set `DATABASE_URL` to a PostgreSQL connection string.
6. Start: `uvicorn app.main:app --reload`
7. Open `http://127.0.0.1:8000`

## GitHub Plugin setup
1. Create a fine-grained GitHub token limited to the repositories you want the system to access.
2. Grant repository Contents: Read and Actions: Read and write permissions.
3. Add it as `GITHUB_TOKEN` in the Render service Environment settings; never commit it or paste it into chat.
4. In the dashboard open Plugins → GitHub, enter `owner/repository`, load workflows, choose one with `workflow_dispatch`, and start the build.
5. The target Android repository must already contain an Android build workflow that uploads the APK as a GitHub Actions artifact. The plugin can trigger and retrieve artifacts; it does not invent or add an Android Gradle workflow to an unrelated repository.

## Safety and current scope
GitHub workflows are external actions and only start after the user explicitly presses Start GitHub build. The plugin is restricted to GitHub's API and does not run arbitrary shell commands. Other agent work remains text-based unless a dedicated integration is connected.
