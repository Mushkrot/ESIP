"""
Cost tracking and budget enforcement for ESIP.

Every Gemini / Vertex AI call goes through this module:
  1. Pre-flight gate: check today's accumulated cost against daily budget.
  2. record_estimate(): write ESTIMATE event to BQ BEFORE the call (with projected cost).
  3. Post-call record: write ACTUAL token usage to Firestore + BQ (linked by correlation_id).
  4. Divergence check: flag unexpected behaviour (thinking tokens, cost ratio).

Cost Intelligence System (CIS) — 3-layer architecture:
  ESTIMATE → ACTUAL → RECONCILE (see COST_TRACKING_SPEC.md)

  Layer 1 (ESTIMATE): record_estimate(call_type, est_cost_usd, correlation_id)
    - Called BEFORE the Gemini/embedding call, after check_cost_gate()
    - Writes to BQ cost_events (event_type=ESTIMATE) via background thread queue
    - Never blocks the call path

  Layer 2 (ACTUAL): record_usage(..., correlation_id, est_cost_usd)
    - Called AFTER the call with real API-reported tokens
    - Writes to Firestore daily counter (operational gate)
    - Writes to BQ cost_events (event_type=ACTUAL) with estimate_error_pct
    - Links to ESTIMATE via correlation_id

  Layer 3 (RECONCILE): scripts/reconcile_billing.py — daily, async
    - Compares our BQ actuals to GCP Billing Export (1-24h lag)
    - Not yet implemented — see COST_TRACKING_SPEC.md Phase 2

BQ writer:
  - Background daemon thread drains _bq_event_queue in batches of up to 50 rows
  - Never blocks the call path (queue is non-blocking; events dropped if queue full)
  - Call flush_bq_events() at end of scripts to ensure all events written before exit

Firestore write strategy (record_usage):
  - Atomic Increment on cost_counters/{YYYY-MM-DD}.
  - Up to 3 retries with exponential backoff (0.3s / 0.9s / 2.7s).
  - If all retries fail: log error, increment _write_failure_count.
  - If _write_failure_count >= WRITE_FAILURE_THRESHOLD (3 consecutive): CostGateError.
    Rationale: if we cannot track costs, we cannot enforce limits — safest to stop.

Timezone: all date keys use America/New_York.
"""

import json
import logging
import os
import queue
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

BUSINESS_TZ = ZoneInfo("America/New_York")

# CIS Phase 1 — GCS audit spool (mandatory overflow path; /tmp is not durable on Cloud Run)
_COST_SPOOL_BUCKET = "esip-reference-data"
_COST_SPOOL_PREFIX = "cost_events_spool/"
_COST_DEAD_LETTER_PREFIX = "cost_events_spool/dead_letter/"

# Synthetic reference OCR estimate (CAD) used for divergence checks; the gate
# keeps a separate conservative reserve. Deployments should load reviewed rates
# from Firestore rather than relying on these bootstrap values.
OCR_MEASURED_EXPECTED_CAD = 0.00200
OCR_GATE_RESERVED_CAD = 0.020
VERTEX_SEARCH_RESERVED_CAD = 0.005


@dataclass
class BudgetBackendHealth:
    """Explicit backend state for fail-closed (replaces time-of-day heuristics)."""

    firestore_reachable: bool = True
    daily_snapshot_loaded: bool = False
    monthly_total_available: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def degraded(self) -> bool:
        return not self.firestore_reachable

    @property
    def is_healthy(self) -> bool:
        return self.firestore_reachable and self.daily_snapshot_loaded

    def copy(self) -> "BudgetBackendHealth":
        with self._lock:
            return BudgetBackendHealth(
                firestore_reachable=self.firestore_reachable,
                daily_snapshot_loaded=self.daily_snapshot_loaded,
                monthly_total_available=self.monthly_total_available,
            )


_budget_health = BudgetBackendHealth()


def get_budget_backend_health() -> BudgetBackendHealth:
    return _budget_health.copy()


def _health_fs_ok(*, daily_counter_read: bool = False) -> None:
    with _budget_health._lock:
        _budget_health.firestore_reachable = True
        if daily_counter_read:
            _budget_health.daily_snapshot_loaded = True


def _health_fs_fail() -> None:
    with _budget_health._lock:
        _budget_health.firestore_reachable = False

# ---------------------------------------------------------------------------
# BQ Cost Events Writer — background daemon thread
# Writes ESTIMATE + ACTUAL events to cost_events table (non-blocking).
# ---------------------------------------------------------------------------

_BQ_EVENTS_TABLE = "esip_reference.cost_events"  # prefixed with project at write time
_bq_event_queue: queue.Queue = queue.Queue(maxsize=5000)
_bq_client_lock = threading.Lock()
_bq_client_instance = None
_bq_write_enabled = True   # set False in tests to disable BQ writes


def _connected_runtime() -> bool:
    """Return whether external provider adapters are explicitly enabled."""
    from app.config import settings  # noqa: PLC0415

    return settings.runtime_profile == "connected"


def _get_bq_client():
    """Lazy singleton BQ client. Returns None if unavailable (never raises)."""
    global _bq_client_instance
    if not _connected_runtime():
        return None
    with _bq_client_lock:
        if _bq_client_instance is not None:
            return _bq_client_instance
        try:
            from google.cloud import bigquery  # noqa: PLC0415
            from app.config import settings    # noqa: PLC0415
            project = settings.google_cloud_project_id
            if settings.google_application_credentials:
                os.environ.setdefault(
                    "GOOGLE_APPLICATION_CREDENTIALS",
                    settings.google_application_credentials,
                )
            _bq_client_instance = bigquery.Client(project=project)
            return _bq_client_instance
        except Exception as exc:
            logger.warning("cost_tracker: BQ client init failed (non-critical): %s", exc)
            return None


_bq_events_spooled = 0
_bq_events_dead_lettered = 0
_bq_spool_lock = threading.Lock()


def _gcs_write_jsonl_line(relative_path: str, payload: dict) -> bool:
    """Write one NDJSON line to a new GCS object (one event per blob for simplicity)."""
    try:
        from google.cloud import storage  # noqa: PLC0415
        from app.config import settings  # noqa: PLC0415

        client = storage.Client(project=settings.google_cloud_project_id)
        line = json.dumps(payload, default=str) + "\n"
        client.bucket(_COST_SPOOL_BUCKET).blob(relative_path).upload_from_string(
            line, content_type="application/x-ndjson"
        )
        return True
    except Exception as exc:
        logger.error("cost_tracker: GCS spool write failed: %s", exc)
        return False


def _spool_single_event_to_gcs(event: dict) -> None:
    """Overflow path when in-memory queue is full — mandatory for audit durability (CIS Phase 1)."""
    global _bq_events_spooled
    rev = os.environ.get("K_REVISION", _process_session_id)[:64]
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    rel = f"{_COST_SPOOL_PREFIX}{rev}/{ts}_{uuid4().hex[:8]}.jsonl"
    with _bq_spool_lock:
        if _gcs_write_jsonl_line(rel, event):
            _bq_events_spooled += 1
            logger.warning("cost_tracker: BQ queue full — event spooled to gs://%s/%s", _COST_SPOOL_BUCKET, rel)


