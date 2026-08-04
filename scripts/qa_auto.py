#!/usr/bin/env python3
"""
Autonomous QA script for ESIP.

Tests (mix of HTTP and direct service calls):
  1. BQ exact search finds the expected Drive document
  2. Analytics router still routes aggregate questions to BQ
  3. Analytics router still routes exact Drive phrase questions to Drive exact
  4. Universal policy forces deterministic store issue breakdown
  5. Universal context policy ignores unrelated open ticket for explicit store questions
  6. Universal context policy lets explicit ticket ID override open ticket context
  7. Universal policy lets broad explicit-ticket asks still use tools
  8. Universal policy keeps same-restaurant follow-ups scoped to the open ticket
  9. Universal policy keeps challenge/source follow-ups scoped to the open ticket
10. Universal policy keeps prevention/fix follow-ups scoped to the open ticket
 11. Universal policy keeps contextual category/frequency follow-ups scoped
 12. Universal policy builds same-store context snapshots without incident keywords
 13. Universal prompt treats analytics errors as uncertainty, not no-data
 14. Universal prompt keeps store snapshot evidence boundaries explicit
 15. Universal BQ evidence can build City Fries 2009 snapshots
 16. Universal prompt keeps event timing and other-store grounding separate
 17. Universal similar-ticket filtering removes low-context examples
 18. Universal chat HTTP handles store issue breakdown without dead-end answer
 19. Universal chat HTTP routes exact Drive phrase search

Run:
  scripts/backend_python.sh scripts/qa_auto.py
"""
import json
import sys
import os
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
VENV_PYTHON = BACKEND / ".venv" / "bin" / "python"
if VENV_PYTHON.exists() and Path(sys.executable).resolve() != VENV_PYTHON.resolve():
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

# Direct service imports
sys.path.insert(0, str(BACKEND))
from dotenv import load_dotenv
load_dotenv(override=True)

BASE_URL = "https://api.esip.example.com"

PASS = "✅ PASS"
FAIL = "❌ FAIL"


def get_token() -> str:
    resp = requests.post(f"{BASE_URL}/api/zaf/auth", json={
        "email": "morgan.chen@northstar.example.com",
        "name": "QA Bot",
        "role": "agent",
    }, timeout=10)
    resp.raise_for_status()
    return resp.json()["session_token"]


def universal_chat_http(token: str, question: str, ticket_id: int | None = None) -> str:
    headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
    payload = {
        "messages": [{"role": "user", "content": question}],
        "ticket_id": ticket_id,
    }
    resp = requests.post(
        f"{BASE_URL}/api/universal/stream",
        headers=headers, json=payload, stream=True, timeout=45,
    )
    resp.raise_for_status()
    full_text = ""
    for line in resp.iter_lines():
        if not line:
            continue
        line = line.decode("utf-8") if isinstance(line, bytes) else line
        if line.startswith("data: "):
            try:
                data = json.loads(line[6:])
                if "delta" in data:
                    full_text += data["delta"]
                if data.get("done"):
                    break
            except Exception:
                pass
    return full_text


def check(name: str, response: str, must_contain: list = None,
          must_not_contain: list = None) -> bool:
    failures = []
    if must_contain:
        for term in must_contain:
            if term.lower() not in response.lower():
                failures.append(f"Missing: '{term}'")
    if must_not_contain:
        for term in must_not_contain:
            if term.lower() in response.lower():
                failures.append(f"Should NOT contain: '{term}'")
    status = FAIL if failures else PASS
    print(f"\n{status}  {name}")
    print(f"  Response: {response[:250].strip()!r}")
    for f in failures:
        print(f"  {FAIL} {f}")
    return not failures


results = []

print("=" * 65)
print("ESIP Autonomous QA — rev 00101")
print("=" * 65)

# ------------------------------------------------------------------
# TEST 1: BQ exact search directly (fundamental check)
# ------------------------------------------------------------------
print("\n--- TEST 1: BQ search_drive_exact — Oracle POS and Cash Drawers ---")
try:
    from app.services.bigquery import search_drive_exact
    hits = search_drive_exact("Oracle POS and Cash Drawers", page_size=5)
    ok = len(hits) > 0 and any("foundry burger" in h["file_name"].lower() for h in hits)
    status = PASS if ok else FAIL
    print(f"{status}  search_drive_exact returns Foundry Burger doc")
    if hits:
        print(f"  Found {len(hits)} result(s): {hits[0]['file_name']}")
    else:
        print(f"  No results returned")
    results.append(ok)
except Exception as e:
    print(f"{FAIL}  Exception: {e}")
    results.append(False)

