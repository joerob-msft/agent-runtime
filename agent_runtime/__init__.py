"""agent-runtime — remote agent sidecar for AI coding workflows."""

__version__ = "0.1.0"

# pip install URL — used by the wrapper for self-updates.
# Override with --register <dashboard_url> to pull wheels from a private server.
PIP_INSTALL_URL = "agent-runtime[terminal]"