def _spool_dead_letter_batch(batch: list[dict]) -> None:
    global _bq_events_dead_lettered
    rev = os.environ.get("K_REVISION", _process_session_id)[:64]
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    rel = f"{_COST_DEAD_LETTER_PREFIX}{rev}/{ts}_{uuid4().hex[:8]}.jsonl"
    body = "".join(json.dumps(ev, default=str) + "\n" for ev in batch)
    try:
        from google.cloud import storage  # noqa: PLC0415
        from app.config import settings  # noqa: PLC0415

        client = storage.Client(project=settings.google_cloud_project_id)
        client.bucket(_COST_SPOOL_BUCKET).blob(rel).upload_from_string(body, content_type="application/x-ndjson")
        with _bq_spool_lock:
            _bq_events_dead_lettered += len(batch)
        logger.error(
            "cost_tracker: BQ insert failed — dead-lettered %d rows to gs://%s/%s",
            len(batch), _COST_SPOOL_BUCKET, rel,
        )
    except Exception as exc:
        logger.critical("cost_tracker: dead-letter GCS write also failed: %s", exc)


def _enqueue_bq_event(event: dict) -> None:
    if not _connected_runtime():
        return
    try:
        _bq_event_queue.put_nowait(event)
    except queue.Full:
        _spool_single_event_to_gcs(event)


def _gcs_recovery_loop() -> None:
    """Every ~60s, re-inject spooled events from GCS into the BQ writer queue."""
    while True:
        time.sleep(60)
        if not _connected_runtime():
            continue
        try:
            from google.cloud import storage  # noqa: PLC0415
            from app.config import settings  # noqa: PLC0415

            client = storage.Client(project=settings.google_cloud_project_id)
            b = client.bucket(_COST_SPOOL_BUCKET)
            for blob in b.list_blobs(prefix=_COST_SPOOL_PREFIX, max_results=200):
                name = blob.name
                if "dead_letter" in name:
                    continue
                try:
                    raw = blob.download_as_text()
                except Exception:
                    continue
                lines = [ln for ln in raw.splitlines() if ln.strip()]
                all_ok = True
                for ln in lines:
                    try:
                        ev = json.loads(ln)
                    except json.JSONDecodeError:
                        continue
                    try:
                        _bq_event_queue.put_nowait(ev)
                    except queue.Full:
                        all_ok = False
                        break
                if all_ok and lines:
                    try:
                        blob.delete()
                    except Exception:
                        pass
        except Exception as exc:
            logger.debug("cost_tracker: GCS recovery tick: %s", exc)


def _bq_writer_loop() -> None:
    """Background daemon: drain _bq_event_queue in batches of up to 50 rows."""
    while True:
        batch: list[dict] = []
        try:
            batch.append(_bq_event_queue.get(timeout=10))
            while len(batch) < 50:
                try:
                    batch.append(_bq_event_queue.get_nowait())
                except queue.Empty:
                    break
        except queue.Empty:
            continue

        client = _get_bq_client()
        if client and batch and _bq_write_enabled:
            try:
                from app.config import settings  # noqa: PLC0415
                table_id = f"{settings.google_cloud_project_id}.{_BQ_EVENTS_TABLE}"
                errors = client.insert_rows_json(table_id, batch)
                if errors:
                    logger.warning("cost_tracker: BQ insert errors: %s", errors[:3])
                    _spool_dead_letter_batch(batch)
            except Exception as exc:
                logger.warning("cost_tracker: BQ write failed: %s", exc)
                _spool_dead_letter_batch(batch)

        for _ in batch:
            _bq_event_queue.task_done()


# Connected deployments keep cost events durable asynchronously. The local
# reference runtime does not start cloud-writer threads or discover credentials.
_bq_writer_thread: threading.Thread | None = None
_gcs_recovery_thread: threading.Thread | None = None
if _connected_runtime():
    _bq_writer_thread = threading.Thread(
        target=_bq_writer_loop,
        daemon=True,
        name="cost-bq-writer",
    )
    _bq_writer_thread.start()

    _gcs_recovery_thread = threading.Thread(
        target=_gcs_recovery_loop,
        daemon=True,
        name="cost-gcs-recovery",
    )
    _gcs_recovery_thread.start()


# ---------------------------------------------------------------------------
# Session / environment helpers
# ---------------------------------------------------------------------------

_process_session_id: str = str(uuid4())  # unique per process start


def _get_environment() -> str:
    """Detect if running in Cloud Run or script context."""
    if os.environ.get("K_SERVICE"):
        return "cloud_run"
    return "script"


def new_correlation_id() -> str:
    """Generate a UUID to link an ESTIMATE + ACTUAL event pair."""
    return str(uuid4())


def flush_bq_events(timeout: float = 30.0) -> None:
    """
    Block until all queued BQ events are written (or timeout).
    Call at the end of scripts to ensure no events are lost on process exit.
    """
    try:
        _bq_event_queue.join()
    except Exception as exc:
        logger.warning("cost_tracker: flush_bq_events error: %s", exc)

# ---------------------------------------------------------------------------
# Call type taxonomy
# ---------------------------------------------------------------------------

class CallType(str, Enum):
    OCR_IMAGE      = "ocr_image"       # bq_sync: ticket attachment images
    DRIVE_OCR      = "drive_ocr"       # drive_sync: Drive document images
    EMBED_TICKET   = "embed_ticket"    # vectorize_new_tickets()
    EMBED_QUERY    = "embed_query"     # search-time query embeddings
    EMBED_DRIVE    = "embed_drive"     # drive_sync embeddings
    CHAT_PHASE0    = "chat_phase0"     # analyze_query()
    CHAT_PHASE1    = "chat_phase1"     # decide_tool_calls()
    CHAT_PHASE2    = "chat_phase2"     # streaming response
    ANALYTICS_SQL      = "analytics_sql"        # bigquery.py NL→SQL (ticket chat)
    ANALYTICS_DECISION = "analytics_decision"  # analytics.py Phase 1 JSON source decision
    ANALYTICS_ANSWER   = "analytics_answer"    # analytics.py Phase 3 streaming answer
    SUMMARY        = "summary"         # generate_summary()
    DOCS_GAP       = "docs_gap"        # docs_gap.py
    VERTEX_SEARCH  = "vertex_search"   # Discovery Engine search queries (KB + tickets)
    CLOUD_RUN_IDLE = "cloud_run_idle"  # Cloud Run min=1 idle cost (recorded hourly)
    CLOUD_BUILD    = "cloud_build"     # Cloud Build / gcloud run deploy — not auto-metered; admin POST
    UNIVERSAL_SEMANTIC = "universal_semantic"  # Pre-routing semantic normalization
    UNIVERSAL_P1   = "universal_p1"    # Universal Chat JSON-mode tool decision
    UNIVERSAL_P2   = "universal_p2"    # Universal Chat streaming response
    UNIVERSAL_TRANSLATE = "universal_translate"  # Translation without route reasoning
    UNIVERSAL_REVIEW = "universal_review"  # Optional second-pass answer quality review
    UNIVERSAL_DISPATCHER_PLANNER = "universal_dispatcher_planner"  # Bounded Universal Chat AI dispatcher planner
    UNIVERSAL_OUTPUT_CRITIC = "universal_output_critic"  # Shadow-only post-answer quality critic
    CUSTOMER_RISK = "customer_risk"  # Per-ticket customer frustration/sentiment signal


