# Product walkthrough

This tour takes about ten minutes. Start the API and web workspace from [SETUP.md](SETUP.md), then
open `http://127.0.0.1:3000` and select **Open reference workspace**.

## Investigate a ticket

1. Open **AI Chat**.
2. Enter `950001` in Ticket Context.
3. Review the ticket subject, store, assignment, status, comments, and customer-risk panel.
4. Generate the brief summary and open the suggested knowledge records.
5. Open the related-cases view. Each result keeps its ticket ID, store, status, and resolution detail.

Ticket `950001` belongs to Cedar Grill store `2001`. The same identifiers are used by the API,
analytics tables, chat evidence, and embedded application.

## Continue with scoped chat

Select the `Chat #950001` tab and ask:

```text
What has already been tried, and what should the next engineer verify?
```

The answer stays attached to ticket `950001`. Switch to **General** and ask:

```text
Show payment-terminal issues from the last 30 days by brand.
```

General chat uses a separate history. Opening a ticket on the left does not change the analytics
question into a ticket question.

## Inspect analytics evidence

Open **Ticket Analytics** and run a supported question. Review the returned table, date range, brand
scope, and linked knowledge records. The result includes the query and scope metadata used to produce
it.

Follow with a narrower request such as:

```text
Show only Cedar Grill and sort by ticket count.
```

The follow-up operates on the earlier result set rather than widening the query.

## Review a knowledge gap

Open **Knowledge Gaps** and run the default analysis. The workspace returns three ranked findings with
ticket samples, source coverage, impact detail, and recommended documentation work.

Open one finding, inspect its evidence, then create an article draft. The draft remains in review state;
the analysis workflow does not publish it.

## Check operational controls

Use the Admin navigation to inspect:

- **Data Sources** for seven source adapters, enablement, exclusions, schedules, and sync history;
- **Quality** for validation outcomes, repair attempts, model-route exposure, and latency;
- **Usage Insights** and **Service Desk KPI** for adoption and workload reporting;
- **Impact** for assisted tickets, response latency, solved volume, and evidence coverage;
- **Users** for role, access, and durable-chat-log controls.

Reference-mode changes are local. For example, disable a schedule or add a notification recipient, then
restart the API to restore the original state.

## Open the Zendesk companion

Build and serve the embedded application as described in [SETUP.md](SETUP.md), then open
`http://127.0.0.1:4567/reference.html`.

The sidebar opens on ticket `950001`. Expand the summary, inspect five related cases, and select
**Details & Chat** for the modal workflow. The ticket number and conversation scope carry into the
larger view.
