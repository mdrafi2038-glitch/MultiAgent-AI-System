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
- Plugin hub: built-in GitHub integration plus user-defined HTTPS API plugins for services that expose an API.
- Custom API plugins support named GET/POST/PUT/PATCH/DELETE actions, optional bearer credentials sourced only from hosting environment variables, persistent plugin definitions, and explicit per-call approval for all write actions.
- Custom plugin URLs are HTTPS-only; local/private network targets and redirects are blocked. Credentials are never stored in the plugin database or exposed in dashboard responses.
- Custom API actions are configured as an explicit allowlist. The current release does not automatically import arbitrary OpenAPI documents or automate websites that have no supported API.
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
- Custom plugin credentials: create environment variables using the exact names entered in the plugin setup form (for example `MY_SERVICE_API_TOKEN`); credentials are not entered into chat or saved in the plugin database.

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


## Universal API Plugin Hub
1. Open Dashboard → Plugins → Universal API Plugin Hub.
2. Add a plugin ID, display name, HTTPS API base URL, and optional credential environment-variable name.
3. Define the permitted actions as a JSON array. Each action has a unique `id`, HTTP `method`, relative `path`, and optional `description`. Example:
   ```json
   [
     {"id":"list_items","method":"GET","path":"/v1/items","description":"List items"},
     {"id":"create_item","method":"POST","path":"/v1/items","description":"Create an item"}
   ]
   ```
4. If authentication is needed, add the named environment variable and its secret value in the Render service Environment settings. The dashboard stores only the variable name, never the secret.
5. Use the action buttons to call an API. GET requests are read-only; every other method prompts for a separate confirmation before execution.

The connector supports services with compatible HTTPS APIs. It does not guarantee access to every website, does not bypass login/anti-bot controls, and does not yet auto-import OpenAPI specifications or perform browser automation. A website without a supported API needs a separate, explicitly designed integration.


## Optional AI capability suite

The AI Tools dashboard exposes optional adapters. Each provider is independent; configure only the services you use.

| Feature | Environment variables | Notes |
|---|---|---|
| Web search | `TAVILY_API_KEY` | Uses Tavily Search API and returns source URLs. |
| Image generation | `IMAGE_API_KEY`, `IMAGE_MODEL`, optional `IMAGE_API_BASE_URL` | OpenAI-compatible Images API. |
| Video generation | `VIDEO_API_URL`, `VIDEO_API_KEY` | Generic HTTPS submit adapter; task response/status format depends on provider. |
| File analysis | None for extraction; model key for AI summary | TXT, MD, CSV, JSON, PDF, DOCX and common image formats; max 10 MB. Images require a vision-capable configured model. |
| Browser automation | `BROWSERLESS_URL`, `BROWSERLESS_TOKEN`, `BROWSER_ALLOWED_HOSTS` | Browserless-compatible remote browser. Only allowlisted public domains; mutating actions need approval. |
| Render connector | `RENDER_API_KEY`, `RENDER_SERVICE_ID` | Reads service/deploy status. Deploy endpoint requires explicit approval. |
| GitHub connector | `GITHUB_TOKEN` | Existing Actions integration. |
| Custom connectors | Plugin Hub | Allowlisted HTTPS API actions; write actions require approval. |

### Provider setup

1. Open the Render service Environment page.
2. Add only the environment variables for providers you choose. Never paste secrets into chat or commit them to Git.
3. For image generation, use an OpenAI-compatible provider that implements `POST /images/generations`.
4. For video generation, set the exact HTTPS submit endpoint and bearer token supplied by the chosen video provider. The adapter returns the provider's raw job response; provider-specific polling/download support may need an adapter.
5. For browser automation, configure the remote browser endpoint and a comma-separated allowlist such as `example.com,docs.example.org`. Do not allowlist domains you do not control or trust.

All capability status responses reveal only whether a credential is configured, never its value. Browser navigation is limited to allowlisted public hosts. External writes and deployments require an explicit approval flag in addition to dashboard authentication.
