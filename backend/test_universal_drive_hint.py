"""Universal Chat Drive hint policy tests.

Run from project root:
scripts/backend_python.sh backend/test_universal_drive_hint.py
"""

from __future__ import annotations

import unittest

from app.routers import universal


class UniversalDriveHintPolicyTest(unittest.TestCase):
    def test_customer_risk_route_skips_drive_hint(self):
        allowed, reason = universal._drive_hint_policy(
            any_drive_search_enabled=True,
            force_drive_tabular=True,
            selected_executor="customer_risk_analytics",
            tools=[],
        )

        self.assertFalse(allowed)
        self.assertEqual(reason, "customer_risk_analytics")

    def test_ticket_semantic_and_direct_ticket_routes_skip_drive_hint(self):
        for executor in ("ticket_semantic_search", "direct_ticket_context"):
            with self.subTest(executor=executor):
                allowed, reason = universal._drive_hint_policy(
                    any_drive_search_enabled=True,
                    force_drive_tabular=True,
                    selected_executor=executor,
                    tools=[],
                )

                self.assertFalse(allowed)
                self.assertEqual(reason, executor)

    def test_ticket_analytics_route_skips_drive_hint(self):
        allowed, reason = universal._drive_hint_policy(
            any_drive_search_enabled=True,
            force_drive_tabular=True,
            selected_executor="ticket_analytics",
            tools=[],
        )

        self.assertFalse(allowed)
        self.assertEqual(reason, "ticket_analytics")

    def test_drive_tabular_fallback_still_allows_drive_hint(self):
        allowed, reason = universal._drive_hint_policy(
            any_drive_search_enabled=True,
            force_drive_tabular=True,
            selected_executor="phase1_tools",
            tools=[],
        )

        self.assertTrue(allowed)
        self.assertIsNone(reason)

    def test_explicit_drive_lookup_uses_normal_drive_path_not_hint(self):
        allowed, reason = universal._drive_hint_policy(
            any_drive_search_enabled=True,
            force_drive_tabular=True,
            selected_executor="phase1_tools",
            tools=["search_drive_exact"],
            has_explicit_drive_lookup=True,
        )

        self.assertFalse(allowed)
        self.assertEqual(reason, "explicit_drive_lookup_present")


if __name__ == "__main__":
    unittest.main(verbosity=2)
