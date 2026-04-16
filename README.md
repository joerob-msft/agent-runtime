# agent-runtime

A lightweight agent runtime that turns any machine into an AI-agent-ready compute target. Zero dependencies. Self-updating. Works with any ACP-compatible coding agent.

## What is this?

**agent-runtime** is a sidecar you install on any machine (Dev Box, VM, cloud instance) to make it remotely controllable by AI coding agents. It provides:

- **Async command server** — `POST /exec` → job ID, `GET /jobs/{id}` → poll for results
- **WebSocket terminal** — interactive ConPTY sessions over WebSocket (xterm.js compatible)
- **ACP client** — structured Agent Client Protocol communication with any ACP agent
- **Self-updating** — `POST /update` triggers graceful upgrade via pip
- **Devtunnel integration** — persistent tunnels, token rotation, challenge-code registration
- **API key auth** — auto-generated keys, persisted across restarts

## Install

```bash
# Core (zero dependencies)
pip install devpilot-agent

# With interactive terminal support (Windows)
pip install devpilot-agent[terminal]
```

## Quick Start

```bash
# Start the server
agent-server

# Start on a custom port
agent-server --port 9090

# Start with devtunnel + auto-registration
agent-server --wrapper --tunnel --register https://your-dashboard.example.com

# Start with self-update wrapper
agent-server --wrapper
```

## API reference

### Authentication

- `GET /health` works without an API key, but unauthenticated callers only get a minimal response:
  `{"status":"ok","authenticated":false}`
- All other endpoints require `X-API-Key`.
- With a valid API key, `GET /health` returns the full server capabilities payload.

### `POST /exec`

Submit a shell or ACP job. Successful requests return `202 Accepted` with:

```json
{"jobId":"abc123","status":"pending"}
```

Common request fields:

| Field | Type | Required | Notes |
|---|---|---:|---|
| `mode` | string | no | `"shell"` by default; set to `"acp"` for ACP mode |
| `workdir` | string | no | Working directory for the child process |
| `timeout` | integer | no | Timeout in seconds; defaults to `300` |
| `env` | object | no | Optional environment overlay. Keys and values must both be strings or the request fails with `400`. |

#### Shell mode

Shell mode runs the `command` in a background subprocess.

| Field | Type | Required | Notes |
|---|---|---:|---|
| `command` | string | yes | Shell command to execute |

Example:

```bash
curl -X POST http://localhost:8585/exec \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "command": "python -c \"import os; print(os.environ.get(\\\"DEVPILOT_TEST_ENV\\\", \\\"\\\"))\"",
    "workdir": "C:/src/repo",
    "timeout": 300,
    "env": {
      "DEVPILOT_TEST_ENV": "hello-from-env"
    }
  }'
```

The `env` overlay is merged onto the runtime process environment before the shell subprocess starts.

#### ACP mode

ACP mode launches an ACP-compatible agent and returns structured execution results.

| Field | Type | Required | Notes |
|---|---|---:|---|
| `agent` | string | no | ACP launcher command; defaults to `copilot --acp --stdio` |
| `prompt` | string | no | Prompt text to send after session creation/load |
| `acp_session_id` | string | no | Resume an existing ACP session |
| `model` | string | no | ACP config option |
| `effort` | string | no | ACP reasoning effort |

Example:

```bash
curl -X POST http://localhost:8585/exec \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "mode": "acp",
    "agent": "copilot --acp --stdio",
    "prompt": "Implement the login feature",
    "workdir": "C:/src/repo",
    "env": {
      "DOTNET_ROOT": "C:/Users/alice/AppData/Local/Microsoft/dotnet"
    }
  }'
```

In ACP mode, the `env` overlay is applied to:

- the ACP agent subprocess itself
- any terminal subprocesses created through ACP `terminal/create`

### `GET /jobs/{id}`

Poll a single job by ID.

Common response fields:

| Field | Type | Notes |
|---|---|---|
| `jobId` | string | Job identifier |
| `status` | string | `pending`, `running`, `completed`, `failed`, or `timeout` |
| `command` | string | Original shell command or `[acp] ...` launcher |
| `submittedAt` | number | Unix timestamp |
| `lastOutputAt` | number | Updated when output arrives |
| `processAlive` | boolean | Whether the tracked subprocess is still running |

Shell jobs add fields such as `stdout`, `stderr`, `exitCode`, `durationMs`, and `timedOut`.

ACP jobs add fields such as:

- `acpSessionId`
- `stopReason`
- `events`
- `acpError` (when ACP execution fails)

Example:

```bash
curl http://localhost:8585/jobs/abc123 -H "X-API-Key: $KEY"
```

### `GET /jobs`

Returns the most recent jobs (up to 20), newest first. Each item is the same sanitized shape returned by `GET /jobs/{id}`.

### `POST /jobs/{id}/nudge`

Writes a newline to the running job's stdin. This is mainly useful for shell commands that are waiting for input or need output to flush.

Example:

```bash
curl -X POST http://localhost:8585/jobs/abc123/nudge -H "X-API-Key: $KEY"
```

Response shape:

```json
{"nudged":true,"processAlive":true}
```

If the process is already finished or no stdin is available, the endpoint returns `nudged: false` with a reason.

### `GET /health`

Unauthenticated example:

```bash
curl http://localhost:8585/health
```

