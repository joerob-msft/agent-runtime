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

## API

### Command Execution

```bash
# Submit a command
curl -X POST http://localhost:8585/exec \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{"command": "echo hello", "workdir": "/path/to/repo", "timeout": 300}'
# → {"jobId": "abc123", "status": "pending"}

# Poll for results
curl http://localhost:8585/jobs/abc123 -H "X-API-Key: $KEY"
# → {"status": "completed", "exitCode": 0, "stdout": "hello\n", ...}

# Nudge a stalled process (writes to stdin)
curl -X POST http://localhost:8585/jobs/abc123/nudge -H "X-API-Key: $KEY"
```

### ACP Mode

Submit commands in ACP mode for structured output from any ACP-compatible agent:

```bash
curl -X POST http://localhost:8585/exec \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "mode": "acp",
    "agent": "copilot --acp --stdio",
    "prompt": "Implement the login feature",
    "workdir": "/path/to/repo"
  }'
```

ACP responses include structured events, tool calls, and session IDs instead of raw stdout.

### Health Check

```bash
curl http://localhost:8585/health
# → {"status": "ok", "version": "0.1.0", "acp_supported": true, ...}
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
    permission_policy=HeadlessApprovePolicy("/path/to/repo"),
)

print(result.output_text)       # Agent's response
print(result.session_id)        # For session continuity
print(result.tool_calls)        # Structured tool call data
print(result.stop_reason)       # "end_turn", "timeout", "error"
```

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
| `DEVPILOT_AGENT_API_KEY` | auto-generated | API key for authenticating requests |
| `DEVPILOT_TUNNEL_URL` | — | Devtunnel URL (set automatically with `--tunnel`) |
| `DEVPILOT_TUNNEL_TOKEN` | — | Devtunnel access token |

## License

Apache 2.0 — see [LICENSE](LICENSE) for details.
