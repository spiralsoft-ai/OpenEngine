"""The runtime: binds the pure engine to concrete capabilities.

Depends on `engine.ports` (and through it `engine.domain`), never on a specific
adapter. Adapters depend on the runtime, not the other way around.
"""

from engine.runtime.approval_policy import PolicyDecision, policy_decision_for
from engine.runtime.approvals import (
    ApprovalBroker,
    ApprovalDecisionNotAllowedError,
    ApprovalError,
    ApprovalNotPendingError,
    ApprovalPresenter,
    ApprovalsUnsupportedError,
    UnknownApprovalError,
    UserInputNotAllowedError,
)
from engine.runtime.capabilities import Capabilities
from engine.runtime.config import (
    CONFIG_ENVIRONMENT_VARIABLE,
    ApprovalCapability,
    ApprovalConfig,
    BashApprovalConfig,
    ClaudeConfig,
    CommunicationsConfig,
    EngineConfig,
    EngineConfigError,
    LoadedEngineConfig,
    ResponseStyle,
    WorkflowsConfig,
    describe_loaded_config,
    load_engine_config,
    parse_engine_config,
)
from engine.runtime.config import WorkOrdersConfig
from engine.runtime.dispatcher import Dispatcher, UnhandledCommandError
from engine.runtime.notifications import RunNotifier
from engine.runtime.profiles import (
    BUILT_IN,
    CODER,
    FOREMAN,
    GRANTED_TOOLS_NOTE,
    PLANNER,
    UnknownAgentError,
    profile_for,
    with_granted_tools,
)
from engine.runtime.protocol_diagnostics import (
    AGENT_PROTOCOL_DIAGNOSTIC_LOG,
    AgentProtocolDiagnostics,
    interaction_rejection_message,
)
from engine.runtime.planning_tools import (
    PLANNING_TOOL_NAMES,
    PlanningMcpBroker,
    PlanningTools,
    ProjectPlan,
    project_chat_capabilities,
)
from engine.runtime.run_read_model import RunReader, WorkflowRunView
from engine.runtime.session import (
    DEFAULT_RUNNER,
    INTERRUPTED_TOOL_RESULT,
    INTERRUPTED_TURN_NOTE,
    AgentSession,
    UnknownInstanceError,
    UnknownRunnerError,
    UnknownToolGrantError,
    WorkspacesUnavailableError,
)
from engine.runtime.session_grants import (
    matching_grant,
    normalized_scope,
    session_grant_from,
)
from engine.runtime.step_results import (
    INVALID_COMPLETION_ERROR,
    InvalidStepResultError,
    complete_step_tool,
    fail_step_tool,
    run_failed_from_tool_call,
    requests_clarification_or_escalation,
    step_completed_from_tool_call,
    run_failed_from_arguments,
    step_completed_from_arguments,
    step_result_instructions,
    step_result_from_tool_call,
)
from engine.runtime.terminal_mcp import (
    REPOSITORY_TOOL_NAMES,
    TerminalMcpBroker,
    TerminalResultAlreadySubmittedError,
    TerminalResultRegistry,
    terminal_tool_names,
)
from engine.runtime.workflow_execution import (
    WorkflowExecutionError,
    WorkflowExecutor,
    resolve_default_branch,
)
from engine.runtime.workflows import (
    WorkflowCatalog,
    WorkflowLoadError,
    load_workflow_catalog,
)

__all__ = [
    "GRANTED_TOOLS_NOTE",
    "INTERRUPTED_TOOL_RESULT",
    "INTERRUPTED_TURN_NOTE",
    "INVALID_COMPLETION_ERROR",
    "BUILT_IN",
    "CODER",
    "CONFIG_ENVIRONMENT_VARIABLE",
    "DEFAULT_RUNNER",
    "FOREMAN",
    "PLANNER",
    "AgentSession",
    "AgentProtocolDiagnostics",
    "AGENT_PROTOCOL_DIAGNOSTIC_LOG",
    "ApprovalBroker",
    "ApprovalDecisionNotAllowedError",
    "ApprovalError",
    "ApprovalNotPendingError",
    "ApprovalPresenter",
    "ApprovalsUnsupportedError",
    "ApprovalCapability",
    "ApprovalConfig",
    "BashApprovalConfig",
    "Capabilities",
    "ClaudeConfig",
    "Dispatcher",
    "CommunicationsConfig",
    "EngineConfig",
    "EngineConfigError",
    "InvalidStepResultError",
    "LoadedEngineConfig",
    "ResponseStyle",
    "WorkflowsConfig",
    "PolicyDecision",
    "PLANNING_TOOL_NAMES",
    "PlanningMcpBroker",
    "PlanningTools",
    "ProjectPlan",
    "project_chat_capabilities",
    "RunNotifier",
    "RunReader",
    "TerminalMcpBroker",
    "WorkOrdersConfig",
    "TerminalResultAlreadySubmittedError",
    "TerminalResultRegistry",
    "UnhandledCommandError",
    "UnknownAgentError",
    "UnknownApprovalError",
    "UserInputNotAllowedError",
    "UnknownInstanceError",
    "UnknownRunnerError",
    "UnknownToolGrantError",
    "WorkspacesUnavailableError",
    "WorkflowRunView",
    "WorkflowExecutionError",
    "WorkflowExecutor",
    "resolve_default_branch",
    "WorkflowCatalog",
    "WorkflowLoadError",
    "complete_step_tool",
    "describe_loaded_config",
    "fail_step_tool",
    "matching_grant",
    "normalized_scope",
    "policy_decision_for",
    "profile_for",
    "load_engine_config",
    "load_workflow_catalog",
    "interaction_rejection_message",
    "parse_engine_config",
    "session_grant_from",
    "run_failed_from_tool_call",
    "requests_clarification_or_escalation",
    "step_completed_from_tool_call",
    "run_failed_from_arguments",
    "step_completed_from_arguments",
    "step_result_instructions",
    "step_result_from_tool_call",
    "REPOSITORY_TOOL_NAMES",
    "terminal_tool_names",
    "with_granted_tools",
]