def get_gate_reserve(call_type: CallType, expected_cad: float) -> float:
    """
    Reserved CAD amount passed to check_cost_gate (safety margin vs formula expected).
    See COST_TRACKING_SPEC.md §Layer 1 — ESTIMATE.
    """
    ct = call_type if isinstance(call_type, CallType) else CallType(str(call_type))
    exp = max(0.0, float(expected_cad))
    if ct in (CallType.OCR_IMAGE, CallType.DRIVE_OCR):
        return OCR_GATE_RESERVED_CAD
    if ct == CallType.VERTEX_SEARCH:
        return max(VERTEX_SEARCH_RESERVED_CAD, exp * 2.0)
    if ct in (CallType.EMBED_TICKET, CallType.EMBED_QUERY, CallType.EMBED_DRIVE):
        return max(exp * 2.0, 1e-9)
    return max(exp * 2.0, 1e-9)


# ---------------------------------------------------------------------------
# Unit rates — structured as: USD list price → conversion → effective CAD rate.
#
# Do NOT collapse these into a single number. Always store USD list price AND
# conversion rate so future rate changes can be traced and audited.
#
# Override effective rates via Firestore system/cost_rates if prices change.
# ---------------------------------------------------------------------------

BOOTSTRAP_RATES: dict[str, Any] = {
    # ── USD list prices (official Vertex AI pricing page, cloud.google.com/vertex-ai/generative-ai/pricing)
    # These are the numbers Google publishes. Do NOT use them directly in cost calculations.
    "gemini_input_usd_per_m":    0.30,   # gemini-2.5-flash input tokens
    "gemini_output_usd_per_m":   2.50,   # gemini-2.5-flash output — unified: response + reasoning
    "embedding_usd_per_m":            0.025,   # Example public list-price snapshot
    "vertex_search_usd_per_query":    0.0025,  # Agent Builder Standard edition, per query
    "cloud_run_idle_usd_per_hour":    0.1044,  # 1 vCPU + 2GB RAM, min-instances=1

    # ── Reference billing profile
    # These synthetic bootstrap values make the cost-control workflow runnable
    # without encoding a real billing account or exchange-rate observation.
    "billing_currency":         "CAD",
    "currency_conversion_rate": 1.3500,  # Synthetic USD → CAD example

    # ── Effective rates (= USD list price × currency_conversion_rate)
    # These are the values used in ALL cost calculations (calc_cost, record_usage, etc.).
    # They are deterministic reference defaults, not a billing statement.
    #
    # Unified output pricing (GA): response tokens + reasoning (thinking) tokens → same rate.
    # thinking_budget=0 controls TOKEN VOLUME (prevents extra thinking tokens), NOT the rate.
    "gemini_input_per_m":    0.40500,  # CAD example ($0.30 × 1.35)
    "gemini_output_per_m":   3.37500,  # CAD example ($2.50 × 1.35), unified output
    "gemini_thinking_per_m": 3.37500,  # CAD example, same as output
    "embedding_per_m":         0.03375,  # CAD example ($0.025 × 1.35)
    "vertex_search_per_query": 0.003375, # CAD example ($0.0025 × 1.35)
    "cloud_run_idle_per_hour": 0.14094,  # CAD example ($0.1044 × 1.35)
    "rates_effective_date":  "synthetic-bootstrap-v1",
}


# Model-routing candidate pricing snapshot.
#
# Public reference source:
# https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing
#
# The reference profile assumes a non-global deployment. This table is
# intentionally deterministic and does not call provider APIs at runtime.
MODEL_PRICING_SNAPSHOT_DATE = "synthetic-bootstrap-v1"
MODEL_PRICING_SOURCE_URL = "https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing"


@dataclass(frozen=True)
class ModelTokenPricing:
    model_id: str
    input_usd_per_m: float
    output_usd_per_m: float
    thinking_usd_per_m: float
    price_basis: str


MODEL_TOKEN_PRICING_USD: dict[str, ModelTokenPricing] = {
    "gemini-2.5-flash": ModelTokenPricing(
        model_id="gemini-2.5-flash",
        input_usd_per_m=0.30,
        output_usd_per_m=2.50,
        thinking_usd_per_m=2.50,
        price_basis="standard-global-existing-esip-rate",
    ),
    "gemini-3.1-flash-lite": ModelTokenPricing(
        model_id="gemini-3.1-flash-lite",
        input_usd_per_m=0.495,
        output_usd_per_m=2.97,
        thinking_usd_per_m=2.97,
        price_basis="standard-non-global-us-central1-candidate",
    ),
    "gemini-3.5-flash": ModelTokenPricing(
        model_id="gemini-3.5-flash",
        input_usd_per_m=2.97,
        output_usd_per_m=17.82,
        thinking_usd_per_m=17.82,
        price_basis="standard-non-global-us-central1-candidate",
    ),
}


@dataclass(frozen=True)
class ModelCostEstimate:
    model_id: str
    input_tokens: int
    output_tokens: int
    thinking_tokens: int
    expected_cost_cad: float
    reserved_cost_cad: float
    pricing_snapshot_date: str
    price_basis: str

# Backward-compatible alias (spec name: BOOTSTRAP_RATES)
DEFAULT_RATES = BOOTSTRAP_RATES

_RUNTIME_RATES: Optional[dict[str, Any]] = None
_RUNTIME_RATES_MONO: float = 0.0
_RUNTIME_RATES_TTL_SEC = 3600.0
_bq_rates_load_fail_logged = False


def _runtime_rates() -> dict[str, Any]:
    """Effective CAD + metadata rates: BQ cost_pricing_history bootstrap when available, else BOOTSTRAP_RATES."""
    global _RUNTIME_RATES, _RUNTIME_RATES_MONO, _bq_rates_load_fail_logged
    now = time.monotonic()
    if _RUNTIME_RATES is not None and (now - _RUNTIME_RATES_MONO) < _RUNTIME_RATES_TTL_SEC:
        return _RUNTIME_RATES
    merged = dict(BOOTSTRAP_RATES)
    if not _connected_runtime():
        _RUNTIME_RATES = merged
        _RUNTIME_RATES_MONO = now
        return _RUNTIME_RATES
    loaded = _try_load_rates_from_bq(merged)
    if loaded:
        _RUNTIME_RATES = merged
        _bq_rates_load_fail_logged = False
    else:
        _RUNTIME_RATES = merged
        if not _bq_rates_load_fail_logged:
            logger.warning(
                "cost_tracker: using BOOTSTRAP_RATES — BQ cost_pricing_history has no bootstrap rows "
                "or BQ is unreachable (run scripts/setup_cost_events_bq.py once)."
            )
            _bq_rates_load_fail_logged = True
    _RUNTIME_RATES_MONO = now
    return _RUNTIME_RATES


def _try_load_rates_from_bq(target: dict[str, Any]) -> bool:
    """Merge latest confirmed pricing rows into target. Returns True if at least one row applied."""
    client = _get_bq_client()
    if not client:
        return False
    try:
        from app.config import settings  # noqa: PLC0415
        pid = settings.google_cloud_project_id
        q = f"""
        WITH ranked AS (
          SELECT
            rate_key,
            current_rate,
            rates_version,
            effective_date,
            recorded_at,
            ROW_NUMBER() OVER (
              PARTITION BY rate_key
              ORDER BY effective_date DESC NULLS LAST, recorded_at DESC
            ) AS rn
          FROM `{pid}.esip_reference.cost_pricing_history`
          WHERE confirmed_by_admin = TRUE
            AND rate_key IS NOT NULL
        )
        SELECT rate_key, current_rate, rates_version, effective_date, recorded_at
        FROM ranked
        WHERE rn = 1
        """
        rows = list(client.query(q).result())
        if not rows:
            return False
        latest_marker: tuple[str, str] | None = None
        for row in rows:
            key = row.rate_key
            try:
                if key and key in BOOTSTRAP_RATES and row.current_rate is not None:
                    target[key] = float(row.current_rate)
            except (TypeError, ValueError):
                continue
            marker = (
                str(getattr(row, "effective_date", "") or ""),
                str(getattr(row, "recorded_at", "") or ""),
            )
            if getattr(row, "rates_version", None) and (latest_marker is None or marker > latest_marker):
                latest_marker = marker
                target["rates_effective_date"] = str(row.rates_version)
        return True
    except Exception as exc:
        logger.debug("cost_tracker: rate load from BQ skipped: %s", exc)
        return False


