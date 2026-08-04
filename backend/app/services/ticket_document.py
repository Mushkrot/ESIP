"""
Ticket document generator for full-content ticket search.

Generates plain-text representations of tickets with all fields, all comments
chronologically, and OCR text interleaved inline — for upload to Vertex AI Agent Builder
and for computing full-content embeddings for same-store cosine similarity search.
"""

import logging
from datetime import datetime

from app.services.bigquery import _get_client, GCP_PROJECT, BQ_DATASET

logger = logging.getLogger(__name__)


def generate_ticket_document(ticket_id: int) -> str:
    """
    Build a full-content plain-text document for a ticket.

    Fetches from BigQuery:
      - ticket fields from `tickets`
      - all comments from `ticket_comments` (ordered by created_at)
      - OCR text from `ticket_attachment_ocr` keyed by comment_id

    Returns a plain-text string in the canonical format:
      TICKET #ID | STATUS: ... | CREATED: ... | SOLVED: ...
      STORE: ... | ORG: ... | BRAND: ... | CHANNEL: ...
      ...
      COMMENTS:
      [datetime | Author (public/internal)]
      Comment body...
      [IMAGE: filename.png]
      OCR text...
    """
    client = _get_client()

    # Fetch ticket metadata
    ticket_sql = f"""
    SELECT
        id, subject, status, brand, store_id, org_id, org_name,
        created_at, updated_at, solved_at,
        via_channel, issue_category, product_type, caused_by,
        resolution_type, escalated, vendor_dispatched, vendor_escalation,
        symptoms, resolution
    FROM `{GCP_PROJECT}.{BQ_DATASET}.tickets`
    WHERE id = @ticket_id
    """
    from google.cloud import bigquery as bq
    ticket_rows = list(client.query(
        ticket_sql,
        job_config=bq.QueryJobConfig(
            query_parameters=[bq.ScalarQueryParameter("ticket_id", "INT64", ticket_id)]
        )
    ).result())

    if not ticket_rows:
        raise ValueError(f"Ticket {ticket_id} not found in BigQuery")

    t = ticket_rows[0]

    def _fmt_date(dt) -> str:
        if dt is None:
            return "—"
        if isinstance(dt, datetime):
            return dt.strftime("%Y-%m-%d")
        return str(dt)[:10]

    def _val(v) -> str:
        if v is None or (isinstance(v, str) and not v.strip()):
            return "—"
        return str(v)

    # Header block
    lines = [
        f"TICKET #{t.id} | STATUS: {_val(t.status)} | CREATED: {_fmt_date(t.created_at)} | SOLVED: {_fmt_date(t.solved_at)}",
        f"STORE: {_val(t.store_id)} | ORG: {_val(t.org_name)} | BRAND: {_val(t.brand)} | CHANNEL: {_val(t.via_channel)}",
        f"CATEGORY: {_val(t.issue_category)} | PRODUCT: {_val(t.product_type)} | CAUSED-BY: {_val(t.caused_by)}",
        f"RESOLUTION-TYPE: {_val(t.resolution_type)} | ESCALATED: {'yes' if t.escalated else 'no'} | VENDOR: {'yes' if t.vendor_dispatched else 'no'}",
        f"SYMPTOMS: {_val(t.symptoms)}",
        f"RESOLUTION: {_val(t.resolution)}",
    ]

    if t.subject:
        lines.insert(0, f"SUBJECT: {t.subject}")

    lines.append("")
    lines.append("COMMENTS:")
    lines.append("")

    # Fetch comments ordered chronologically
    comments_sql = f"""
    SELECT
        comment_id, created_at, is_internal, author_name, body
    FROM `{GCP_PROJECT}.{BQ_DATASET}.ticket_comments`
    WHERE ticket_id = @ticket_id
    ORDER BY created_at ASC
    """
    comment_rows = list(client.query(
        comments_sql,
        job_config=bq.QueryJobConfig(
            query_parameters=[bq.ScalarQueryParameter("ticket_id", "INT64", ticket_id)]
        )
    ).result())

    # Fetch OCR texts grouped by comment_id
    ocr_sql = f"""
    SELECT
        ta.comment_id,
        ta.file_name,
        ocr.ocr_text
    FROM `{GCP_PROJECT}.{BQ_DATASET}.ticket_attachments` ta
    LEFT JOIN `{GCP_PROJECT}.{BQ_DATASET}.ticket_attachment_ocr` ocr
        ON ta.content_url = ocr.content_url
    WHERE ta.ticket_id = @ticket_id
      AND ta.is_image = TRUE
      AND (ocr.processing_status IS NULL OR ocr.processing_status = 'success')
    ORDER BY ta.created_at ASC
    """
    ocr_rows = list(client.query(
        ocr_sql,
        job_config=bq.QueryJobConfig(
            query_parameters=[bq.ScalarQueryParameter("ticket_id", "INT64", ticket_id)]
        )
    ).result())

    # Group OCR by comment_id (None = no parent comment)
    ocr_by_comment: dict[int | None, list[tuple[str, str | None]]] = {}
    for row in ocr_rows:
        key = row.comment_id
        if key not in ocr_by_comment:
            ocr_by_comment[key] = []
        ocr_by_comment[key].append((row.file_name or "image", row.ocr_text))

    # Render each comment with its OCR inline
    for c in comment_rows:
        vis = "internal" if c.is_internal else "public"
        dt_str = c.created_at.strftime("%Y-%m-%d %H:%M") if c.created_at else "?"
        author = c.author_name or "Unknown"
        lines.append(f"[{dt_str} | {author} ({vis})]")
        if c.body:
            lines.append(c.body.strip())

        # Inline OCR for this comment's images
        for fname, ocr_text in ocr_by_comment.get(c.comment_id, []):
            lines.append(f"[IMAGE: {fname}]")
            if ocr_text and ocr_text.strip():
                lines.append(ocr_text.strip())

        lines.append("")

    # Standalone images not attached to a comment
    for fname, ocr_text in ocr_by_comment.get(None, []):
        lines.append(f"[IMAGE: {fname}]")
        if ocr_text and ocr_text.strip():
            lines.append(ocr_text.strip())
        lines.append("")

    return "\n".join(lines)