```json
{"status":"ok","authenticated":false}
```

Authenticated example:

```bash
curl http://localhost:8585/health -H "X-API-Key: $KEY"
```

```json
{
  "status": "ok",
  "version": "0.2.2",
  "hostname": "devbox-01",
  "jobs": 0,
  "cwd": "C:\\\\src\\\\agent-runtime",
  "authenticated": true,
  "terminal_supported": true,
  "terminal_sessions": 0,
  "acp_supported": true,
  "execution_modes": ["shell", "acp"]
}
```

### `POST /update`

Triggers a graceful restart for self-update. The server returns `409` instead when jobs are still running or terminal sessions are active.

```bash
curl -X POST http://localhost:8585/update -H "X-API-Key: $KEY"
```

## ACP Client (Standalone)

The headless ACP client can be used independently for driving any ACP agent from Python:

```python
from agent_runtime.acp_client import run_acp_session_sync, HeadlessApprovePolicy

result = run_acp_session_sync(
    agent_cmd=["copilot", "--acp", "--stdio"],
    prompt="Refactor the auth module",
    workdir="/path/to/repo",
    timeout=600,
    env={"DOTNET_ROOT": "/home/alice/.dotnet"},
    permission_policy=HeadlessApprovePolicy("/path/to/repo"),
)

print(result.output_text)       # Agent's response
print(result.session_id)        # For session continuity
print(result.tool_calls)        # Structured tool call data
print(result.stop_reason)       # "end_turn", "timeout", "error"
```

`env` is optional and overlays string key/value pairs onto the current process environment for the ACP agent subprocess and ACP-created terminal subprocesses.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Your Orchestrator / Dashboard / CI                  │
│  (any HTTP client)                                   │
└────────┬─────────────────────┬──────────────────────┘
         │ HTTP                 │ WebSocket
         ▼                     ▼
┌─────────────────────────────────────────────────────┐
│  agent-runtime (on the target machine)               │
│                                                      │
│  ┌───────────────────┐  ┌─────────────────────────┐ │
│  │ Command Server     │  │ Terminal Server          │ │
│  │ :8585              │  │ :8586 (WebSocket)        │ │
│  │                    │  │                          │ │
│  │ POST /exec         │  │ ConPTY ↔ xterm.js       │ │
│  │ GET /jobs/{id}     │  │ JSON input/raw output    │ │
│  │ POST /nudge        │  │ Resize, idle timeout     │ │
│  │ POST /update       │  │ Max 2 sessions           │ │
│  └───────────────────┘  └─────────────────────────┘ │
│                                                      │
│  ┌─────────────────────────────────────────────────┐ │
│  │ ACP Client (headless)                            │ │
│  │ JSON-RPC over stdio → any ACP agent              │ │
│  │ Permission policies, session continuity          │ │
│  └─────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────┘
```

## Key Design Decisions

- **Zero runtime dependencies** — stdlib-only Python. Terminal extras are opt-in.
- **Async job pattern** — fire-and-forget commands avoid tunnel timeout issues.
- **Self-updating** — exit code 42 triggers pip upgrade + restart via the wrapper loop.
- **Security** — auto-generated API keys, workdir-scoped permission policies, path traversal protection.

## Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `agent_runtime_API_KEY` | auto-generated | API key for authenticating requests |
| `DEVPILOT_TUNNEL_URL` | — | Devtunnel URL (set automatically with `--tunnel`) |
| `DEVPILOT_TUNNEL_TOKEN` | — | Devtunnel access token |

## License

Apache 2.0 — see [LICENSE](LICENSE) for details.

## Releasing

Releases are automated via GitHub Actions. The checked-in publish workflow runs when a version tag is pushed, and the normal flow is:

1. Bump `agent_runtime/__init__.py`
2. Run tests and build locally
3. Commit and push `main`
4. Fast-forward and push the `release` branch
5. Push `vX.Y.Z` to trigger PyPI publish

Example for `0.2.2`:

```bash
# 1. Bump version in agent_runtime/__init__.py
#    __version__ = "0.2.2"

# 2. Validate the release locally
python -m pytest tests/ -q
python -m build

# 3. Commit and push main
git checkout main
git add agent_runtime/__init__.py README.md agent_runtime/ tests/
git commit -m "Release 0.2.2"
git push origin main

# 4. Fast-forward the shared release branch
git checkout release
git merge --ff-only main
git push origin release

# 5. Push the version tag — this triggers .github/workflows/publish.yml
git checkout main
git tag v0.2.2
git push origin refs/tags/v0.2.2
```

The publish workflow runs:

1. Windows tests
2. Source distribution + wheel build
3. PyPI publish via Trusted Publisher

If Trusted Publisher is unavailable, the fallback is:

```bash
python -m build
python -m twine upload dist/devpilot_agent-0.2.2*
```

Use a PyPI API token for `twine upload`. The preferred path is still the tag-triggered GitHub Actions publish.

**First-time setup** (one-time on pypi.org):
1. Go to `pypi.org/manage/project/devpilot-agent/settings/publishing/`
2. Add trusted publisher: owner=`joerob-msft`, repo=`agent-runtime`, workflow=`publish.yml`, environment=`pypi`
3. Create a `pypi` environment in GitHub repo settings (Settings → Environments → New)
