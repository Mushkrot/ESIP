"""
One-time script: add gdrive_operations_job_aids to Firestore system/data_sources.

Usage:
    cd /workspace/esip
    python scripts/add_gdrive_job_aids_source.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from dotenv import load_dotenv
load_dotenv(override=True)

import firebase_admin
from firebase_admin import credentials, firestore

from app.config import settings


cred = credentials.Certificate(settings.google_application_credentials)
firebase_admin.initialize_app(cred, {"projectId": settings.firebase_project_id})

db = firestore.client()
doc_ref = db.collection("system").document("data_sources")

doc_ref.set({
    "sources": {
        "gdrive_operations_job_aids": {
            "display_name": "Store Operations Job Aids",
            "type": "vertex_ai",
            "enabled": True,
            "exclude_path_prefixes": [],
        }
    }
}, merge=True)

print("Added gdrive_operations_job_aids to Firestore system/data_sources")
doc = doc_ref.get().to_dict() or {}
for sid, cfg in sorted((doc.get("sources") or {}).items()):
    status = "ENABLED" if cfg.get("enabled") else "DISABLED"
    print(f"  {status:<8} {sid}: {cfg.get('display_name')}")