def vertex_search_cost_per_query_cad() -> float:
    """Effective CAD cost per Agent Builder / Discovery Engine query (CIS runtime rates)."""
    return float(_runtime_rates()["vertex_search_per_query"])


def cloud_run_idle_cost_per_hour_cad() -> float:
    """Effective CAD cost per hour of min-instances idle (CIS runtime rates)."""
    return float(_runtime_rates()["cloud_run_idle_per_hour"])


# ---------------------------------------------------------------------------
# Safety thresholds
# ---------------------------------------------------------------------------

# If thinking tokens exceed this on a thinking_budget=0 call: immediate stop.
# A value > 50 means Gemini disobeyed the budget setting — this should never happen.
THINKING_GUARD_THRESHOLD = 50

# How many consecutive Firestore write failures before we stop executing calls.
WRITE_FAILURE_THRESHOLD = 3

# Budget document cache TTL (seconds) — avoid reading Firestore on every call.
_BUDGET_CACHE_TTL = 60

# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class CostDivergenceError(Exception):
    """Raised when actual cost diverges too far from the pre-flight estimate,
    or when unexpected thinking tokens are detected."""

class CostGateError(Exception):
    """Raised when daily/monthly budget would be exceeded, or when cost tracking
    is broken (Firestore write failures) and we cannot enforce limits."""


# ---------------------------------------------------------------------------
# In-memory session buffer
# Survives within a single Cloud Run instance (process lifetime).
# Resets automatically at midnight Eastern time.
# ---------------------------------------------------------------------------

class _SessionBuffer:
    def __init__(self):
        self._lock = threading.Lock()
        self._date: str = ""
        self._cost_usd: float = 0.0

    def _today_key(self) -> str:
        return datetime.now(BUSINESS_TZ).strftime("%Y-%m-%d")

    def add(self, cost_usd: float) -> None:
        with self._lock:
            today = self._today_key()
            if today != self._date:
                self._date = today
                self._cost_usd = 0.0
            self._cost_usd += cost_usd

    def get(self) -> tuple[str, float]:
        """Returns (date_key, accumulated_cost_usd) for today."""
        with self._lock:
            today = self._today_key()
            if today != self._date:
                return today, 0.0
            return self._date, self._cost_usd


_session_buffer = _SessionBuffer()
_write_failure_count = 0   # consecutive Firestore write failures


# ---------------------------------------------------------------------------
# Budget cache (avoid Firestore on every call)
# ---------------------------------------------------------------------------

_budget_cache: Optional[dict] = None
_budget_cache_ts: float = 0.0

# Monthly spend cache (avoids up to 31 Firestore reads per gate check)
_month_cache: dict = {}   # {month_key: (total, monotonic_ts)}
_MONTH_CACHE_TTL = 300    # 5 minutes — monthly limit is coarse-grained, stale reads are fine


def _default_budget() -> dict:
    return {
        "daily_limit_usd":      10.0,
        "monthly_limit_usd":   100.0,
        "soft_cap_pct":         70,
        "hard_cap_pct":         90,
        "divergence_threshold":  2.0,
    }


def get_budget() -> dict:
    """Read system/cost_budget from Firestore (cached 60s). Falls back to defaults."""
    global _budget_cache, _budget_cache_ts
    now = time.monotonic()
    if _budget_cache is not None and (now - _budget_cache_ts) < _BUDGET_CACHE_TTL:
        return _budget_cache
    try:
        from app.services.firestore import get_db
        doc = get_db().document("system/cost_budget").get()
        data = doc.to_dict() or {} if doc.exists else {}
        merged = {**_default_budget(), **data}
        _budget_cache = merged
        _budget_cache_ts = now
        _health_fs_ok()
        return merged
    except Exception as exc:
        logger.warning("cost_tracker: could not read system/cost_budget: %s", exc)
        _health_fs_fail()
        return _budget_cache or _default_budget()


# ---------------------------------------------------------------------------
# Cost calculation
# ---------------------------------------------------------------------------

def calc_cost(
    tokens_in: int,
    tokens_out: int,
    tokens_thinking: int = 0,
    is_embedding: bool = False,
    call_type: Optional[CallType] = None,
) -> float:
    """Return cost in CAD for the given token counts."""
    r = _runtime_rates()
    if is_embedding:
        return (tokens_in / 1_000_000) * float(r["embedding_per_m"])
    cost  = (tokens_in      / 1_000_000) * float(r["gemini_input_per_m"])
    cost += (tokens_out     / 1_000_000) * float(r["gemini_output_per_m"])
    cost += (tokens_thinking / 1_000_000) * float(r["gemini_thinking_per_m"])
    return cost


def estimate_model_cost_cad(
    *,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    thinking_tokens: int = 0,
    reserve_multiplier: float = 2.0,
) -> ModelCostEstimate:
    """Return a deterministic CAD model-cost estimate for routing decisions.

    Existing `calc_cost(...)` remains unchanged for current production callers.
    This helper is for candidate-model routing and escalation guardrails.
    """
    normalized_model = (model_id or "").strip().lower()
    pricing = MODEL_TOKEN_PRICING_USD.get(normalized_model)
    if pricing is None:
        raise ValueError(f"missing model pricing for {model_id!r}")
    if input_tokens < 0 or output_tokens < 0 or thinking_tokens < 0:
        raise ValueError("token counts must be non-negative")
    if reserve_multiplier < 1.0:
        raise ValueError("reserve_multiplier must be >= 1.0")

    rates = _runtime_rates()
    conversion = float(rates.get("currency_conversion_rate", BOOTSTRAP_RATES["currency_conversion_rate"]))
    expected = (
        (input_tokens / 1_000_000) * pricing.input_usd_per_m
        + (output_tokens / 1_000_000) * pricing.output_usd_per_m
        + (thinking_tokens / 1_000_000) * pricing.thinking_usd_per_m
    ) * conversion

    return ModelCostEstimate(
        model_id=pricing.model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        thinking_tokens=thinking_tokens,
        expected_cost_cad=expected,
        reserved_cost_cad=max(expected * reserve_multiplier, 1e-9),
        pricing_snapshot_date=MODEL_PRICING_SNAPSHOT_DATE,
        price_basis=pricing.price_basis,
    )


def estimate_tokens_from_text(text: str) -> int:
    """Rough token estimate from text length. Gemini Flash ≈ 4 chars/token."""
    return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# Today's spent (Firestore + buffer)
# ---------------------------------------------------------------------------

