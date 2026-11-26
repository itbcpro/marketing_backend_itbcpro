"""Read tools every workspace has: memory search and delegation."""

from pydantic import BaseModel, Field

from agents import runtime
from agents.models import AgentRun
from agents.tools import ToolContext, read_tool, registry
from core.errors import DomainError
from memory import services as memory


class MemorySearchInput(BaseModel):
    query: str = Field(description="What to look up in the company's learned rules and preferences")


def _memory_search(ctx: ToolContext, args: MemorySearchInput) -> str:
    hits = memory.retrieve(
        workspace_id=ctx.workspace_id, query=args.query, agent=ctx.agent_name, limit=6
    )
    return memory.render_memories(hits)


class DelegateInput(BaseModel):
    agent: str = Field(description="Name of the specialist agent to hand the sub-goal to")
    goal: str = Field(description="A complete, self-contained description of the sub-goal")


def _delegate(ctx: ToolContext, args: DelegateInput) -> str:
    if AgentRun.objects.filter(parent=ctx.run).count() >= runtime.MAX_CHILDREN:
        raise DomainError("too many delegations in one run")
    child = runtime.start_run(
        workspace_id=ctx.workspace_id,
        agent_name=args.agent,
        goal=args.goal,
        requested_by=f"agent:{ctx.agent_name}",
        parent=ctx.run,
        dispatch=False,
    )
    finished = runtime.run_agent(child.pk, service=ctx.service, tools=ctx.registry)
    ctx.run.refresh_from_db(fields=["tainted"])
    return (
        f"Status: {finished.status}. "
        f"Result: {(finished.result or {}).get('text', '')} "
        f"Pending approvals: {(finished.result or {}).get('pending_actions', [])}"
    )


def register_standard_tools() -> None:
    registry.register(
        read_tool(
            "memory_search",
            "Search the company's learned rules, preferences and facts.",
            MemorySearchInput,
            _memory_search,
        )
    )
    registry.register(
        read_tool(
            "delegate",
            "Hand a self-contained sub-goal to a specialist agent and wait for its summary.",
            DelegateInput,
            _delegate,
        )
    )


register_standard_tools()
