# Starting a work order by pinging the bot

Ping the OpenEngine bot in Slack and it starts a WorkOrder, replies in that
thread with a link to it, and keeps reporting there until a person's decision
is needed — at which point it says so and names them.

```
you    @openengine add a health endpoint to the API
bot    ↳ Started a work order on `acme/api`. I will report progress here.
         View work order
bot    ↳ *Implementation* started.
bot    ↳ *Implementation*: reading how the existing routes are registered
bot    ↳ *Implementation* complete.
         Added /healthz and a test for it.
         Open pull request · View work order
bot    ↳ *Review* started.
bot    ↳ *Review* complete. …
bot    ↳ @you Review complete and ready for your decision: …
         Open pull request · View work order
```

## What has to be set up

Three things, and each is independently missing-able. `/api/slack/status`
reports whether all three are in place as `events`.

**1. The Slack app can post.** The existing Connect Slack flow in Settings.
Reconnect after upgrading: the authorization now also asks for
`app_mentions:read`, and without it Slack delivers no mentions at all.

**2. The signing secret is saved.** Settings → Slack → *Slack Signing Secret*,
from your app's *Basic Information* page. It is how this server tells a real
delivery from anyone who found the URL, and an unsigned request is refused.
Without one saved, `/api/slack/events` answers 503 and nothing starts.

**3. Slack knows where to deliver.** In your Slack app, under *Event
Subscriptions*, set the request URL to `<public_url>/api/slack/events` and
subscribe the bot to `app_mention`. Slack verifies the URL once with a
handshake this endpoint answers, so save the signing secret first.

Then say what a mention should run, in `engine.toml`:

```toml
public_url = "https://engine.example"   # what the links in the thread point at

[work_orders]
repository = "acme/api"                 # required; a mention names no repository
workflow = "implementation-review-v1"   # optional when the deployment has one
runner = "claude"                       # optional; the executor's default
```

`repository` is the one with no sensible default. Without it a mention is
answered with a message saying so rather than with a run against a repository
nobody named.

## What the agent can say

A step whose run came from a conversation is served one extra run-bound MCP
tool, `update_status`, and told to use it. It takes a sentence, posts it in the
thread, and does not end the step. A run started from the web is not served the
tool at all, because there is nowhere for its updates to go.

Everything else in the thread is the runtime reporting, not the agent:

| What happened | What the thread says |
| --- | --- |
| A step began | `*Review* started.` |
| `update_status` | `*Implementation*: <what the agent wrote>` |
| `complete_step` | `*Implementation* complete.` with the step's summary, the pull request when the step declared a `pr_url` output, and a link to the WorkOrder |
| `fail_step` | `*Implementation* failed.` with the reason |
| `clarify` | the agent answered a question and changed nothing |
| Any other pausing tool | `*Implementation* is waiting for an answer.` with what it asked |
| The run died outside a step | `This work order failed.` with the reason |
| Reviews finished | the human-review notification, addressed to whoever asked |

Answering in the thread does **not** continue the run today. The reply is
where the WorkOrder reports; the WorkOrder page is where it is answered, which
is what the link in every message is for.

## What it will not do

- **`[BETA]` graph workflows are not startable this way.** They are run by the
  other engine, which has neither the run-bound tools an agent reports through
  nor anywhere to keep where the request came from — so one started from a
  mention would go silent the moment it began. Only step workflows are offered.
- **Slack redeliveries are ignored.** Slack retries anything it did not hear a
  prompt 200 for; acting on a retry would start the same work order twice, so a
  delivery carrying `X-Slack-Retry-Num` is acknowledged and dropped.
- **Nothing is reported when the provider is down.** Delivery is best effort
  everywhere: a Slack outage must not fail the work it was reporting on. The
  run continues and its record on the WorkOrder page is unaffected.
