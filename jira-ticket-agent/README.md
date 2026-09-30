# AI QA Agent (Slack → Ollama → Jira)

Production-oriented MVP that listens to Slack channels, classifies QA-related messages with **Ollama** (local OpenAI-compatible chat completions), creates **Jira Cloud** issues with structured descriptions (ADF), and posts an acknowledgement back in the **Slack thread**.

## Features

- **Slack Socket Mode** (FastAPI lifespan + `AsyncSocketModeHandler`) — no public HTTP ingress for Slack required.
- **Ollama** (default `llama3.2`) with JSON-mode responses validated by Pydantic (`GeminiQAAnalysis` DTO).
- **Issue types**: bug, enhancement, regression, UI issue, crash, performance (plus `not_qa_relevant` for casual noise).
- **Thread-aware**: pulls `conversations.replies` when the message is in a thread (or has replies) for richer context.
- **Duplicate detection**: in-memory TTL cache keyed by a fingerprint (duplicate hint + summary + category + module).
- **Jira**: REST API v3, ADF description, auto labels, optional assignee map by module/platform keyword.
- **Retries**: Ollama (5xx / network) and Jira client (network timeouts) via `tenacity`.
- **Logging** and defensive error handling so Slack socket stays healthy.

## Requirements

- Python **3.12**
- **Ollama** installed and running locally — [ollama.com](https://ollama.com)
- Slack app with **Bot Token** + **Socket Mode** enabled + **`message.channels`** (and **`message.groups`** for private channels) bot scopes.
- Jira Cloud user + API token.

## Quick start

1. **Start Ollama and pull the model**

   ```bash
   ollama serve          # if not already running
   ollama pull llama3.2  # or your chosen OLLAMA_MODEL
   ```

2. **Clone / enter the project root**

   ```bash
   cd /path/to/jira_ticket_agent
   python3.12 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Configure environment**

   ```bash
   cp .env.example .env
   # Edit .env with real credentials
   ```

   | Variable | Purpose |
   |----------|---------|
   | `OLLAMA_BASE_URL` | Ollama server URL, default `http://localhost:11434` |
   | `OLLAMA_MODEL` | Model name, default `llama3.2` — run `ollama pull <model>` first |
   | `OLLAMA_API_KEY` | Optional — only for authenticated remote Ollama |
   | `SLACK_BOT_TOKEN` | `xoxb-...` |
   | `SLACK_APP_TOKEN` | `xapp-...` (Socket Mode) |
   | `SLACK_CHANNEL_IDS` | Optional comma-separated channel IDs; **empty = all channels** the bot is in |
   | `JIRA_EMAIL` | Jira login email |
   | `JIRA_API_TOKEN` | Atlassian API token |
   | `JIRA_BASE_URL` | e.g. `https://your-domain.atlassian.net` |
   | `JIRA_PROJECT_KEY` | Target project key |
   | `JIRA_ISSUE_TYPE` | Usually `Bug` |
   | `JIRA_MODULE_ASSIGNEE_MAP` | JSON map of keyword → assignee **email** (resolved to `accountId`) |

4. **Run the API + Slack listener**

   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8080
   ```

   Or load `.env` and honor **`APP_PORT`**:

   ```bash
   python run_dev.py
   ```

   **Port 8080 busy and `kill -9 <pid>` → Permission denied**

   The stuck `uvicorn` was almost certainly started from a **Cursor background/agent terminal**. Those processes get the kernel LSM label **`cursor_sandbox (enforce)`** (see `cat /proc/<pid>/attr/current`), so **your normal shell cannot send signals** to free the port.

   **What to do:** stop that job in Cursor's terminal / background-tasks UI, **or** restart Cursor, **or** run on another port until 8080 is free:

   ```bash
   APP_PORT=8081 python run_dev.py
   ```

   If you control the machine and policy allows it, `sudo kill -9 <pid>` can clear a sandboxed process when interactive `kill` cannot.

5. **Invite the bot** to your QA channel(s) and post a message. Non-QA / casual content is **silently skipped** (no Slack noise). Actionable issues get a Jira ticket and a **thread reply** with key, priority, severity, assignee, and summary.

## Slack app setup (checklist)

1. Create a Slack app → **Socket Mode** ON.
2. **OAuth scopes** (minimum starting point):  
   `channels:history`, `groups:history`, `chat:write`, `users:read`  
   (add more if your workspace policies require them).
3. Install app to workspace; copy **Bot User OAuth Token** → `SLACK_BOT_TOKEN`.
4. Generate an **App-Level Token** with `connections:write` → `SLACK_APP_TOKEN`.
5. **Event Subscriptions** → subscribe to bot event **`message.channels`** (and **`message.groups`** for private channels).

## Jira setup

- Project must allow the bot user (the Atlassian account behind `JIRA_EMAIL`) to create issues.
- **Priority** and **assignee** are best-effort: if Jira rejects `priority` or `assignee`, the client strips the offending field and retries once each.
- **Labels** are sanitized to Jira rules (alphanumeric, `-`, `_`).

## Project layout

```text
app/
  main.py              # FastAPI + lifespan (Slack socket)
  config.py            # Pydantic settings / .env
  models.py            # Slack + LLM + Jira DTOs
  prompts.py           # QA persona + prompt builders
  ai/ollama_service.py
  slack/slack_handler.py
  jira/jira_service.py
  services/bug_processor.py
  utils/logger.py
samples/
  sample_slack_messages.txt
  sample_jira_payload.json
```

## Testing aids

- `samples/sample_slack_messages.txt` — copy/paste scenarios (bugs vs casual).
- `samples/sample_jira_payload.json` — example REST body matching the agent's ADF layout.

## Operations

- **Health**: `GET /health` — returns `{"status":"ok","service":"ai-qa-agent"}`.
- **Logs**: structured plain text to stdout (`app.utils.logger.setup_logging`).
- **Scale-out**: duplicate cache is in-process memory; run a single replica per workspace or replace with Redis for horizontal scaling.

## Security

- Never commit `.env`. Use secret managers in production.
- Jira + Slack tokens are high privilege; rotate regularly.

## License

Use and modify per your organization's policy.
