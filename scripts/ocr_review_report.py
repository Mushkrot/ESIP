"""
Post-reprocessing review report.

Finds all ticket attachment OCR rows that need manual review:
  1. OCR failed during reprocessing — kept old text, no [IMAGE DESCRIPTION:]
  2. Originally skipped/failed (non-image files, too large, etc.)
  3. URL-expired rows (kept old text — might be missing descriptions)

Run after scripts/reprocess_all_ocr.py completes:
  python3 scripts/ocr_review_report.py
"""

import sys
sys.path.insert(0, "backend")
from dotenv import load_dotenv
load_dotenv(override=True)

from app.services.bigquery import _get_client, GCP_PROJECT, BQ_DATASET

client = _get_client()

print("=" * 70)
print("OCR REPROCESSING REVIEW REPORT")
print("=" * 70)

# 1. Summary stats
print("\n--- SUMMARY ---")
rows = list(client.query(f"""
    SELECT
        processing_status,
        COUNTIF(CONTAINS_SUBSTR(ocr_text, '[IMAGE DESCRIPTION:')) AS has_description,
        COUNTIF(NOT CONTAINS_SUBSTR(IFNULL(ocr_text, ''), '[IMAGE DESCRIPTION:')) AS no_description,
        COUNT(*) AS total
    FROM `{GCP_PROJECT}.{BQ_DATASET}.ticket_attachment_ocr`
    GROUP BY processing_status
    ORDER BY processing_status
""").result())
for r in rows:
    print(f"  status={r.processing_status}: total={r.total}, "
          f"has_description={r.has_description}, no_description={r.no_description}")

# 2. Success rows WITHOUT [IMAGE DESCRIPTION:] — these are failed re-OCR (kept old text)
print("\n--- SUCCESS ROWS WITHOUT DESCRIPTION (failed re-OCR, kept old text) ---")
failed_rows = list(client.query(f"""
    SELECT
        content_url,
        ticket_id,
        LENGTH(IFNULL(ocr_text, '')) AS text_len,
        LEFT(IFNULL(ocr_text, ''), 200) AS ocr_preview,
        processed_at
    FROM `{GCP_PROJECT}.{BQ_DATASET}.ticket_attachment_ocr`
    WHERE processing_status = 'success'
      AND NOT CONTAINS_SUBSTR(IFNULL(ocr_text, ''), '[IMAGE DESCRIPTION:')
    ORDER BY ticket_id
""").result())
print(f"  Total: {len(failed_rows)}")
for r in failed_rows:
    print(f"\n  ticket_id={r.ticket_id}")
    print(f"  url={r.content_url}")
    print(f"  text_len={r.text_len}")
    print(f"  preview={repr(r.ocr_preview)}")

# 3. Non-success rows (skip_reason, failed from original processing)
print("\n--- NON-SUCCESS ROWS (originally skipped/failed) ---")
skip_rows = list(client.query(f"""
    SELECT
        content_url,
        ticket_id,
        processing_status,
        skip_reason,
        processed_at
    FROM `{GCP_PROJECT}.{BQ_DATASET}.ticket_attachment_ocr`
    WHERE processing_status != 'success'
    ORDER BY processing_status, ticket_id
    LIMIT 200
""").result())
print(f"  Total: {len(skip_rows)}")
for r in skip_rows:
    print(f"  [{r.processing_status}] ticket_id={r.ticket_id} | reason={r.skip_reason} | url={r.content_url[:80]}")

print("\n" + "=" * 70)
print(f"Total rows needing review: {len(failed_rows)} failed re-OCR + {len(skip_rows)} non-success")
print("=" * 70)