# ------------------------------------------------------------------
# TEST 2: Analytics routing decision — network last month
# ------------------------------------------------------------------
print("\n--- TEST 2: Analytics routing — network last month (no clarification) ---")
try:
    from app.routers.analytics import _decide_sources
    decision = _decide_sources(365, None, [{"role": "user", "content": "How many network tickets last month?"}])
    print(f"  decision: needs_query={decision['needs_query']}, needs_kb={decision['needs_kb_search']}, desc={decision['description']!r}")
    ok = decision["needs_query"] is True and bool(decision.get("sql"))
    status = PASS if ok else FAIL
    print(f"{status}  Routes to BQ query (not clarification or KB search)")
    results.append(ok)
except Exception as e:
    print(f"{FAIL}  Exception: {e}")
    results.append(False)

# ------------------------------------------------------------------
# TEST 3: Analytics routing decision — exact Drive search
# ------------------------------------------------------------------
print("\n--- TEST 3: Analytics routing — exact Drive phrase ---")
try:
    from app.routers.analytics import _decide_sources
    decision = _decide_sources(365, None, [{"role": "user", "content": 'Find document that contains: "Oracle POS and Cash Drawers"'}])
    print(f"  decision: needs_drive_exact={decision.get('needs_drive_exact')}, drive_query={decision.get('drive_exact_query')!r}")
    ok = decision.get("needs_drive_exact") is True and bool(decision.get("drive_exact_query"))
    status = PASS if ok else FAIL
    print(f"{status}  Routes to Drive exact search")
    results.append(ok)
except Exception as e:
    print(f"{FAIL}  Exception: {e}")
    results.append(False)

# ------------------------------------------------------------------
# TEST 4: Universal deterministic policy — store issue breakdown
# ------------------------------------------------------------------
print("\n--- TEST 4: Universal policy — store issue breakdown ---")
try:
    from app.routers.universal import _is_store_issue_breakdown_question
    q = "Can you give me the details and top issues from store 2011 from the past 3 months?"
    ok = _is_store_issue_breakdown_question(q) is True
    status = PASS if ok else FAIL
    print(f"{status}  Store issue breakdown bypass is active")
    results.append(ok)
except Exception as e:
    print(f"{FAIL}  Exception: {e}")
    results.append(False)

# ------------------------------------------------------------------
# TEST 5: Universal context policy — unrelated open ticket ignored for store question
# ------------------------------------------------------------------
print("\n--- TEST 5: Universal policy — open ticket ignored for store analytics ---")
try:
    from app.services import chat_policy
    q = "Can you give me the details and top issues from store 2011 from the past 3 months?"
    ok = chat_policy.should_use_open_ticket_context(q) is False
    status = PASS if ok else FAIL
    print(f"{status}  Store analytics question does not inject unrelated open-ticket context")
    results.append(ok)
except Exception as e:
    print(f"{FAIL}  Exception: {e}")
    results.append(False)

# ------------------------------------------------------------------
# TEST 6: Universal context policy — explicit ticket ID override
# ------------------------------------------------------------------
print("\n--- TEST 6: Universal policy — explicit ticket ID override ---")
try:
    from app.services import chat_policy
    q = "Can you give me the details about ticket [#950014](https://support.example.com/agent/tickets/950014)?"
    explicit_id = chat_policy.extract_explicit_ticket_id(q)
    ok = explicit_id == 950026 and chat_policy.should_use_open_ticket_context(q) is False
    status = PASS if ok else FAIL
    print(f"{status}  Explicit ticket ID extracted and does not depend on open-ticket context")
    results.append(ok)
except Exception as e:
    print(f"{FAIL}  Exception: {e}")
    results.append(False)

# ------------------------------------------------------------------
# TEST 7: Universal context policy — broad explicit-ticket asks still use tools
# ------------------------------------------------------------------
print("\n--- TEST 7: Universal policy — broad explicit-ticket ask does not become ticket-only ---")
try:
    from app.services import chat_policy
    q = (
        "Analyze ticket 950009 and all similar degradation tickets from this same restaurant "
        "and other restaurants over the last year."
    )
    ok = chat_policy.extract_explicit_ticket_id(q) == 950032 and chat_policy.explicit_ticket_request_needs_tools(q) is True
    status = PASS if ok else FAIL
    print(f"{status}  Explicit ticket can seed context while still allowing tools")
    results.append(ok)
except Exception as e:
    print(f"{FAIL}  Exception: {e}")
    results.append(False)

