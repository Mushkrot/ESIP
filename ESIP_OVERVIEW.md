# ESIP architecture

ESIP separates user interaction, domain policy, provider access, and background processing. The
browser clients never call data or AI providers directly. FastAPI owns authentication, authorization,
scope resolution, tool selection, result normalization, and telemetry.

## System map

```mermaid
flowchart TB
    subgraph clients["Client applications"]
        WEB["React web workspace"]
        ZAF["Zendesk sidebar and modal"]
    end

    subgraph api["FastAPI application"]
        AUTH["Identity and role policy"]
        CHAT["Universal Chat orchestration"]
        TICKET["Ticket investigation"]
        KG["Knowledge-gap analysis"]
        REPORTS["Reports and administration"]
        VALIDATE["Answer validation and bounded repair"]
    end

    subgraph services["Service boundaries"]
        HELP["Ticketing adapter"]
        DATA["Analytics adapter"]
        SEARCH["Knowledge and vector retrieval"]
        MODEL["Model gateway"]
        DOCS["Document ingestion"]
        MAIL["Notification adapter"]
    end

    subgraph state["State and processing"]
        OP["Operational configuration"]
        WAREHOUSE["Ticket and reporting data"]
        INDEX["Search indexes"]
        JOBS["Incremental jobs and checkpoints"]
        TELEMETRY["Quality, latency, and cost events"]
    end

    WEB --> AUTH
    ZAF --> AUTH
    AUTH --> CHAT
    AUTH --> TICKET
    AUTH --> KG
    AUTH --> REPORTS
    CHAT --> HELP
    CHAT --> DATA
    CHAT --> SEARCH
    CHAT --> MODEL
    CHAT --> VALIDATE
    TICKET --> HELP
    KG --> DATA
    KG --> SEARCH
    REPORTS --> OP
    REPORTS --> TELEMETRY
    HELP --> WAREHOUSE
    DATA --> WAREHOUSE
    SEARCH --> INDEX
    DOCS --> INDEX
    MODEL --> TELEMETRY
    JOBS --> HELP
    JOBS --> DOCS
    JOBS --> WAREHOUSE
```

## Request path

1. The API resolves the authenticated user, role, and tenant.
2. Scope parsing extracts ticket IDs, store IDs, dates, brands, and references to earlier results.
3. Policy selects the allowed ticket, knowledge, structured-document, or analytics tools.
4. Adapters return typed result sets with source identity and provenance.
5. The response layer builds an answer from those results.
6. Validation checks scope retention, citations, ticket references, unsupported claims, and tool errors.
7. A bounded repair may run when the failure type is eligible. The original evidence remains fixed.
8. The API records route, latency, quality, and cost metadata for review.

Ticket-specific and general conversations have separate histories. Opening a ticket does not silently
change the active conversation; the question must refer to that ticket before its context is used.

## Runtime profiles

`RUNTIME_PROFILE=reference` is the default. It supplies deterministic identities, tickets, knowledge,
analytics, reports, jobs, and administrative state from local services. Mutations live for the life of
the API process and reset on restart.

`RUNTIME_PROFILE=connected` enables configured provider adapters. Authentication and authorization
remain server-side. Credentials are read from the runtime environment, not from either client.

The profiles share HTTP contracts, so browser behavior and API tests do not need provider-specific
branches.

## Data and processing

The connected architecture uses BigQuery for ticket analytics, Firestore for operational settings and
checkpoints, object storage for generated reports, and search indexes for knowledge and similar-ticket
retrieval. These systems are behind service interfaces; the reference runtime implements the same
interfaces in process.

Ticket and document ingestion is incremental. Each run records source, trigger, start time, completion
state, checkpoint, counts, and errors. A partial run stays visible as partial. Checkpoints advance only
after the associated work is complete.

OCR and embedding work has explicit batch limits and cost gates. Failed attachments retain their last
known state rather than entering an automatic retry loop.

## AI controls

Model calls use named route profiles. Each profile defines the model family, output limit, thinking
control, timeout, retry count, safety settings, and eligible tools. Unknown combinations fail before a
provider request is sent.

The model does not receive unrestricted databases or credentials. Tools return bounded evidence, and
the final answer keeps links and identifiers that let an engineer verify the result. Quality telemetry
records structured outcomes instead of full prompts by default.

## Access model

- Support engineers can investigate tickets, use chat, search knowledge, and view announcements.
- Administrators can manage routine users, sources, reports, notifications, and operational controls.
- Super administrators can manage protected model and platform controls and preview lower-access roles.

Role checks run in the API. Client-side navigation only changes what is visible; it does not grant
access.

## Deployment shape

The repository includes containers for the FastAPI service and background jobs, a static React build,
and a Zendesk application bundle. The intended hosted shape uses Cloud Run, Cloud Run jobs, Cloud
Scheduler, Firebase Hosting, BigQuery, Firestore, object storage, and Vertex AI services.

Local development needs none of those services. See [SETUP.md](SETUP.md).
