#!/usr/bin/env python3
"""Regression tests for the project-owned UTM-8 visual navigator."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
ROUTER_ROOT = ROOT / "scripts" / "utm_8_screen_workflow_router"
MODULE_PATH = ROUTER_ROOT / "screen_workflow_router.py"
WORKFLOW_PATH = ROUTER_ROOT / "workflows" / "apple-account-password.json"


def load_router():
    if not MODULE_PATH.is_file():
        raise AssertionError(f"missing project-owned visual router: {MODULE_PATH}")
    spec = importlib.util.spec_from_file_location("utm_8_screen_workflow_router", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class UTM8VisualRouterTests(unittest.TestCase):
    def test_project_workflow_has_three_owned_templates_and_three_second_closure(self) -> None:
        router = load_router()
        name, steps = router.load_workflow(WORKFLOW_PATH)

        self.assertEqual(name, "apple-account-password")
        self.assertEqual(len(steps), 3)
        self.assertTrue(all(step["click"] for step in steps))
        self.assertTrue(all(step["wait_after_click_seconds"] >= 3 for step in steps))
        self.assertGreaterEqual(router.CLICK_REVERIFY_DELAY_SECONDS, 3)
        self.assertTrue(all(Path(step["template"]).is_file() for step in steps))
        self.assertTrue(Path(steps[0]["verify_template"]).is_file())
        self.assertTrue(Path(steps[1]["verify_template"]).is_file())
        self.assertIsNone(steps[2]["verify_template"])

    def test_router_rejects_more_than_one_threshold_match(self) -> None:
        router = load_router()
        with self.assertRaisesRegex(router.TemplateMatchError, "匹配不唯一"):
            router.select_unique_match(
                [
                    {"score": 0.91, "x": 10, "y": 10},
                    {"score": 0.88, "x": 400, "y": 300},
                ],
                threshold=0.60,
            )

    def test_router_accepts_exactly_one_threshold_match(self) -> None:
        router = load_router()
        selected = router.select_unique_match(
            [
                {"score": 0.91, "x": 10, "y": 10},
                {"score": 0.42, "x": 400, "y": 300},
            ],
            threshold=0.60,
        )
        self.assertEqual(selected["score"], 0.91)


if __name__ == "__main__":
    unittest.main()
