# ESIP product map

ESIP follows support work from intake to resolution, then feeds the outcome back into knowledge and
operations reporting.

## Ticket workflow

1. A support engineer opens a ticket, store, or free-form question.
2. ESIP resolves the ticket, store, date, brand, and source scope.
3. Ticket, knowledge, document, or analytics tools return typed evidence.
4. The answer layer summarizes that evidence and keeps its links and identifiers.
5. Validation checks scope, citations, ticket references, and unsupported claims.
6. Structured telemetry records route, latency, quality, and cost outcomes.

## Connected workspaces

| Workspace | Responsibility |
|---|---|
| Zendesk companion | Keeps summary, customer risk, similar cases, and chat beside the active ticket. |
| Ticket Context | Opens tickets and store history without changing the active conversation. |
| Universal Chat | Combines scoped evidence and preserves follow-up constraints. |
| Ticket Analytics | Produces read-only operational tables and charts with query provenance. |
| Knowledge Gaps | Finds repeated support work that current documentation does not cover well. |
| Admin | Exposes access, source, job, cost, quality, notification, and reporting controls. |

## Evidence sources

Support tickets provide timelines, resolution details, assignment, status, and SLA fields. Maintained
articles and Drive documents provide procedures and exact structured rows. Analytics supplies bounded
aggregates. Each source retains its own identity, freshness, and retrieval path.

## Operating model

Source sync is incremental. Checkpoints let interrupted jobs continue without repeating finished work.
Model routes define their tools, output limits, thinking controls, timeouts, retries, and cost bounds.
Administrators can inspect partial failures instead of receiving a false success state.

The Northstar workspace keeps 12 brands, 60 stores, 12 users, and 240 tickets aligned across the API,
frontend, tests, and Zendesk application. The same fixture graph supports repeatable local runs and
regression tests.
