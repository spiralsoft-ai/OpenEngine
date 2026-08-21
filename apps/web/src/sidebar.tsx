/** The rail: Projects, Workflows, Chats, in that order, with one open.
 *
 *  All three headers stay on screen at all times and the open one takes the
 *  space that is left, so choosing a section slides the headers above it up
 *  and the ones below it down rather than swapping one rail for another. The
 *  open header is printed in white and the closed ones in the rail's muted
 *  ink, which is the whole of the selected state. */

import {
  ThreadListItemPrimitive,
  ThreadListPrimitive,
  useAuiState,
} from "@assistant-ui/react";
import { useState, type ReactNode } from "react";

import type { ApiWorkflowRun } from "./api";
import { RailBrand, RailFoot } from "./brand";
import { conversationCount, IN_PROGRESS_PHASES, runStatusLabel } from "./runs";

export type RailSection = "projects" | "workflows" | "chats";

function Section({
  id,
  title,
  open,
  onOpen,
  children,
}: {
  id: RailSection;
  title: string;
  open: boolean;
  onOpen: (section: RailSection) => void;
  children: ReactNode;
}) {
  return (
    <section className="rail-section" data-open={open || undefined}>
      <button
        aria-controls={`rail-${id}`}
        aria-expanded={open}
        className="rail-head"
        onClick={() => onOpen(id)}
        type="button"
      >
        {title}
      </button>
      {/* A closed section is laid out at zero height rather than unmounted, so
          the slide has something to move; `inert` is what keeps the Tab key and
          screen readers out of the part of it that is off screen. */}
      <div className="rail-section-body" id={`rail-${id}`} inert={!open}>
        {children}
      </div>
    </section>
  );
}

function ThreadItemMeta() {
  const custom = useAuiState((state) => state.threadListItem.custom) as
    | { agentId?: string; runner?: string; workspaceRoot?: string }
    | undefined;
  const isRunning = useAuiState((state) => state.threadListItem.isRunning);
  return (
    <span className="rail-item-meta">
      {isRunning && <span className="rail-live" aria-label="Agent is running" />}
      {[custom?.agentId, custom?.runner].filter(Boolean).join(" · ")}
    </span>
  );
}

/** One chat in the rail.
 *
 *  `linked` is for the rails beside a workflow page, where switching the
 *  runtime's conversation would change nothing anybody can see: there, a chat
 *  is a link to its own page instead. */
function ThreadListItem({
  archived = false,
  linked = false,
}: {
  archived?: boolean;
  linked?: boolean;
}) {
  const remoteId = useAuiState((state) => state.threadListItem.remoteId);
  const copy = (
    <>
      <span className="rail-item-title" data-clamp="">
        <ThreadListItemPrimitive.Title fallback="New chat" />
      </span>
      <ThreadItemMeta />
    </>
  );
  return (
    <ThreadListItemPrimitive.Root className="rail-item">
      {archived ? (
        <div className="rail-item-trigger">{copy}</div>
      ) : linked && remoteId ? (
        <a
          className="rail-item-trigger"
          href={`/conversations/${encodeURIComponent(remoteId)}`}
        >
          {copy}
        </a>
      ) : (
        <ThreadListItemPrimitive.Trigger className="rail-item-trigger">
          {copy}
        </ThreadListItemPrimitive.Trigger>
      )}
      {archived ? (
        <ThreadListItemPrimitive.Unarchive
          className="rail-item-action"
          aria-label="Restore chat"
          title="Restore chat"
        >
          Restore
        </ThreadListItemPrimitive.Unarchive>
      ) : (
        <ThreadListItemPrimitive.Archive
          className="rail-item-action"
          aria-label="Archive chat"
          title="Archive chat"
        >
          ×
        </ThreadListItemPrimitive.Archive>
      )}
    </ThreadListItemPrimitive.Root>
  );
}

