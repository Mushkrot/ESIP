#!/bin/bash
#
# Full rebuild pipeline for a controlled Google Cloud VM run.
# Sequence: backfill inline images → OCR → truncate embeddings → vectorize → reset cursor
#
# Start: nohup bash scripts/vm_rebuild_pipeline.sh > /tmp/rebuild.log 2>&1 &
# Monitor: tail -f /tmp/rebuild.log
# Done when log shows: "=== ALL DONE ==="
#
set -e
cd "$(dirname "$0")/.."

source venv/bin/activate
export PYTHONUNBUFFERED=1

echo "=================================================="
echo "REBUILD PIPELINE STARTING: $(date)"
echo "=================================================="

echo ""
echo "=== STEP 1/4: Backfill inline images into ticket_attachments (~3.5h)..."
echo "    Resume-safe: already-processed tickets are skipped via checkpoint."
python3 -u scripts/backfill_inline_images.py --workers 5
echo "=== STEP 1 DONE: $(date)"

echo ""
echo "=== STEP 2/4: OCR all unprocessed images via Gemini Vision (~30h)..."
echo "    Resume-safe: already-OCR'd images are skipped via LEFT JOIN."
python3 -u scripts/ocr_attachments.py --workers 20
echo "=== STEP 2 DONE: $(date)"

echo ""
echo "=== STEP 3/4: Truncate ticket_embeddings and re-vectorize all tickets (~1h)..."
python3 -u - << 'PYEOF'
import sys
sys.path.insert(0, 'backend')
from dotenv import load_dotenv
load_dotenv('.env', override=True)
from app.config import settings
from google.cloud import bigquery
from google.oauth2 import service_account

# On VM, use Application Default Credentials (service account attached to VM)
# No credentials file needed — gcloud auth is handled via VM's service account
try:
    creds = service_account.Credentials.from_service_account_file(
        settings.google_application_credentials,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    client = bigquery.Client(project=settings.google_cloud_project_id, credentials=creds)
except Exception:
    # On VM: use Application Default Credentials
    client = bigquery.Client(project=settings.google_cloud_project_id)

client.query(
    f"TRUNCATE TABLE `{settings.google_cloud_project_id}.esip_reference.ticket_embeddings`"
).result()
print("ticket_embeddings truncated.")
PYEOF

python3 -u - << 'PYEOF'
import sys
sys.path.insert(0, 'backend')
from dotenv import load_dotenv
load_dotenv('.env', override=True)
from app.services.bq_sync import vectorize_new_tickets

result = vectorize_new_tickets(progress_cb=lambda m: print(m))
print(f"Vectorization done: embedded={result['embedded']} errors={len(result['errors'])}")
if result['errors']:
    print(f"First errors: {result['errors'][:3]}")
PYEOF
echo "=== STEP 3 DONE: $(date)"

echo ""
echo "=== STEP 4/4: Reset Firestore sync cursor to now..."
python3 -u - << 'PYEOF'
import sys
sys.path.insert(0, 'backend')
from dotenv import load_dotenv
load_dotenv('.env', override=True)
from datetime import datetime, timezone
from app.services import firestore as fs_service

now_unix = int(datetime.now(timezone.utc).timestamp())
fs_service.set_sync_cursor(now_unix)
print(f"Cursor reset to {datetime.fromtimestamp(now_unix, tz=timezone.utc).isoformat()}")
PYEOF
echo "=== STEP 4 DONE: $(date)"

echo ""
echo "=================================================="
echo "=== ALL DONE: $(date)"
echo "=================================================="
echo ""
echo "Next steps:"
echo "  1. Verify in BQ: SELECT COUNT(*) FROM esip_reference.ticket_attachment_ocr"
echo "  2. Verify embeddings: SELECT COUNT(*) FROM esip_reference.ticket_embeddings"
echo "  3. Delete this VM: gcloud compute instances delete esip-rebuild --zone=us-central1-a --project=esip-reference-project --quiet"
