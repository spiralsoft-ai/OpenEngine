# `[BETA]` WorkOrders, in plain English

Status: implemented for `apps/web`

There are now two kinds of workflow you can pick when you create a WorkOrder.
This page explains what the second kind is, what happens when you pick it, and
what it cannot do yet — no background in the codebase assumed.

## The two kinds

Every workflow lives as a file in the `workflows` directory, and that directory
is what this deployment knows how to run.

- **A step workflow** is a list: do this, then that, then ask a person. The
  part of OpenEngine that has been running for months reads the list and works
  through it. These appear in the dropdown with a version next to them, like
  `Implementation review · v1`.
- **A graph workflow** is a drawing: boxes with arrows between them. A
  different engine — LangGraph — runs those. These appear in the dropdown with
  `[BETA]` in front of their name and no version.

Both do roughly the same job for the implementation-review workflow: make a
checkout, let an agent change the code, let an agent review the change, then
stop and wait for a person to say yes or no. They differ in what the engine
underneath can do, which is the reason the second one exists:

- The checkout is one of the boxes. If the checkout fails, the run stops
  *there*, visibly, instead of the whole thing failing before it ever started.
- Waiting for a person does not end the agent's turn. It sits there, holding
  the conversation, and carries on when you answer — so answering is a reply
  rather than a fresh start.
- The same is true when an agent asks permission mid-task.

## What happens when you pick one

1. You choose a `[BETA]` entry, type your task and repository, and press
   create.
2. The web server hands the task and the repository to the graph engine and
   asks it to start that graph. The agent is not a separate choice here: there
   is one `[BETA]` entry per agent (`(codex)`, `(claude)`), so picking the
   entry is picking the agent — which is why the form stops asking you which
   runner to use as soon as you select one.
3. The graph engine gives the run an id, and the WorkOrder you see is saved
   under that same id — so both halves are talking about the same run.
4. You land on the WorkOrder page, which shows the task, the repository and
   whether the run is still going.

## Following one, and talking to it

The WorkOrder page shows a graph run's stages, and each agent node has an **Open
conversation** link once it has said anything. That conversation is the same
view a chat is: the task the node was given as the first turn, what the agent
said, its tool calls folded into rows you can open, and — while the agent is
working — a box to write in. What you write is *steering*: a message into the
turn the agent is in the middle of, not a new one. A node that has finished has
nothing in flight to say it to, so the box is not offered.

When an agent stops to ask permission, the request appears in that conversation
under the command it is about, with the buttons to answer it. The run's final
human verdict is answered from the WorkOrder page itself, in the **Action
required** panel.

## What it cannot do yet — and this is why it says `[BETA]`

The event log a conversation is drawn from lives in the server's memory, so
restarting the server empties it: the run picks back up (see below), but what
was said before the restart is gone from the page. Nothing else keeps a graph
run's transcript, so this is the one thing to know before relying on it.

Everything the pages read is served by the graph engine's own API, which the web
server passes through under `/graph`:

```
GET  /graph/api/graphs                              every graph it can run
GET  /graph/api/runs/{run}                          where a run is now, and what
                                                    it is waiting for
GET  /graph/api/runs/{run}/events                   a live feed of everything the
                                                    run says
POST /graph/api/runs/{run}/steering                 send a message to whichever
                                                    agent is working
POST /graph/api/runs/{run}/approvals/{approval}     answer a question it stopped
                                                    on: {"decision": "accept"}
```

To follow one from a terminal, copy its run id from the WorkOrder page and tail
that event feed (the development server uses port 8000 by default):

```bash
curl -N http://localhost:8000/graph/api/runs/RUN_ID/events
```

The useful boundary events are `node.started` (LangGraph scheduled the step),
`conversation.started` (the ACP adapter connected and established the agent
session), `transcript` and `tool.call` (the agent is producing work), and
`approval.requested` (it is waiting for a decision). If a run fails, the
terminal running `engine-web` or the `[api]` side of `engine-dev` carries the
full Python traceback. Recent ACP adapter stderr is included there when the
adapter refuses a request or exits; it is otherwise kept out of the transcript.

A run stops and waits the first time an agent asks permission, and again at the
end when it wants a person's verdict. Both are answerable from the pages; the
last call above is the same answer given from a script, and
`GET /graph/api/runs/{run}` lists what is outstanding with the id to answer.

## Where things are kept

- The graph engine writes what it knows into two small database files under
  `graph-state/` next to where you started the server (`graph_state_directory`
  in `apps/web/.../composition.py`). Delete that folder and the beta runs are
  forgotten; the WorkOrder rows in `conversations.sqlite3` would remain.
- The step executor is told to leave graph WorkOrders alone on startup. It
  would otherwise try to resume one and look for a list of steps that a graph
  does not have.

## What a restart does to a run

Where a run got to is written down; the thing actually *working* through the
graph is not — it is a task inside the server process, and stopping the server
ends it. So when the server starts, it goes through every unfinished `[BETA]`
WorkOrder and does one of three things:

| What the engine says about the run | What happens |
| --- | --- |
| It was working | It is sent back to the last position it saved, and carries on from there. Whatever the agent did after that position is lost — the process died mid-sentence, and there is no record of the rest. |
| It is waiting for you | Nothing. Your answer is what starts it again, and that works whether or not the server was restarted in between. |
| It finished or failed while the server was down | The WorkOrder row catches up to that ending, which it could not hear at the time. |

A run the engine has no record of at all — you deleted `graph-state/`, say — is
marked failed with that as the reason, rather than left claiming to be working
forever.

## If something is wrong with the graph engine itself

Two different things can go wrong here, and the server treats them differently
on purpose.

**A graph that does not compile** — a workflow file describes something that is
not a graph, perhaps after a dependency upgrade changed what LangGraph accepts.
The server **does not start**. The log says which graph it was and what was
wrong with it:

```
[BETA] workflow 'implementation-review-codex' does not compile, so this server
will not start: Graph must have an entrypoint: add at least one edge from START
```

This is a file somebody has to fix, and nothing improves by starting without it:
a server that quietly dropped the graph would be running a deployment nobody
configured, and you would find out the first time someone picked the workflow.

**Anything else about the engine's files** — the `graph-state/` directory is not
writable, a checkpoint file is being held by another process. That is about this
machine rather than about any graph, so the graph engine simply does not run:
the error is logged, the rest of the application starts normally, and no
`[BETA]` entries appear in the dropdown because nothing in this process could
run one.

## If no `[BETA]` entries appear

Then this deployment's `workflows` directory holds no graph workflows, so no
graph engine was started and there is nothing to offer. That is deliberate: an
entry nobody could start is worse than no entry at all.
