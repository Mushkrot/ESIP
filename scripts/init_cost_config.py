"""
One-time setup script: initialize Firestore documents for cost monitoring.

Creates (with merge=True — safe to re-run, won't overwrite existing values):
  system/cost_budget          — daily/monthly limits, soft/hard cap thresholds
  system/notification_config  — alert email recipients

Run from project root:
    python3 scripts/init_cost_config.py
"""

import sys
import pathlib

# Add backend to path so app imports work
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "backend"))

from dotenv import load_dotenv
load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env", override=True)

from app.services.firestore import get_db


def main():
    db = get_db()

    # --- system/cost_budget ---
    budget_ref = db.document("system/cost_budget")
    existing = budget_ref.get()
    if existing.exists:
        print("system/cost_budget already exists — updating only missing fields")

    budget_ref.set({
        "daily_limit_usd":      10.0,
        "monthly_limit_usd":   100.0,
        "soft_cap_pct":         70,
        "hard_cap_pct":         90,
        "divergence_threshold":  2.0,
    }, merge=True)
    print("✓ system/cost_budget set")

    # --- system/notification_config ---
    notif_ref = db.document("system/notification_config")
    existing = notif_ref.get()
    if existing.exists:
        print("system/notification_config already exists — skipping recipients (use Admin Panel to change)")
    else:
        notif_ref.set({
            "recipients": [
                "priya.shah@northstar.example.com",
                "morgan.chen@northstar.example.com",
            ]
        })
        print("✓ system/notification_config created with default recipients")

    # --- Verify ---
    print("\n--- Verification ---")
    budget = db.document("system/cost_budget").get().to_dict()
    print(f"cost_budget: {budget}")
    notif = db.document("system/notification_config").get().to_dict()
    print(f"notification_config: {notif}")
    print("\nDone. Cost monitoring config is ready.")


if __name__ == "__main__":
    main()
