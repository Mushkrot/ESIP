# ESIP quick guide

ESIP brings ticket investigation, past resolutions, operating procedures, documents, and analytics
into one support workspace.

In the local reference profile, answers and analytics are deterministic examples. The prompts below
describe connected workflows; local responses do not execute arbitrary filters or model reasoning.
Ticket and knowledge links use example domains.

## Start from the right place

Use the Zendesk sidebar when a ticket is already open. It shows customer risk, a ticket summary,
similar cases, and a larger **Details & Chat** view.

Use **AI Chat** in the full workspace for store history, ticket comparison, document search, analytics,
or any question that crosses sources. You can begin with a ticket or ask a general question first.

## Open a ticket or store

Ticket Context understands these forms:

| Input | Result |
|---|---|
| `950014` | Opens ticket 950014 |
| `950014, 950015` | Lists both tickets |
| `950014-950017` | Lists the ticket range |
| `2002` | Opens store 2002 and its ticket history |
| `store:2002` or `s2002` | Opens store 2002 explicitly |
| `#950014` | Opens ticket 950014 explicitly |

A bare four-digit number is treated as a store. Prefix a shorter legacy ticket number with `#`.
When the input cannot be resolved, **Ask AI...** moves a clarification draft into Conversation.

## Keep conversations scoped

**General** chat handles questions that do not depend on one open ticket. Each ticket also receives its
own `Chat #...` tab. Ticket tabs keep separate histories and can be reordered or closed.

Opening a ticket does not take over the active conversation. ESIP uses that ticket only when the
question refers to it. A new store, document, or trend question remains independent.

Useful ticket prompts:

- `Summarize this ticket for handoff.`
- `What has already been tried, and what should the next engineer verify?`
- `Find similar resolved tickets and show the resolution.`
- `Use only the current ticket and the linked procedure.`

Useful cross-source prompts:

- `For store 2011, show recurring issues from the last 90 days.`
- `Compare tickets 950014, 950015, and 950016.`
- `Find the procedure for resetting a voice gateway.`
- `Show payment-terminal issues from the last 30 days by brand.`
- `Find the exact support-repository row for store 2002.`

Names, identifiers, error text, source scope, and date ranges make retrieval more precise.

## Verify the answer

Depending on the question, an answer can include ticket cards, knowledge links, table rows, query
scope, and an **Open left** action. These items show what supports the response. Inspect them before a
high-impact change or customer-facing commitment.

Select **Improve answer** when an answer looks incomplete. ESIP rechecks the existing evidence and may
return a reviewed or repaired answer. It does not silently expand the source scope.

Good follow-ups include:

- `Show the ticket IDs used.`
- `Use only Drive documentation.`
- `Show the exact spreadsheet row.`
- `Separate confirmed facts from assumptions.`
- `Give only the actions an L1 engineer can take.`

## Work with knowledge gaps

Open **Knowledge Gaps** to compare recurring ticket patterns with available documentation. Choose the
sources, ticket period, minimum cluster size, evidence strictness, and ranking signal before running
the analysis.

Each finding includes ticket evidence, current source coverage, impact detail, and a recommended
documentation action. Completed runs remain available under **Recent analyses**. The Drafting Assistant
creates reviewable material; it does not publish documents.

## Admin workspaces

Administrators can inspect source configuration, schedules, sync history, notifications, users, usage,
service-desk KPIs, impact, costs, quality, and chat logs. Protected model-routing and platform controls
require the super administrator role.

## When a result is weak

Add the exact ticket, store, system, vendor, error, phrase, or date range. State whether ESIP should use
the current ticket, past tickets, maintained articles, Drive documents, or all sources. For analytics,
request a table, comparison, checklist, handoff note, or documentation backlog.

If a result is empty, check whether a four-digit number was meant to be a store, broaden an overly
narrow filter, or ask ESIP to report the source and scope it searched.