def get_today_spent(strict: bool = False) -> float:
    """Return today's accumulated cost: max(Firestore, buffer).

    Args:
        strict: If True (use in batch scripts), raises CostGateError when Firestore
                is unreachable — prevents the gate from silently approving spend based
                on an empty in-process buffer that knows nothing of prior runs.
                If False (default, use in Cloud Run), falls back to buffer on error.
    """
    date_key, buffer_cost = _session_buffer.get()
    try:
        from app.services.firestore import get_db
        doc = get_db().document(f"cost_counters/{date_key}").get()
        fs_cost = (doc.to_dict() or {}).get("total_cost_usd", 0.0) if doc.exists else 0.0
        _health_fs_ok(daily_counter_read=True)
        return max(fs_cost, buffer_cost)
    except Exception as exc:
        if strict:
            _health_fs_fail()
            raise CostGateError(
                f"Cannot enforce budget: Firestore unreachable ({exc}). "
                "Refusing to proceed — in-process buffer cannot reflect cross-run spend."
            ) from exc
        logger.warning("cost_tracker: could not read today's counter: %s", exc)
        _health_fs_fail()
        return buffer_cost


def get_month_spent() -> float:
    """Return this calendar month's accumulated cost (sum of daily Firestore counters).

    Cached for 5 minutes — monthly limit is coarse-grained, stale reads are acceptable.
    Falls back to cached value on Firestore error (never raises).
    """
    today = datetime.now(BUSINESS_TZ)
    month_key = today.strftime("%Y-%m")

    cached = _month_cache.get(month_key)
    if cached and (time.monotonic() - cached[1]) < _MONTH_CACHE_TTL:
        return cached[0]

    total = 0.0
    current = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    try:
        from app.services.firestore import get_db  # noqa: PLC0415
        db = get_db()
        while current.date() <= today.date():
            key = current.strftime("%Y-%m-%d")
            doc = db.document(f"cost_counters/{key}").get()
            total += (doc.to_dict() or {}).get("total_cost_usd", 0.0) if doc.exists else 0.0
            current += timedelta(days=1)
        with _budget_health._lock:
            _budget_health.monthly_total_available = True
        _health_fs_ok()
    except Exception as exc:
        logger.warning("cost_tracker: could not compute monthly spend: %s", exc)
        _health_fs_fail()
        # Return stale cached value if available; otherwise 0 (gate will use daily limit only)
        return _month_cache.get(month_key, (0.0, 0.0))[0]

    _month_cache[month_key] = (total, time.monotonic())
    return total


# ---------------------------------------------------------------------------
# CIS Layer 1: record pre-action estimate → BQ
# ---------------------------------------------------------------------------

def record_estimate(
    call_type: CallType,
    est_cost_usd: float,
    correlation_id: str,
    est_basis: str = "hardcoded",
    est_input_tokens: int = 0,
    est_output_tokens: int = 0,
    session_id: str = "",
    notes: str = "",
    expected_cost_cad: Optional[float] = None,
    reserved_cost_cad: Optional[float] = None,
) -> None:
    """
    Record a pre-action cost estimate to BQ cost_events (event_type=ESTIMATE).

    Call AFTER check_cost_gate() passes and BEFORE the actual Gemini/embedding call.
    Non-blocking — enqueued to background BQ writer thread (overflow → GCS spool).

    Args:
        call_type:         What kind of call this is (OCR, chat, embed, etc.)
        est_cost_usd:      Formula / measured **expected** cost in CAD (legacy field name)
        correlation_id:    UUID linking this ESTIMATE to the subsequent ACTUAL event
        expected_cost_cad: If set, overrides est_cost_usd for the explicit expected column
        reserved_cost_cad: If set, gate reserve used for this call (else get_gate_reserve)
        est_basis:         measured_avg | formula | hardcoded
    """
    ct = call_type.value if isinstance(call_type, CallType) else str(call_type)
    rates = _runtime_rates()
    expected = float(expected_cost_cad if expected_cost_cad is not None else est_cost_usd)
    reserved = float(reserved_cost_cad if reserved_cost_cad is not None else get_gate_reserve(call_type, expected))

    event = {
        "event_id":            str(uuid4()),
        "correlation_id":      correlation_id,
        "event_type":          "ESTIMATE",
        "call_type":           ct,
        "session_id":          session_id or _process_session_id,
        "environment":         _get_environment(),
        "timestamp":           datetime.now(timezone.utc).isoformat(),
        "est_cost_usd":        expected,
        "expected_cost_cad":   expected,
        "reserved_cost_cad":   reserved,
        "est_basis":           est_basis,
        "est_input_tokens":    est_input_tokens,
        "est_output_tokens":   est_output_tokens,
        "pricing_version":     rates.get("rates_effective_date", "unknown"),
        "price_input_per_m":   rates.get("gemini_input_per_m", 0.0),
        "price_output_per_m":  rates.get("gemini_output_per_m", 0.0),
        "price_thinking_per_m": rates.get("gemini_thinking_per_m", 0.0),
        "notes":               notes,
    }

    _enqueue_bq_event(event)

    logger.debug(
        "cost_tracker ESTIMATE: %s expected=$%.5f reserved=$%.5f corr=%s",
        ct, expected, reserved, correlation_id[:8],
    )


# ---------------------------------------------------------------------------
# CIS Layer 2: record actual usage → Firestore (gate) + BQ (history)
# ---------------------------------------------------------------------------

