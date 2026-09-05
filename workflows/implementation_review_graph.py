"""Implementation and review, run as a graph.

The same four stages `implementation_review.py` describes as steps, written as a
LangGraph instead:

    workspace -> implementation -> review -> human-review

Both files are workflow definitions this repository owns, and a definition is
classified by which kind it is rather than by a setting. A deployment that wants
only one of them ships only one of these files.

**This is offered, and it is new.** The web interface lists it in the WorkOrder
dropdown behind a `[BETA]` label, and creating a WorkOrder with it starts the
graph below on the graph engine. Its stages, its agents' conversations and the
questions it stops on are on the WorkOrder page, drawn from the events the run
publishes. Beta because those events are held in the server's memory, so a
restart loses the transcript of what was said before it. See
`docs/graph-workorders-beta.md`.

Three things the graph runtime can do that the step runtime cannot, which is
what makes this more than a translation:

* the checkout is a **node**, so provisioning is a position a run stands at --
  and can be reported as having failed at -- rather than something that happens
  before the run exists;
* the human decision is an **approval**, so the node that raised it keeps
  running while somebody thinks. Accepting releases it; refusing ends the run;
* an agent that stops to ask permission does so **without ending its turn**, so
  answering carries on the same conversation instead of starting a new one.

One graph per runner, because a node names the agent it runs. Picking a runner
means starting `implementation-review-codex` or `implementation-review-claude`,
rather than filling in a field on one graph.

Where the checkouts go is the deployment's business, so it is not named here --
`pipeline` takes it. See `pipeline`.
"""

from engine.adapters.workspace_provider.git_worktree import (
    DEFAULT_ROOT_DIRECTORY,
    GitWorktreeWorkspaceProvider,
)
from engine.graph_runtime_langgraph import (
    GraphWorkflow,
    State,
    agent_registry,
    graph_workflow,
)
from engine.graph_runtime_langgraph.components import (
    ACPNode,
    HumanReviewNode,
    WorkspaceNode,
    checkout,
)
from engine.ports import WorkspaceProvider
from langgraph.graph import END, START, StateGraph
from langgraph_acp import ACPAgentRegistry
from langgraph_acp.providers import ClaudeACPProvider, CodexACPProvider

#: What every checkout is based on.
BASE_REF = "origin/main"

WORKSPACE = "workspace"
IMPLEMENTATION = "implementation"
REVIEW = "review"
HUMAN_REVIEW = "human-review"

#: Codex and Claude, reached through their ACP adapters. `agent_registry` is
#: what routes an agent's permission request back to the run that raised it.
AGENTS = agent_registry([CodexACPProvider(), ClaudeACPProvider()])

IMPLEMENTATION_PROMPT = (
    "Implement the requested change in the provided workspace. Read the code "
    "before editing. The workspace is already based on the current remote main "
    "commit; do not fetch, pull, or merge main before editing. Make the "
    "smallest complete change and report the result.\n\n"
    "The task:\n{task}"
)

REVIEW_PROMPT = (
    "Review the implementation already made in the provided workspace. Read "
    "the changed code and the code around it before judging it, and check "
    "correctness, regressions the change could cause, and tests that should "
    "exist but do not. Inspect the workspace only: do not edit, revert, commit, "
    "or otherwise modify anything, and do not fix what you find. Report every "
    "finding with the file it is in and why it matters, and say so explicitly "
    "when you find nothing.\n\n"
    "Original task:\n{task}\n\n"
    "What the implementation reported:\n{implementation}"
)


def pipeline(
    runner: str,
    *,
    workspace_provider: WorkspaceProvider | None = None,
    agents: ACPAgentRegistry = AGENTS,
) -> StateGraph:
    """The four stages, with both agent nodes run by `runner`.

    The two keyword arguments are the only things a deployment or a test has
    business replacing: where the checkouts are made, and which agents answer.
    The rest -- the stages, their order, the prompts, which node is a person --
    is what makes this *the* implementation-review workflow.

    They are arguments so that a variant is a call rather than a second copy of
    this file with one line changed:

        pipeline("codex", workspace_provider=..., agents=...)

    Nothing passes either one today: a workflow file is read before any
    composition root has built anything, so what a deployment gets is the
    default below -- the same worktree root every app here is configured with.
    A composition root that needs its checkouts somewhere else calls `pipeline`
    with its own provider rather than copying this file.
    """
    builder: StateGraph = StateGraph(State)
    builder.add_node(
        WORKSPACE,
        WorkspaceNode(
            provider=workspace_provider
            or GitWorktreeWorkspaceProvider(DEFAULT_ROOT_DIRECTORY),
            base_ref=BASE_REF,
        ),
    )
    builder.add_node(
        IMPLEMENTATION,
        ACPNode(
            agent=runner,
            registry=agents,
            prompt=lambda state: IMPLEMENTATION_PROMPT.format(
                task=state.get("task", "")
            ),
            # Work in the checkout the workspace node made. Read per run, so
            # one compiled graph serves every run.
            cwd=checkout,
            output_key=IMPLEMENTATION,
            graph_node_name="Implementation",
            graph_node_description="Makes the requested change.",
        ),
    )
    builder.add_node(
        REVIEW,
        ACPNode(
            agent=runner,
            registry=agents,
            prompt=lambda state: REVIEW_PROMPT.format(
                task=state.get("task", ""),
                implementation=state.get(IMPLEMENTATION, ""),
            ),
            cwd=checkout,
            output_key=REVIEW,
            graph_node_name="Review",
            graph_node_description="Inspects the change without modifying it.",
        ),
    )
    builder.add_node(HUMAN_REVIEW, HumanReviewNode())
    builder.add_edge(START, WORKSPACE)
    builder.add_edge(WORKSPACE, IMPLEMENTATION)
    builder.add_edge(IMPLEMENTATION, REVIEW)
    builder.add_edge(REVIEW, HUMAN_REVIEW)
    builder.add_edge(HUMAN_REVIEW, END)
    return builder


#: One graph per agent. Picking a runner is picking one of these.
RUNNERS = ("codex", "claude")


def graph_for(
    runner: str,
    *,
    workspace_provider: WorkspaceProvider | None = None,
    agents: ACPAgentRegistry = AGENTS,
) -> GraphWorkflow:
    """This workflow, for one agent, named the way everything else names it.

    The id and the name are built in one place rather than at each call, so
    that the graph a deployment starts and the graph a test drives are the same
    graph under the same name. What a caller may replace is what `pipeline`
    accepts: where the checkouts go, and which agents answer.
    """
    return graph_workflow(
        pipeline(runner, workspace_provider=workspace_provider, agents=agents),
        id=f"implementation-review-{runner}",
        name=f"Implementation review ({runner})",
    )


workflow = tuple(graph_for(runner) for runner in RUNNERS)