# ------------------------------------------------------------------
# TEST 8: Universal context policy — same restaurant follow-up
# ------------------------------------------------------------------
print("\n--- TEST 8: Universal policy — same restaurant follow-up uses open ticket ---")
try:
    from app.services import chat_policy
    questions = [
        "Were there similar complaints in this restaurant before that incident?",
        "Были ли жалобы такого же или похожего рода именно в этом ресторане до этого инцидента?",
        "Были ли жалобы такого же или похожего рода именно в этом рестаране до этого инцидента?",
    ]
    ok = all(chat_policy.should_use_open_ticket_context(q) is True for q in questions)
    status = PASS if ok else FAIL
    print(f"{status}  Same-restaurant follow-up keeps open ticket/store context")
    results.append(ok)
except Exception as e:
    print(f"{FAIL}  Exception: {e}")
    results.append(False)

# ------------------------------------------------------------------
# TEST 9: Universal policy — challenge/source questions use context
# ------------------------------------------------------------------
print("\n--- TEST 9: Universal policy — challenge/source question uses context ---")
try:
    from app.services import chat_policy
    questions = [
        "Где ты нашел такую информацию? Ты уверен? Проверь источник.",
        "None of them is related to this ticket",
        "These tickets are not related to the current issue",
        "Эти тикеты не связаны с текущей проблемой",
    ]
    ok = all(
        chat_policy.is_prior_answer_challenge(q) is True
        and chat_policy.should_use_open_ticket_context(q) is True
        for q in questions
    )
    status = PASS if ok else FAIL
    print(f"{status}  Prior-answer challenge forces current ticket context")
    results.append(ok)
except Exception as e:
    print(f"{FAIL}  Exception: {e}")
    results.append(False)

# ------------------------------------------------------------------
# TEST 10: Universal policy — prevention/fix follow-up uses context
# ------------------------------------------------------------------
print("\n--- TEST 10: Universal policy — prevention/fix follow-up uses open ticket ---")
try:
    from app.services import chat_policy
    true_questions = [
        "how can this be avoided",
        "how do we avoid this",
        "how can this be prevented",
        "what should we do to prevent it from happening again?",
        "как этого избежать?",
        "как предотвратить повторение этой проблемы?",
    ]
    false_questions = [
        "how can payment issues be avoided at all stores?",
        "what are common prevention steps for restaurants?",
    ]
    ok = (
        all(chat_policy.should_use_open_ticket_context(q) is True for q in true_questions)
        and all(chat_policy.should_use_open_ticket_context(q) is False for q in false_questions)
    )
    status = PASS if ok else FAIL
    print(f"{status}  Prevention/fix follow-up keeps open ticket context without broad overreach")
    results.append(ok)
except Exception as e:
    print(f"{FAIL}  Exception: {e}")
    results.append(False)

# ------------------------------------------------------------------
# TEST 11: Universal policy — contextual category/frequency follow-up
# ------------------------------------------------------------------
print("\n--- TEST 11: Universal policy — contextual category count uses open ticket ---")
try:
    from app.routers import universal
    from app.services import bigquery, chat_policy
    true_questions = [
        "How many times has this category of issue been reported?",
        "how often does this type happen?",
        "how many tickets are there for this kind of issue?",
        "сколько раз такая категория встречалась?",
    ]
    false_questions = [
        "How many hard drive issues were reported?",
        "How many categories were reported this week?",
    ]
    sql = universal._contextual_category_count_sql(950034)
    result = bigquery.execute_query(sql)
    row = (result.get("rows") or [[None, None, 0, 0]])[0]
    ctx = "TICKET ID: #950034\nRESTAURANT: Summit Burger 2006 (Store #2006, Brand: Summit Burger)\nISSUE CATEGORY: Server > Hard Drive"
    force_contextual_category_count = (
        chat_policy.should_use_open_ticket_context(true_questions[0])
        and chat_policy.is_contextual_category_frequency_followup(true_questions[0])
    )
    would_replace_with_store_snapshot = (
        not force_contextual_category_count
        and universal._should_build_store_context_snapshot(true_questions[0], ctx, None)
    )
    ok = (
        all(chat_policy.is_contextual_category_frequency_followup(q) for q in true_questions)
        and all(chat_policy.should_use_open_ticket_context(q) for q in true_questions)
        and all(not chat_policy.is_contextual_category_frequency_followup(q) for q in false_questions)
        and force_contextual_category_count
        and not would_replace_with_store_snapshot
        and "WHERE id = 950034" in sql
        and "issue_category" in sql
        and "total_matching_tickets" in sql
        and "matching_tickets_last_365_days" in sql
        and result.get("row_count") == 1
        and row[1] == "server__hard_drive"
        and int(row[2] or 0) > 0
    )
    status = PASS if ok else FAIL
    print(f"{status}  Category/frequency follow-up keeps open ticket context and uses current issue_category")
    results.append(ok)
