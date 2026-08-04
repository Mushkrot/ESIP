"""
One-time script: add reference_support_docs to Firestore system/data_sources.
Run once after Google Drive connector is set up in Vertex AI Agent Builder.

Usage:
    cd /workspace/esip
    python scripts/add_gdrive_source.py
"""
import sys
import os
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

doc_ref.update({
    "sources.reference_support_docs": {
        "display_name": "SD Documentation (Google Drive)",
        "type": "vertex_ai",
        "enabled": True,
    }
})

print("✅ Added reference_support_docs to Firestore system/data_sources")
doc = doc_ref.get().to_dict()
for sid, cfg in doc.get("sources", {}).items():
    status = "✅" if cfg.get("enabled") else "☐ "
    print(f"  {status} {sid}: {cfg.get('display_name')}")
