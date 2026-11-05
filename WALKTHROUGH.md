# Product walkthrough

This tour takes about ten minutes. Start the API and web workspace from [SETUP.md](SETUP.md), then
open `http://127.0.0.1:3000` and select **Open reference workspace**.

The local profile uses fixed example answers and synthetic records. This tour demonstrates navigation,
ticket context, evidence cards, and administrative workflows. Knowledge and Zendesk links point to
example domains; no real helpdesk or model is contacted.

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
Which brands had the most network outage tickets this quarter?
```

General chat uses a separate history. This example returns a fixed network-issue response with linked
tickets. It does not execute the requested calendar filter.

## Inspect analytics evidence

Open `http://127.0.0.1:3000/analytics` and ask:

```text
Which brands have the most tickets?
```

Review the brand counts and source links. The reference route groups the fixture tickets; it does not
run SQL or interpret arbitrary follow-ups. Select **Cedar Grill** in the brand filter, then click **Re-run with current filters**.
The new table contains one row with 20 tickets.

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