export function Sidebar({
  runs,
  initialSection,
  linkChats = false,
  activeRunId,
  activeConversationUrl,
  activeView,
}: {
  runs: ApiWorkflowRun[];
  /** Which section the page on screen belongs to. */
  initialSection: RailSection;
  linkChats?: boolean;
  activeRunId?: string;
  activeConversationUrl?: string;
  activeView?: "runs" | "new";
}) {
  const [open, setOpen] = useState<RailSection>(initialSection);
  return (
    <aside className="rail">
      <RailBrand href="/" />
      <div className="rail-sections">
        <Section id="projects" title="Projects" open={open === "projects"} onOpen={setOpen}>
          {/* A plan is a conversation like any other, so this is the new chat
              page reached by a link rather than a rail control of its own --
              what the link settles is which agent answers there. */}
          <div className="rail-nav">
            <a className="rail-button rail-button-primary" href="/plan">
              Plan
            </a>
          </div>
          <div className="rail-note">
            <p>Projects are in progress.</p>
            <p>
              A project will hold the timeline and milestones an operator is working
              toward, and the workflow runs and chats that belong to them.
            </p>
          </div>
        </Section>
        <Section id="workflows" title="Workflows" open={open === "workflows"} onOpen={setOpen}>
          <div className="rail-nav">
            <a className="rail-button rail-button-primary" href="/runs/new">
              + New workflow
            </a>
            <a
              className="rail-button"
              data-active={activeView === "runs" || undefined}
              href="/runs"
            >
              All workflow runs
            </a>
          </div>
          <nav className="rail-scroll" aria-label="Recent runs">
            {runs.map((run) => (
              <div key={run.runId}>
                <div
                  className="rail-item"
                  data-active={
                    activeRunId === run.runId && !activeConversationUrl ? true : undefined
                  }
                >
                  <a className="rail-item-trigger" href={`/runs/${run.runId}`}>
                    <span className="rail-item-title" data-clamp="">
                      {run.name}
                    </span>
                    <span className="rail-item-meta">
                      {IN_PROGRESS_PHASES.has(run.phase) && (
                        <span className="rail-live" aria-label="Workflow is in progress" />
                      )}
                      {runStatusLabel(run)} · {run.workflowVersion || run.workflowId}
                    </span>
                  </a>
                </div>
                {conversationCount(run) > 0 && (
                  <div className="rail-sub" aria-label={`Conversations for ${run.name}`}>
                    {run.steps
                      .filter((step) => step.conversationUrl)
                      .map((step) => (
                        <a
                          aria-current={
                            activeConversationUrl === step.conversationUrl ? "page" : undefined
                          }
                          data-active={activeConversationUrl === step.conversationUrl || undefined}
                          href={step.conversationUrl!}
                          key={step.stepId}
                        >
                          {step.name} conversation
                          {step.waiting && <span aria-label="Waiting for input"> ❔</span>}
                        </a>
                      ))}
                  </div>
                )}
              </div>
            ))}
          </nav>
        </Section>
        <Section id="chats" title="Chats" open={open === "chats"} onOpen={setOpen}>
          <ThreadListPrimitive.Root className="rail-list">
            <div className="rail-nav">
              {linkChats ? (
                <a className="rail-button rail-button-primary" href="/conversations">
                  + New chat
                </a>
              ) : (
                <ThreadListPrimitive.New className="rail-button rail-button-primary">
                  + New chat
                </ThreadListPrimitive.New>
              )}
            </div>
            <div className="rail-scroll">
              <ThreadListPrimitive.Items>
                {() => <ThreadListItem linked={linkChats} />}
              </ThreadListPrimitive.Items>
              <details className="rail-archive">
                <summary>Archived</summary>
                <ThreadListPrimitive.Items archived>
                  {() => <ThreadListItem archived />}
                </ThreadListPrimitive.Items>
              </details>
            </div>
          </ThreadListPrimitive.Root>
        </Section>
      </div>
      <RailFoot />
    </aside>
  );
}
