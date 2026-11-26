"""Tests for finding the tab an action was invoked from."""
from __future__ import annotations

import json
import unittest

from eartag.plugin_context import invoked_tab_id


class InvokedTabIdTest(unittest.TestCase):
    def test_context_json_wins(self) -> None:
        env = {"HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"tab_id": "w1:t2"}), "HERDR_TAB_ID": "w9:t9"}
        self.assertEqual(invoked_tab_id(env), "w1:t2")

    def test_falls_back_to_tab_id_env(self) -> None:
        self.assertEqual(invoked_tab_id({"HERDR_TAB_ID": "w1:t3"}), "w1:t3")
        self.assertEqual(invoked_tab_id({"HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"tab_id": None}), "HERDR_TAB_ID": "w1:t3"}), "w1:t3")

    def test_broken_context_is_ignored(self) -> None:
        for raw in ("not json", "[1, 2]", json.dumps({"tab_id": 5}), ""):
            self.assertEqual(invoked_tab_id({"HERDR_PLUGIN_CONTEXT_JSON": raw, "HERDR_TAB_ID": "w1:t1"}), "w1:t1", raw)

    def test_nothing_known(self) -> None:
        self.assertIsNone(invoked_tab_id({}))
        self.assertIsNone(invoked_tab_id({"HERDR_TAB_ID": "  "}))


if __name__ == "__main__":
    unittest.main()