def record_usage(
    call_type: CallType,
    tokens_in: int,
    tokens_out: int,
    tokens_thinking: int = 0,
    is_embedding: bool = False,
    correlation_id: str = "",
    est_cost_usd: float = 0.0,
    notes: str = "",
    expected_cost_cad: float = 0.0,
) -> float:
    """
    Record actual token usage after a Gemini/embedding call completes.

    1. Calculates cost.
    2. Adds to in-memory session buffer (always succeeds).
    3. Writes to Firestore with retry (up to 3 attempts).
    4. If writes fail 3 times consecutively: raises CostGateError.

    Returns the computed cost in CAD.

    expected_cost_cad: if > 0, used for estimate_error_pct (CIS: expected vs actual, not reserved).
    """
    global _write_failure_count
    cost = calc_cost(tokens_in, tokens_out, tokens_thinking, is_embedding, call_type=call_type)
    _session_buffer.add(cost)

    date_key = datetime.now(BUSINESS_TZ).strftime("%Y-%m-%d")
    ct = call_type.value if isinstance(call_type, CallType) else str(call_type)

    from google.cloud.firestore import Increment  # noqa: PLC0415
    update_data = {
        "total_cost_usd":            Increment(cost),
        "total_tokens_in":           Increment(tokens_in),
        "total_tokens_out":          Increment(tokens_out),
        "total_tokens_thinking":     Increment(tokens_thinking),
        "total_calls":               Increment(1),
        f"by_type.{ct}.cost_usd":   Increment(cost),
        f"by_type.{ct}.tokens_in":  Increment(tokens_in),
        f"by_type.{ct}.tokens_out": Increment(tokens_out),
        f"by_type.{ct}.calls":      Increment(1),
        "updated_at":                datetime.now(BUSINESS_TZ).isoformat(),
    }

    backoff = 0.3
    for attempt in range(1, 4):
        try:
            from app.services.firestore import get_db  # noqa: PLC0415
            db = get_db()
            db.document(f"cost_counters/{date_key}").set(
                update_data, merge=True
            )
            # Also write to hourly bucket (best-effort — failure does not affect gates)
            hour_key = datetime.now(BUSINESS_TZ).strftime("%Y-%m-%d-%H")
            try:
                db.document(f"cost_counters/{hour_key}").set(
                    update_data, merge=True
                )
            except Exception as hex_:
                logger.warning("cost_tracker: hourly bucket write failed (non-critical): %s", hex_)
            _write_failure_count = 0  # reset on success
            _health_fs_ok(daily_counter_read=True)
            break
        except Exception as exc:
            logger.warning(
                "cost_tracker: Firestore write attempt %d/3 failed: %s", attempt, exc
            )
            if attempt < 3:
                time.sleep(backoff)
                backoff *= 3
            else:
                _write_failure_count += 1
                logger.error(
                    "cost_tracker: all 3 write attempts failed. Consecutive failures: %d",
                    _write_failure_count,
                )
                _health_fs_fail()
                if _write_failure_count >= WRITE_FAILURE_THRESHOLD:
                    raise CostGateError(
                        f"Cost tracking unavailable after {_write_failure_count} "
                        "consecutive Firestore failures — stopping to prevent untracked spend."
                    ) from exc

    # CIS Layer 2: write ACTUAL event to BQ (non-blocking, best-effort)
    error_pct: Optional[float] = None
    if correlation_id:
        exp_for_err = expected_cost_cad if expected_cost_cad > 0 else est_cost_usd
        if exp_for_err > 0 and cost > 0:
            error_pct = round((exp_for_err - cost) / cost * 100, 2)

        rates = _runtime_rates()
        actual_event = {
            "event_id":             str(uuid4()),
            "correlation_id":       correlation_id,
            "event_type":           "ACTUAL",
            "call_type":            ct,
            "session_id":           _process_session_id,
            "environment":          _get_environment(),
            "timestamp":            datetime.now(timezone.utc).isoformat(),
            "act_input_tokens":     tokens_in,
            "act_output_tokens":    tokens_out,
            "act_thinking_tokens":  tokens_thinking,
            "act_cost_usd":         cost,
            "actual_cost_cad":      cost,
            "pricing_version":      rates.get("rates_effective_date", "unknown"),
            "price_input_per_m":    rates.get("gemini_input_per_m", 0.0),
            "price_output_per_m":   rates.get("gemini_output_per_m", 0.0),
            "price_thinking_per_m": rates.get("gemini_thinking_per_m", 0.0),
            "estimate_error_pct":   error_pct,
            "notes":                notes,
        }
        _enqueue_bq_event(actual_event)

    logger.debug(
        "cost_tracker: %s — in=%d out=%d think=%d cost=$%.6f%s",
        ct, tokens_in, tokens_out, tokens_thinking, cost,
        f" err={error_pct:+.1f}%" if correlation_id and error_pct is not None else "",
    )
    return cost


# ---------------------------------------------------------------------------
# Flat cost recording (non-token-based: search queries, idle time)
# ---------------------------------------------------------------------------

def record_flat_cost(
    call_type: CallType,
    cost_usd: float,
    units: int = 1,
    *,
    counter_date_key: Optional[str] = None,
) -> float:
    """
    Record a fixed-cost event (not token-based).
    Used for Vertex AI Search queries, Cloud Run idle time, and Cloud Build deploy charges.

    Args:
        call_type: VERTEX_SEARCH, CLOUD_RUN_IDLE, or CLOUD_BUILD
        cost_usd:  Actual cost in USD
        units:     Number of units (queries or hours) for the 'calls' counter
        counter_date_key: If set (YYYY-MM-DD, Eastern calendar day), write only that daily
            `cost_counters/{day}` doc — no hourly bucket (used for billing-import backfill).

    Returns the cost recorded.
    """
    global _write_failure_count
    _session_buffer.add(cost_usd)

    now = datetime.now(BUSINESS_TZ)
    if counter_date_key is not None and (
        len(counter_date_key) != 10 or counter_date_key[4] != "-" or counter_date_key[7] != "-"
    ):
        raise ValueError(f"counter_date_key must be YYYY-MM-DD, got {counter_date_key!r}")
    date_key = counter_date_key if counter_date_key else now.strftime("%Y-%m-%d")
    write_hourly = counter_date_key is None
    ct = call_type.value if isinstance(call_type, CallType) else str(call_type)

    from google.cloud.firestore import Increment  # noqa: PLC0415
    update_data = {
        "total_cost_usd":           Increment(cost_usd),
        "total_calls":              Increment(units),
        f"by_type.{ct}.cost_usd":  Increment(cost_usd),
        f"by_type.{ct}.calls":     Increment(units),
        "updated_at":               now.isoformat(),
    }

    backoff = 0.3
    for attempt in range(1, 4):
        try:
            from app.services.firestore import get_db  # noqa: PLC0415
            db = get_db()
            db.document(f"cost_counters/{date_key}").set(update_data, merge=True)
            if write_hourly:
                hour_key = now.strftime("%Y-%m-%d-%H")
                try:
                    db.document(f"cost_counters/{hour_key}").set(update_data, merge=True)
                except Exception as hex_:
                    logger.warning("cost_tracker: hourly bucket write failed (non-critical): %s", hex_)
            _write_failure_count = 0
            _health_fs_ok(daily_counter_read=True)
            break
        except Exception as exc:
            logger.warning("cost_tracker: flat_cost write attempt %d/3 failed: %s", attempt, exc)
            if attempt < 3:
                time.sleep(backoff)
                backoff *= 3
            else:
                _write_failure_count += 1
                _health_fs_fail()
                if _write_failure_count >= WRITE_FAILURE_THRESHOLD:
                    raise CostGateError(
                        f"Cost tracking unavailable after {_write_failure_count} failures."
                    ) from exc

    rates = _runtime_rates()
    corr = new_correlation_id()
    _enqueue_bq_event({
        "event_id":             str(uuid4()),
        "correlation_id":       corr,
        "event_type":           "ACTUAL",
        "call_type":            ct,
        "session_id":           _process_session_id,
        "environment":          _get_environment(),
        "timestamp":            datetime.now(timezone.utc).isoformat(),
        "act_input_tokens":     0,
        "act_output_tokens":    0,
        "act_thinking_tokens":  0,
        "act_cost_usd":         cost_usd,
        "actual_cost_cad":      cost_usd,
        "pricing_version":      rates.get("rates_effective_date", "unknown"),
        "price_input_per_m":    rates.get("gemini_input_per_m", 0.0),
        "price_output_per_m":   rates.get("gemini_output_per_m", 0.0),
        "price_thinking_per_m": rates.get("gemini_thinking_per_m", 0.0),
        "notes":                (
            f"flat_cost units={units}"
            + (f" counter_date={counter_date_key}" if counter_date_key else "")
        ),
    })

    logger.debug("cost_tracker: %s — units=%d cost=$%.6f", ct, units, cost_usd)
    return cost_usd


# ---------------------------------------------------------------------------
# Divergence check
# ---------------------------------------------------------------------------

