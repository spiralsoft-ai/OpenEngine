"""The agents this system ships with.

Profiles are configuration, so they are values, not classes -- adding an agent
is adding an entry here, and nothing about it is special-cased anywhere else.
This lives in the runtime rather than the domain because it is a *choice* about
which agents exist; `engine.domain.agents` owns what an agent **is**.

A profile deliberately does not name the runner that executes it. Which runner
that is comes from `Capabilities.agent_runner`, chosen by the composition root,
so the same profile is a Codex agent in one process and something else in
another without being edited.
"""

from collections.abc import Mapping

from engine.domain.agents import AgentProfile
from engine.domain.ids import AgentId

FOREMAN = AgentProfile(
    agent_id=AgentId("foreman"),
    instructions=(
        "You are the foreman of a team of coding agents. Coordinate implementation "
        "work, answer questions from coders, and escalate to a human when a "
        "decision is not yours to make.\n\n"
        "You cannot yet dispatch work or author workflows -- those tools are not "
        "built. Until they are, say plainly what you would do and who would do it, "
        "rather than implying anything has been started."
    ),
    # Empty on purpose. The grants this profile wants -- "dispatch",
    # "author_workflow" -- resolve to no tool today, and `AgentSession` refuses
    # to run a profile whose grants it cannot honour rather than quietly
    # dropping them. They go in when the tools do.
    capabilities=(),
    description="Coordinates implementation work and escalates decisions.",
)

CODER = AgentProfile(
    agent_id=AgentId("coder"),
    instructions=(
        "You are a coding agent working in a checkout of this repository. Read "
        "before you answer, prefer the smallest change that solves the problem, "
        "and say what you are uncertain about rather than guessing."
    ),
    capabilities=(),
    description="Reads and reasons about the code in the working tree.",
)

PROJECT_MANAGER = AgentProfile(
    agent_id=AgentId("project-manager"),
    instructions=(
        "You are the project manager for a product an operator is building. A "
        "project holds the timeline and milestones they are working toward, and "
        "the workflow runs and chats that belong to them. Break stated goals "
        "into milestones, size them against each other, and say what should "
        "happen next and in what order.\n\n"
        "You cannot yet read, start, or schedule workflow runs -- those tools "
        "are not built. Until they are, say plainly what you would schedule and "
        "when, rather than implying anything has been started."
    ),
    # Empty for the same reason the foreman's is: the grants this profile wants
    # name tools that do not exist yet, and `AgentSession` refuses a profile
    # whose grants it cannot honour. They go in with the workflow tools.
    capabilities=(),
    description="Plans a project's timeline and the work that reaches it.",
)

#: Every profile the system knows, by id.
BUILT_IN: Mapping[AgentId, AgentProfile] = {
    profile.agent_id: profile for profile in (FOREMAN, CODER, PROJECT_MANAGER)
}


class UnknownAgentError(KeyError):
    """No profile is registered under that id."""

    def __init__(self, agent_id: AgentId, known: Mapping[AgentId, AgentProfile]) -> None:
        super().__init__(f"no agent profile {agent_id!r}; known: {sorted(known)}")
        self.agent_id = agent_id


def profile_for(agent_id: AgentId, profiles: Mapping[AgentId, AgentProfile] = BUILT_IN) -> AgentProfile:
    """Look up one profile, or say which ones exist."""
    try:
        return profiles[agent_id]
    except KeyError:
        raise UnknownAgentError(agent_id, profiles) from None


__all__ = [
    "BUILT_IN",
    "CODER",
    "FOREMAN",
    "PROJECT_MANAGER",
    "UnknownAgentError",
    "profile_for",
]
