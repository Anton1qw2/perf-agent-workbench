# Deployment guide

## Prerequisites

Install Python 3.11+ with pip and venv support, and Git. Python 3.12 on Windows
is the validated lock-file environment. On Debian/Ubuntu, the Python venv package
may require a separate installation. Git is also needed for local source access.
Node.js 18+ is needed only for Web regression tests, not to run the application.

## Install

```text
git clone https://github.com/Anton1qw2/perf-agent-workbench.git
cd perf-agent-workbench
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Linux/macOS:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
cp .env.example .env
```

The portable installation resolves dependencies from `pyproject.toml`; it is not
identical to the Windows lock file. For a runtime-only installation, use `-e .`
instead of `-e ".[dev]"`. Keep an existing `.env` when upgrading.

## Start and verify

Run from the project root, using the same environment for both processes:

| Process | Windows PowerShell | Linux/macOS |
| --- | --- | --- |
| API and Web | `.\.venv\Scripts\python.exe -m perf_chat.api` | `.venv/bin/python -m perf_chat.api` |
| Worker | `.\.venv\Scripts\python.exe -m perf_chat.worker` | `.venv/bin/python -m perf_chat.worker` |
| Optional TUI | `.\.venv\Scripts\python.exe -m perf_chat.ui.tui` | `.venv/bin/python -m perf_chat.ui.tui` |

Open `http://127.0.0.1:8000/api/health`: expect `status=ok`, `mode=demo` and
`executor=demo`. This checks the API, not worker liveness. Open the Web root, set
Repository to `local/repo` and Branch to `main`, then describe a task. In the default
demo these are task metadata; no real repository is required. Wait for the reply,
click Run and answer the executor's question. Expect a clearly labeled demo result.
If messages remain queued, check that the worker is running with the same database.

## Addresses and storage

```dotenv
PERF_CHAT_API_HOST=127.0.0.1
PERF_CHAT_API_PORT=8000
PERF_CHAT_API_URL=http://127.0.0.1:8000
PERF_CHAT_DB=data/perf-chat.sqlite3
```

`API_HOST` and `API_PORT` configure the server listener. `API_URL` configures only
the TUI destination; it does not change the listener. Restart processes after editing
configuration. The Web client uses same-origin `/api` and SSE URLs.

Relative database and local repository paths depend on the working directory.
For services, use an explicit working directory and the same absolute database path
for API and worker. Keep SQLite on a local filesystem; do not split these processes
across hosts sharing a network filesystem. Preserve the database and its permissions
across restarts. Back up with SQLite's backup API or stop both processes before
copying the database and any WAL files.

Keep the default loopback binding for local use. For access from another machine,
prefer an SSH tunnel, for example `ssh -L 8000:127.0.0.1:8000 user@server`, then open
`http://127.0.0.1:8000` locally. Binding `0.0.0.0` exposes this unauthenticated API
on all interfaces; it is a listener address, not a browser destination.

For a managed installation, supervise API and worker as separate long-running
processes with the same configuration and writable database directory. A reverse
proxy must serve the application at the domain root, forward `/api` and static
assets, disable buffering for SSE and allow long-lived SSE connections. Provide
access control outside the application before exposing it beyond a trusted local user.

## Optional integrations

- LLM intake: set `PERF_CHAT_MODE=llm`, `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL`.
- Real profiler MCP: install `-e ".[mcp]"`, set `PERF_CHAT_EXECUTOR=profile-mcp` and
  `PROFILER_MCP_URL` on both API and worker. Save the token through Web settings.
- Encrypted tokens on Linux/macOS: install `-e ".[secrets]"` and generate a key with
  `.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
  Store it as `PERF_CHAT_SECRET_KEY` in the private `.env` or a secret manager for both
  processes. Generate it once; keep it across restarts. Do not commit it. Windows uses DPAPI;
  run API and worker under the same OS account.
- Local source: register aliases in `PERF_CHAT_GIT_REPOSITORIES`, with paths accessible
  to the worker. GitLab source uses HTTPS repository URLs and backend token storage.

See [the integration guide](integration-guide.md) for contracts and detailed limits.
Real MCP deployments and Linux/macOS runtime behavior require environment-specific validation.

## LLM connection checks

Use the [README configuration example](../README.md#connect-your-llm). Restart both
API and worker after changes; existing process environment variables take precedence
over `.env`. Inside a container, loopback refers to that container, so configure a
server hostname reachable from the backend.

Check `/api/health` for `mode=llm`, then send a task in chat and wait for an agent
reply. Health confirms configuration, not provider connectivity. If a message stays
queued, check worker liveness and the shared database. For provider HTTP 401/403,
check authorization; for 404, check the base URL and model ID; for 400, check JSON
mode support and the provider's request requirements. Provider response details are
not exposed in application error messages; diagnose against the provider's own tools
without logging credentials. Real providers need deployment-specific validation.

## Browser request IDs

`crypto.randomUUID()` is limited to secure contexts and may be unavailable when
opening the Web UI through a remote HTTP address. The client falls back to UUID v4
IDs generated with `crypto.getRandomValues()`, preserving request deduplication.
A browser with neither API displays a Web Crypto compatibility error. After updating
the Web client, hard-refresh the page to replace a cached script.