except Exception as e:
    print(f"{FAIL}  Exception: {e}")
    results.append(False)

# ------------------------------------------------------------------
# TEST 12: Universal policy — scoped store snapshot helpers
# ------------------------------------------------------------------
print("\n--- TEST 12: Universal policy — scoped store snapshot helpers ---")
try:
    from app.routers import universal
    ctx = "TICKET ID: #950032\nRESTAURANT: City Fries 2009 (Store #2009, Brand: City Fries)\nSUBJECT: Network connectivity issues - degraded speed"
    q = "Analyze ticket 950009 and similar degradation tickets from this same restaurant over the last year."
    ok = (
        universal._extract_store_id_from_ticket_context(ctx) == "2009"
        and "internet" in universal._ticket_analysis_terms(q, ctx)
        and universal._should_build_store_context_snapshot(q, ctx, 950032) is True
    )
    status = PASS if ok else FAIL
    print(f"{status}  Store snapshot can derive scope without hardcoded event words")
    results.append(ok)
except Exception as e:
    print(f"{FAIL}  Exception: {e}")
    results.append(False)

# ------------------------------------------------------------------
# TEST 13: Universal prompt formatting — analytics error is not no-data
# ------------------------------------------------------------------
print("\n--- TEST 13: Universal prompt — analytics error is not treated as no-data ---")
try:
    from app.services.universal_chat import _fmt_bq
    text = _fmt_bq("Query error: Syntax error: Unclosed string literal", "SELECT 'bad")
    ok = (
        "ANALYTICS QUERY ERROR" in text
        and "Do not infer" in text
        and "no valid rows" in text
    )
    status = PASS if ok else FAIL
    print(f"{status}  Failed analytics SQL is labeled as error, not zero matches")
    results.append(ok)
except Exception as e:
    print(f"{FAIL}  Exception: {e}")
    results.append(False)

# ------------------------------------------------------------------
# TEST 14: Universal prompt formatting — store snapshot beats wrong-store proof
# ------------------------------------------------------------------
print("\n--- TEST 14: Universal prompt — store snapshot beats wrong-store proof ---")
try:
    from app.services.universal_chat import _fmt_bq
    text = _fmt_bq(
        [
            {"evidence_scope": "same_store_history_snapshot", "id": 950002, "ticket_created_at": "2024-04-19", "ticket_solved_at": "2024-04-26", "store_id": "2009", "subject": "Mon-3p not working"},
            {"evidence_scope": "same_store_history_snapshot", "id": 950003, "ticket_created_at": "2024-07-18", "ticket_solved_at": "2024-07-25", "store_id": "2009", "subject": "Store clock in ipad is disconnecting from the wifi"},
        ],
        "DETERMINISTIC STORE CONTEXT SNAPSHOT: store_id=2008, rows=2, keyword_filter=none",
    )
    ok = (
        "SAME-STORE CONTEXT SNAPSHOT" in text
        and "keyword-free snapshot" in text
        and "ticket_created_at for the ticket open date" in text
        and "Do not use global similar-ticket rows as same-restaurant evidence" in text
    )
    status = PASS if ok else FAIL
    print(f"{status}  Store snapshot explains current-store evidence boundary")
    results.append(ok)
except Exception as e:
    print(f"{FAIL}  Exception: {e}")
    results.append(False)

# ------------------------------------------------------------------
# TEST 15: Universal BQ evidence — City Fries 2009 store snapshot
# ------------------------------------------------------------------
print("\n--- TEST 15: Universal BQ evidence — City Fries 2009 store snapshot ---")
try:
    from app.services.bigquery import fetch_store_history_snapshot
    rows = fetch_store_history_snapshot("2009", start_date="2024-01-01", end_date="2025-01-01", limit=160)
    ids = {int(row.get("id")) for row in rows if row.get("id")}
    years = {str(row.get("created_at", ""))[:4] for row in rows if row.get("created_at")}
    ok = (
        years == {"2024"}
        and {950004, 950005}.issubset(ids)
        and bool(ids.intersection({950002, 950003}))
        and 950008 not in ids
    )
    status = PASS if ok else FAIL
    print(f"{status}  Keyword-free store snapshot honors explicit 2024 window and contains incident-era context")
    results.append(ok)
except Exception as e:
    print(f"{FAIL}  Exception: {e}")
    results.append(False)

