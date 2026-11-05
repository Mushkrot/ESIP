"""
Application settings loaded from environment variables.
Uses Pydantic Settings — all values can be overridden by the .env file.
"""

import pathlib
from typing import Literal

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.reference_data.fixtures import IDENTITY_DOMAIN, PLATFORM_ADMIN_EMAIL

# Load .env with override=True so project-specific values (e.g. GOOGLE_APPLICATION_CREDENTIALS)
# take precedence over any conflicting system environment variables on the developer's machine.
_env_file = pathlib.Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(_env_file, override=True)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file="../.env",
        env_file_encoding="utf-8",
        extra="ignore",  # ignore unknown env vars (e.g. DIALOGFLOW_*)
    )

    # --- Zendesk ---
    zendesk_subdomain: str = "example"
    zendesk_email: str = PLATFORM_ADMIN_EMAIL
    zendesk_api_token: str = ""

    # --- Google Cloud ---
    # Reference is the credential-free local runtime. Connected enables the
    # explicitly configured provider adapters used by a hosted deployment.
    runtime_profile: Literal["reference", "connected"] = "reference"
    google_cloud_project_id: str = "esip-reference-project"
    google_application_credentials: str = ""  # Optional: not needed on GCP VMs (uses ADC)
    gcp_billing_export_table: str = (
        "esip-reference-billing.Reference_Billing_Export."
        "gcp_billing_export_v1_REFERENCE"
    )

    # --- Gemini (Vertex AI) ---
    # Model runs through Vertex AI using the service account — no separate API key needed.
    # us-central1 is required for embedding models (global endpoint does not support them).
    # Update gemini_model when the next GA generation is released (e.g. gemini-3-flash).
    gemini_model: str = "gemini-2.5-flash"
    gemini_location: str = "us-central1"

    # --- Vertex AI Search (Discovery Engine) ---
    vertex_ai_data_store_id: str = "reference-support-knowledge"
    vertex_ai_tickets_data_store_id: str = "reference-support-tickets"
    vertex_ai_gdrive_data_store_id: str = "reference-support-documents"
    vertex_ai_location: str = "global"

    # --- Firebase / Auth ---
    # Same GCP project is also the Firebase project.
    firebase_project_id: str = "esip-reference-project"
    # Comma-separated list of allowed email domains; Firestore allowlist is the second layer.
    allowed_email_domains: str = IDENTITY_DOMAIN

    @property
    def allowed_email_domains_list(self) -> list[str]:
        return [d.strip() for d in self.allowed_email_domains.split(",") if d.strip()]

    # --- Zendesk App Framework ---
    # Shared secret from the app manifest settings — used to validate Zendesk JWTs
    # Set in Cloud Run env vars. Leave empty to disable ZAF auth endpoint.
    zendesk_app_secret: str = ""

    # --- Admin sync ---
    # Secret header value used by Cloud Scheduler to call /api/admin/sync
    # Set in Cloud Run env vars + Cloud Scheduler X-Sync-Secret custom header
    sync_secret: str = ""

    # --- Admin architecture documentation ---
    # Private GCS bucket containing rendered architecture diagram bundles.
    architecture_docs_bucket: str = "esip-architecture"
    architecture_docs_enabled: bool = True

    # --- Service Desk KPI snapshot artifacts ---
    # Full snapshots are stored in GCS so scheduled runs survive local restarts.
    reference_kpi_reports_bucket: str = "esip-reference-data"
    reference_kpi_reports_prefix: str = "reference_kpi_reports"

    # --- Universal Chat answer validation ---
    # Runtime kill switch for automatic second-level answer validation. Manual
    # Improve Answer remains available even when this is disabled.
    universal_ai_validator_enabled: bool = True
    # Separate kill switch and hard caps for the automatic repair loop that can
    # revalidate repaired answers. Manual Improve Answer and the first validator
    # pass remain controlled by their own settings.
    universal_ai_repair_loop_enabled: bool = True
    universal_ai_repair_loop_max_repairs: int = 2
    universal_ai_repair_loop_max_validations: int = 3

    # --- Universal Chat model routing ---
    # Empty per-surface overrides use the global fallback model.
    universal_validator_model: str = ""
    universal_review_model: str = ""
    universal_output_critic_shadow_enabled: bool = False
    universal_output_critic_model: str = ""
    universal_output_critic_timeout_ms: int = 2500
    universal_output_critic_repair_enabled: bool = False
    universal_output_critic_repair_min_confidence: float = 0.82
    universal_output_critic_repair_allowed_kinds: str = (
        "raw_artifact_strip,false_no_data_or_tool_error,followup_scope_loss,resolution_claim_guard"
    )
    universal_model_escalation_enabled: bool = False
    universal_model_escalation_model: str = ""
    universal_model_escalation_allowed_actions: str = (
        "rerun_bq,zendesk_fallback,drive_exact_retry,rebuild_scoped_evidence"
    )
    universal_model_escalation_max_per_turn: int = 1

    # --- Email notifications (cost alerts, soft/hard cap) ---
    # Primary transport is Gmail API with domain-wide delegation as ai-ops.
    # SendGrid remains available only as an explicit legacy/fallback transport.
    email_transport: str = "gmail_api"
    gmail_sender_email: str = PLATFORM_ADMIN_EMAIL
    gmail_sender_name: str = "ESIP Alerts"
    gmail_delegated_subject: str = PLATFORM_ADMIN_EMAIL
    email_sendgrid_fallback_enabled: bool = False
    sendgrid_api_key: str = ""       # SG.xxxxx — set in .env and Cloud Run env vars
    alert_from_email: str = PLATFORM_ADMIN_EMAIL

    # --- NICE CXone / RingCentral Contact Center ---
    # Read-only access used for Service Desk telephony KPI reporting.
    cxone_access_key_id: str = ""
    cxone_access_key_secret: str = ""
    cxone_region: str = "na1"
    cxone_tenant_id: str = ""
    cxone_user_id: str = ""
    cxone_account_id: str = ""

    # --- Backend runtime ---
    # Comma-separated list of allowed CORS origins.
    # Example: "http://localhost:3000,https://app.esip.example.com"
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000,http://127.0.0.1:4567"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def zendesk_base_url(self) -> str:
        return f"https://{self.zendesk_subdomain}.zendesk.com/api/v2"

    @property
    def zendesk_auth(self) -> tuple[str, str]:
        """HTTP Basic Auth tuple for Zendesk API."""
        return (f"{self.zendesk_email}/token", self.zendesk_api_token)


# Single shared instance — imported everywhere as `from app.config import settings`
settings = Settings()