def check_divergence(
    estimated_cost: float,
    actual_cost: float,
    call_type: CallType,
    tokens_thinking: int = 0,
    thinking_budget_was_zero: bool = True,
) -> None:
    """
    Compare pre-flight estimate vs actual cost.

    Guard A — Zero-tolerance thinking tokens:
      If thinking_budget_was_zero=True and tokens_thinking > THINKING_GUARD_THRESHOLD:
      raises CostDivergenceError immediately (this is the March 2026 incident pattern).

    Guard B — Ratio check (text calls only, skipped for OCR/drive_ocr):
      If actual_cost > estimated_cost × threshold: raises CostDivergenceError.
      Skipped for image calls because image token counts can't be pre-estimated from text.

    Writes a cost alert to Firestore on divergence.
    """
    ct = call_type.value if isinstance(call_type, CallType) else str(call_type)

    # Guard A: thinking tokens
    if thinking_budget_was_zero and tokens_thinking > THINKING_GUARD_THRESHOLD:
        msg = (
            f"THINKING TOKENS DETECTED on {ct} (budget was 0): "
            f"{tokens_thinking} thinking tokens — cost=${actual_cost:.4f}. "
            "This matches the March 2026 incident pattern. Stopping immediately."
        )
        logger.error("cost_tracker DIVERGENCE: %s", msg)
        _write_cost_alert("thinking_tokens_detected", msg, call_type)
        _send_alert_email("Thinking tokens detected — IMMEDIATE STOP", msg, level="critical")
        raise CostDivergenceError(msg)

    # Guard B: ratio check (skip for image calls)
    image_calls = {CallType.OCR_IMAGE, CallType.DRIVE_OCR}
    if call_type in image_calls:
        return  # can't estimate image tokens reliably from text length

    if estimated_cost <= 0:
        return  # nothing to compare

    budget = get_budget()
    threshold = budget.get("divergence_threshold", 2.0)
    ratio = actual_cost / estimated_cost

    # Only trigger if actual cost is large enough to matter — normal text generation
    # variance (e.g. Detailed vs Brief summary) routinely hits 2x on tiny amounts.
    # The real danger is large absolute cost spikes, not small-amount noise.
    MIN_DIVERGENCE_COST_USD = 0.05

    if ratio > threshold and actual_cost >= MIN_DIVERGENCE_COST_USD:
        msg = (
            f"Cost divergence on {ct}: estimated=${estimated_cost:.4f} "
            f"actual=${actual_cost:.4f} ratio={ratio:.1f}x > threshold={threshold}x. "
            "Stopping batch to prevent uncontrolled spend."
        )
        logger.error("cost_tracker DIVERGENCE: %s", msg)
        _write_cost_alert("cost_divergence", msg, call_type)
        raise CostDivergenceError(msg)


# ---------------------------------------------------------------------------
# Gate check (pre-job or per-batch)
# ---------------------------------------------------------------------------

def check_cost_gate(
    call_type: CallType,
    projected_additional_cost: float = 0.0,
    fail_closed: bool = False,
) -> None:
    """
    Check whether running this call would exceed the daily or monthly hard cap.

    Call BEFORE expensive batch jobs (OCR loops, embedding batches).
    Also useful before any single Gemini call as a lightweight guard.

    Raises CostGateError if daily or monthly hard cap would be exceeded.
    Fires soft cap alert (non-blocking) if a soft cap is reached.

    fail_closed: when True and Firestore budget backend is degraded, block immediately
    (high-cost call types — see COST_TRACKING_SPEC.md §Fail-Closed Behavior).

    All amounts are in CAD (effective billing currency for this GCP account).
    Budget fields named *_usd in Firestore are also in CAD — naming is legacy.
    """
    if fail_closed and get_budget_backend_health().degraded:
        raise CostGateError(
            "Cost gate fail-closed: Firestore budget path is degraded — "
            "refusing high-cost call until the budget backend is healthy."
        )

    budget = get_budget()
    daily_limit  = budget.get("daily_limit_usd",   10.0)
    monthly_limit = budget.get("monthly_limit_usd", 100.0)
    soft_pct = budget.get("soft_cap_pct", 70)
    hard_pct = budget.get("hard_cap_pct", 90)

    today_spent = get_today_spent()
    projected_total = today_spent + projected_additional_cost

    daily_hard = daily_limit  * hard_pct / 100
    daily_soft = daily_limit  * soft_pct / 100

    ct = call_type.value if isinstance(call_type, CallType) else str(call_type)

    # ── Daily hard cap ──────────────────────────────────────────────────────
    if projected_total >= daily_hard:
        msg = (
            f"Daily hard cap reached for {ct}: "
            f"today=C${today_spent:.4f} + projected=C${projected_additional_cost:.4f} "
            f"= C${projected_total:.4f} >= hard cap C${daily_hard:.4f} "
            f"({hard_pct}% of C${daily_limit}/day). Blocking execution."
        )
        logger.error("cost_tracker GATE BLOCKED: %s", msg)
        _write_cost_alert("hard_cap", msg, call_type)
        _send_alert_email("Hard cap reached — platform blocked", msg, level="critical")
        _enforce_hard_cap()
        raise CostGateError(msg)

    # ── Daily soft cap ──────────────────────────────────────────────────────
    if projected_total >= daily_soft:
        msg = (
            f"Daily soft cap reached for {ct}: "
            f"today=C${today_spent:.4f} (soft=C${daily_soft:.4f}, hard=C${daily_hard:.4f}). "
            "Continuing but alerting."
        )
        logger.warning("cost_tracker SOFT CAP: %s", msg)
        _write_cost_alert("soft_cap", msg, call_type, blocking=False)
        _send_alert_email("Soft cap reached", msg, level="warning")

    # ── Monthly hard cap ────────────────────────────────────────────────────
    if monthly_limit > 0:
        month_spent = get_month_spent()
        monthly_hard = monthly_limit * hard_pct / 100
        monthly_soft = monthly_limit * soft_pct / 100
        month_projected = month_spent + projected_additional_cost

        if month_projected >= monthly_hard:
            msg = (
                f"Monthly hard cap reached for {ct}: "
                f"month=C${month_spent:.4f} + projected=C${projected_additional_cost:.4f} "
                f"= C${month_projected:.4f} >= monthly hard cap C${monthly_hard:.4f} "
                f"({hard_pct}% of C${monthly_limit}/month). Blocking execution."
            )
            logger.error("cost_tracker GATE BLOCKED (monthly): %s", msg)
            _write_cost_alert("monthly_hard_cap", msg, call_type)
            _send_alert_email("Monthly hard cap reached — platform blocked", msg, level="critical")
            _enforce_hard_cap()
            raise CostGateError(msg)

        if month_spent >= monthly_soft and today_spent < daily_soft:
            # Only fire monthly soft alert if daily soft hasn't already fired (avoid double-alert)
            msg = (
                f"Monthly soft cap reached for {ct}: "
                f"month=C${month_spent:.4f} (soft=C${monthly_soft:.4f}, hard=C${monthly_hard:.4f}). "
                "Continuing but alerting."
            )
            logger.warning("cost_tracker MONTHLY SOFT CAP: %s", msg)
            _write_cost_alert("monthly_soft_cap", msg, call_type, blocking=False)
            _send_alert_email("Monthly soft cap reached", msg, level="warning")

    logger.debug(
        "cost_tracker gate OK for %s: today=C$%.4f + projected=C$%.4f, daily hard=C$%.4f",
        ct, today_spent, projected_additional_cost, daily_hard,
    )


# ---------------------------------------------------------------------------
# Hard cap enforcement (pause schedulers, block non-admin requests)
# ---------------------------------------------------------------------------