# ------------------------------------------------------------------
# TEST 16: Universal prompt — event timing and other-store grounding
# ------------------------------------------------------------------
print("\n--- TEST 16: Universal prompt — event timing and other-store grounding ---")
try:
    from app.routers import universal
    from app.services import chat_policy
    from app.services.universal_chat import SYSTEM_PROMPT, _fmt_similar
    sample_similar = _fmt_similar(
        [{
            "ticket_id": 950004,
            "created_at": "2024-12-15",
            "solved_at": "2024-12-23",
            "store_id": "2009",
            "brand": "City Fries",
            "org_name": "City Fries 2009",
            "issue_category": "Network > Meraki Firewall > No Power",
            "subject": "Restaurant fire above the office",
            "resolution": "Tech finished assessment",
        }],
        "fire history",
    )
    filtered, removed = universal._filter_out_store_tickets(
        [
            {"ticket_id": 950007, "store_id": "2009", "subject": "WiFi Dropping"},
            {"ticket_id": 950008, "store_id": "2007", "subject": "Support Workstation is slow"},
        ],
        "2009",
    )
    ok = (
        "Event/date claims must be grounded" in SYSTEM_PROMPT
        and "retrieved event-related tickets begin" in SYSTEM_PROMPT
        and "Do not convert email sent dates" in SYSTEM_PROMPT
        and "not necessarily event dates" in SYSTEM_PROMPT
        and "\"ticket from DATE\" means ticket created_at" in SYSTEM_PROMPT
        and "never label a ticket as other-restaurant evidence" in SYSTEM_PROMPT
        and "created_at=2024-12-15" in sample_similar
        and "solved_at=2024-12-23" in sample_similar
        and "store_id=2008" in sample_similar
        and chat_policy.requests_other_restaurant_comparison("Сравни с похожими проблемами в других ресторанах") is True
        and removed == 1
        and len(filtered or []) == 1
        and (filtered or [{}])[0].get("store_id") == "2007"
    )
    status = PASS if ok else FAIL
    print(f"{status}  Prompt protects event-date and other-restaurant evidence boundaries")
    results.append(ok)
except Exception as e:
    print(f"{FAIL}  Exception: {e}")
    results.append(False)

# ------------------------------------------------------------------
# TEST 17: Universal similar-ticket filtering — low-context examples
# ------------------------------------------------------------------
print("\n--- TEST 17: Universal similar-ticket filtering — low-context examples ---")
try:
    from app.routers import universal
    from app.services import bigquery
    tickets = bigquery.fetch_ticket_details_for_cards([950007, 950006])
    filtered, removed = universal._filter_low_value_similar_tickets(tickets)
    kept_ids = {int(t.get("ticket_id")) for t in (filtered or []) if t.get("ticket_id")}
    ok = (
        removed >= 1
        and 950007 not in kept_ids
        and 950006 in kept_ids
    )
    status = PASS if ok else FAIL
    print(f"{status}  Low-context generic ticket examples are filtered before Phase 2")
    results.append(ok)
except Exception as e:
    print(f"{FAIL}  Exception: {e}")
    results.append(False)

# ------------------------------------------------------------------
# TEST 18: Universal chat HTTP — store issue breakdown
# ------------------------------------------------------------------
print("\n--- TEST 18: Universal chat HTTP — store 2011 breakdown ---")
try:
    token = get_token()
    resp = universal_chat_http(token, "Can you give me the details and top issues from store 2011 from the past 3 months?")
    ok = check(
        "Universal chat store breakdown: no dead-end answer",
        resp,
        must_contain=["6411"],
        must_not_contain=[
            "not filtered by a specific store", "not filtered by store",
            "cannot provide details", "cannot provide top issues",
        ],
    )
    results.append(ok)
except Exception as e:
    print(f"{FAIL}  Exception: {e}")
    results.append(False)

# ------------------------------------------------------------------
# TEST 19: Universal chat HTTP — exact Drive search
# ------------------------------------------------------------------
print("\n--- TEST 19: Universal chat HTTP — exact Drive phrase ---")
try:
    token = get_token()
    resp = universal_chat_http(token, 'Find document that contains: "Oracle POS and Cash Drawers"')
    ok = check(
        "Universal chat: exact Drive search finds Foundry Burger doc",
        resp,
        must_contain=["foundry burger"],
        must_not_contain=["did not find", "no document", "cannot find"],
    )
    results.append(ok)
except Exception as e:
    print(f"{FAIL}  Exception: {e}")
    results.append(False)

# ------------------------------------------------------------------
print("\n" + "=" * 65)
passed = sum(results)
total = len(results)
print(f"RESULTS: {passed}/{total} passed")
print("=" * 65)
sys.exit(0 if passed == total else 1)
