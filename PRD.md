# Product requirements

## Product goal

Give enterprise support engineers one governed workspace for ticket investigation, maintained
knowledge, operational analytics, and evidence-backed AI assistance.

## Users and access

- A support engineer investigates tickets, searches knowledge, asks scoped questions, and reviews
  announcements.
- An administrator manages routine users, sources, reports, notifications, and operational controls.
- A super administrator manages protected platform and model controls and can preview lower-access
  roles.

Authorization is enforced in the API. Hidden client navigation is not an access control.

## Ticket investigation

The workspace must find tickets and stores by stable identifier, display the ticket timeline and
organization profile, generate brief and detailed summaries, retrieve similar resolved tickets, and
keep ticket-specific chat separate from general chat. Store history and similar-case retrieval are
different scopes and must not be merged silently.

## Knowledge and analytics

Knowledge results retain source identity, URL, freshness, and indexing state. Administrators can
control sources independently and inspect sync history. Knowledge-gap analysis compares recurring
support needs with available material and produces drafts for review; it does not publish them.

Analytics accepts supported natural-language questions and produces read-only queries against approved
datasets and fields. Every result includes its date, tenant, brand, store, and row-limit scope along
with query provenance. Follow-up questions can operate on a prior result set without rerunning a wider
query.

## AI policy

Each model route has an explicit model family, tool policy, output limit, thinking control, timeout,
retry count, safety setting, and cost boundary. Unknown combinations stop before transport.

The answer path works from bounded tool results. It checks citations, ticket references, retained
scope, provider errors, and unsupported claims. Automatic repair is limited by attempt count and
eligible failure type. Manual review remains available when automatic validation is disabled.

## Processing and operations

Ticket, document, attachment, OCR, and embedding updates are incremental. Large runs use durable
checkpoints. The system distinguishes complete, partial, failed, and cost-blocked outcomes, and it does
not convert incomplete evidence into a successful result.

Administrators can inspect source health, schedules, sync runs, notifications, usage, service-desk
KPIs, impact, cost, quality, chat logs, and model-routing state. Protected controls require the super
administrator role.

## Embedded workflow

The Zendesk application runs in a 320 px sidebar and a larger modal. It shows customer-risk signals,
ticket summaries, similar tickets, and ticket-scoped chat, then opens the full ESIP workspace with the
active ticket identifier preserved.

## Quality requirements

- Local reference workflows run without external credentials or network access.
- Secrets and provider credentials are supplied at runtime and never embedded in either client.
- Unit, contract, integration, parser, and build checks run from a clean environment.
- Report exports are deterministic and machine-readable where the format allows it.
- Reduced-motion preferences are respected, and the embedded view has no horizontal overflow at
  `320 x 720`.
- External writes, paid model calls, jobs, and deployments require explicit connected configuration.

## Current limits

The reference profile stores mutations in the API process. Users, settings, drafts, and job state reset
when the backend restarts. It simulates provider outcomes and report delivery; it does not exercise
cloud quotas, provider latency, or third-party permission models.

The connected profile contains integration code, but it requires separate cloud resources,
credentials, indexes, datasets, and helpdesk configuration. The repository does not provision or
verify a hosted environment automatically. See
[connected-profile limits](ESIP_OVERVIEW.md#connected-profile-limits).

Generated summaries and answers remain decision support. Engineers must inspect linked evidence before
using a response for a high-impact change or customer-facing commitment.