def _enforce_hard_cap() -> None:
    """
    Write hard cap state to Firestore.
    The API layer reads system/cost_alerts and blocks non-admin requests with 503.
    Scheduler pause is handled by the admin panel (IAM not yet granted to SA).
    """
    try:
        from app.services.firestore import get_db  # noqa: PLC0415
        get_db().document("system/cost_alerts").set({
            "hard_cap_active":    True,
            "hard_cap_at":        datetime.now(BUSINESS_TZ).isoformat(),
            "platform_blocked":   True,
        }, merge=True)
    except Exception as exc:
        logger.error("cost_tracker: failed to write hard cap state to Firestore: %s", exc)


# ---------------------------------------------------------------------------
# Email alert (fire-and-forget, never raises)
# ---------------------------------------------------------------------------

def _send_alert_email(subject: str, body: str, level: str = "warning") -> None:
    """Send alert email via email_service. Swallows all exceptions — never blocks execution."""
    try:
        from app.services.email_service import send_alert  # noqa: PLC0415
        send_alert(subject, body, level=level)
    except Exception as exc:
        logger.warning("cost_tracker: failed to send alert email: %s", exc)


# ---------------------------------------------------------------------------
# Alert writer (non-blocking on soft cap)
# ---------------------------------------------------------------------------

def _write_cost_alert(
    alert_type: str,
    message: str,
    call_type: CallType,
    blocking: bool = True,
) -> None:
    """Write a cost alert entry to Firestore system/cost_alerts."""
    try:
        from app.services.firestore import get_db  # noqa: PLC0415
        ct = call_type.value if isinstance(call_type, CallType) else str(call_type)
        alert_doc = {
            "type":       alert_type,
            "call_type":  ct,
            "message":    message,
            "blocking":   blocking,
            "created_at": datetime.now(BUSINESS_TZ).isoformat(),
        }
        db = get_db()
        # Update the rolling summary document
        db.document("system/cost_alerts").set({
            "last_alert_type":    alert_type,
            "last_alert_message": message[:500],
            "last_alert_at":      datetime.now(BUSINESS_TZ).isoformat(),
            "has_active_alert":   True,
        }, merge=True)
        # Also write to a sub-collection for history
        db.collection("system/cost_alerts/history").add(alert_doc)
    except Exception as exc:
        logger.warning("cost_tracker: could not write alert to Firestore: %s", exc)


# ---------------------------------------------------------------------------
# Usage helpers (called from API layer)
# ---------------------------------------------------------------------------

def get_range_summary(from_dt: datetime, to_dt: datetime) -> dict:
    """
    Aggregate cost counters for an arbitrary time range.

    Granularity:
      - range ≤ 48 h → hourly buckets  (cost_counters/{YYYY-MM-DD-HH})
      - range  > 48 h → daily  buckets  (cost_counters/{YYYY-MM-DD})

    Returns totals + per-bucket breakdown for charting.
    Amounts are in CAD (billing currency); legacy *_usd keys remain for compatibility.
    Empty buckets (no data yet) are included as zeros so the chart axis is complete.
    """
    span = to_dt - from_dt
    use_hourly = span.total_seconds() <= 48 * 3600

    try:
        from app.services.firestore import get_db  # noqa: PLC0415
        db = get_db()
    except Exception as exc:
        logger.error("get_range_summary: cannot connect to Firestore: %s", exc)
        return {
            "error": str(exc),
            "currency": "CAD",
            "buckets": [],
            "total_cost_usd": 0.0,
            "total_cost_cad": 0.0,
            "total_calls": 0,
        }

    total_cost: float = 0.0
    total_calls: int = 0
    by_type: dict = {}
    buckets: list = []

    def _merge_by_type(data: dict) -> None:
        # Firestore SDK returns dotted field paths as flat string keys when set() is used
        # (e.g. "by_type.chat_phase0.cost_usd" instead of nested {"by_type": {"chat_phase0": ...}})
        for key, val in data.items():
            if not key.startswith("by_type."):
                continue
            parts = key.split(".")
            if len(parts) != 3:
                continue
            ct, field = parts[1], parts[2]
            if ct not in by_type:
                by_type[ct] = {"cost_usd": 0.0, "cost_cad": 0.0, "calls": 0, "tokens_in": 0, "tokens_out": 0}
            if field in by_type[ct]:
                by_type[ct][field] += val
                if field == "cost_usd":
                    by_type[ct]["cost_cad"] += val

    if use_hourly:
        current = from_dt.replace(minute=0, second=0, microsecond=0)
        while current <= to_dt:
            key = current.strftime("%Y-%m-%d-%H")
            try:
                doc = db.document(f"cost_counters/{key}").get()
                data = doc.to_dict() if doc.exists else {}
            except Exception:
                data = {}
            cost  = data.get("total_cost_usd", 0.0)
            calls = data.get("total_calls", 0)
            total_cost  += cost
            total_calls += calls
            _merge_by_type(data)
            buckets.append({
                "key":      key,
                "label":    current.strftime("%H:00"),
                "cost_usd": round(cost, 6),
                "cost_cad": round(cost, 6),
                "calls":    calls,
            })
            current += timedelta(hours=1)
    else:
        current = from_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        while current.date() <= to_dt.date():
            key = current.strftime("%Y-%m-%d")
            try:
                doc = db.document(f"cost_counters/{key}").get()
                data = doc.to_dict() if doc.exists else {}
            except Exception:
                data = {}
            cost  = data.get("total_cost_usd", 0.0)
            calls = data.get("total_calls", 0)
            total_cost  += cost
            total_calls += calls
            _merge_by_type(data)
            buckets.append({
                "key":      key,
                "label":    current.strftime("%b %d"),
                "cost_usd": round(cost, 6),
                "cost_cad": round(cost, 6),
                "calls":    calls,
            })
            current += timedelta(days=1)

    # Round floats in by_type
    for vals in by_type.values():
        vals["cost_usd"] = round(vals["cost_usd"], 6)
        vals["cost_cad"] = round(vals["cost_cad"], 6)

    return {
        "from_dt":       from_dt.isoformat(),
        "to_dt":         to_dt.isoformat(),
        "granularity":   "hourly" if use_hourly else "daily",
        "currency":       "CAD",
        "total_cost_usd": round(total_cost, 6),
        "total_cost_cad": round(total_cost, 6),
        "total_calls":    total_calls,
        "by_type":        by_type,
        "buckets":        buckets,
    }


def get_daily_summary(date_key: Optional[str] = None) -> dict:
    """
    Read today's (or a specific day's) usage counter from Firestore.
    Returns empty dict with zeros if no data found.
    Amounts are in CAD (billing currency); legacy *_usd keys remain for compatibility.
    """
    if date_key is None:
        date_key = datetime.now(BUSINESS_TZ).strftime("%Y-%m-%d")
    try:
        from app.services.firestore import get_db  # noqa: PLC0415
        doc = get_db().document(f"cost_counters/{date_key}").get()
        data = doc.to_dict() if doc.exists else {}
        if not data:
            data = {"total_cost_usd": 0.0, "total_calls": 0}
        _, buffer_cost = _session_buffer.get()
        # Surface buffer if Firestore is slightly behind
        if buffer_cost > data.get("total_cost_usd", 0.0):
            data["total_cost_usd"] = buffer_cost
            data["_from_buffer"] = True
        data["total_cost_cad"] = data.get("total_cost_usd", 0.0)
        return data
    except Exception as exc:
        logger.warning("cost_tracker: get_daily_summary failed: %s", exc)
        return {"total_cost_usd": 0.0, "total_cost_cad": 0.0, "error": str(exc)}
