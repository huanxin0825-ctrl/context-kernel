from __future__ import annotations

from .chat_extensions import extension_summary
from .memory import MemoryStore
from .storage import Workspace


def workspace_state_summary(workspace: Workspace) -> str:
    skills = len(list(workspace.skills_dir.glob("*.json"))) if workspace.skills_dir.exists() else 0
    runs = len(list(workspace.agent_runs_dir.glob("*.json"))) if workspace.agent_runs_dir.exists() else 0
    project = 1 if workspace.project_file.exists() else 0
    extensions = extension_summary(workspace)
    try:
        memories = len(MemoryStore(workspace).all())
    except Exception:
        memories = 0
    return (
        f"{skills} skills, {extensions['mcp_enabled']}/{extensions['mcp_total']} mcp, "
        f"{memories} memories, {runs} runs, {project} project profiles"
    )
