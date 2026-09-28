import re
import unittest
from pathlib import Path
from unittest.mock import patch

import spine

CURUNIR = "jalemieux/curunir"


class TestNoPhaseImports(unittest.TestCase):
    def test_spine_never_imports_code_factory(self):
        # The boundary rule from agentic-stack-v2.md: spine is imported by
        # phase code, never the other way around.
        source = Path(spine.__file__).read_text()
        self.assertIsNone(
            re.search(r"^\s*(import|from)\s+code_factory", source, re.M),
            "spine.py must not import code_factory or any phase logic",
        )


class TestSchema(unittest.TestCase):
    def test_wip_limit_is_ten(self):
        self.assertEqual(spine.WIP_LIMIT, 10)
        self.assertEqual(spine.SCHEMA["wip_limit"], 10)

    def test_label_vocabulary_includes_failed_and_planning(self):
        labels = set(spine.SCHEMA["labels"].values())
        self.assertIn("bot:failed", labels)
        self.assertIn("bot:planning", labels)
        self.assertIn("bot:plan-proposed", labels)
        self.assertIn("bot:plan-accepted", labels)
        self.assertIn("bot:in-progress", labels)
        self.assertIn("bot:review-requested", labels)

    def test_branch_pattern_documented(self):
        self.assertEqual(spine.SCHEMA["branch_pattern"], "bot/<issue>-<slug>")


class TestLegalTransition(unittest.TestCase):
    def test_happy_path(self):
        self.assertTrue(spine.legal_transition("bot:planning", "bot:plan-proposed"))
        self.assertTrue(spine.legal_transition("bot:plan-proposed", "bot:plan-accepted"))
        self.assertTrue(spine.legal_transition("bot:plan-accepted", "bot:review-requested"))

    def test_design_objection_reopens_plan(self):
        self.assertTrue(spine.legal_transition("bot:review-requested", "bot:plan-proposed"))

    def test_no_skipping_states(self):
        self.assertFalse(spine.legal_transition("bot:plan-proposed", "bot:review-requested"))
        self.assertFalse(spine.legal_transition("bot:planning", "bot:plan-accepted"))
        self.assertFalse(spine.legal_transition("bot:plan-accepted", "bot:plan-proposed"))

    def test_any_state_may_fail(self):
        for old in spine.SCHEMA["labels"].values():
            self.assertTrue(spine.legal_transition(old, "bot:failed"))


class TestClassify(unittest.TestCase):
    def test_tier_a_exact_file(self):
        self.assertEqual(spine.classify(["src/llm.py"], CURUNIR), "A")

    def test_tier_a_nested_path(self):
        self.assertEqual(
            spine.classify(["src/agent/planner/steps/refine.py"], CURUNIR), "A"
        )

    def test_tier_a_portal_ws_prefix(self):
        self.assertEqual(spine.classify(["portal/ws_server.py"], CURUNIR), "A")

    def test_portal_non_ws_file_is_tier_c(self):
        # `portal/ws_*` requires the literal `ws_` prefix — wsgi.py has no
        # underscore after `ws` and must not ride the critical-path tier.
        self.assertEqual(spine.classify(["portal/wsgi.py"], CURUNIR), "C")

    def test_sibling_of_agent_dir_is_tier_c(self):
        # `src/agent/**` needs the slash — `src/agent_utils.py` is outside it.
        self.assertEqual(spine.classify(["src/agent_utils.py"], CURUNIR), "C")

    def test_tier_b_nested(self):
        self.assertEqual(spine.classify(["skills/foo/SKILL.md"], CURUNIR), "B")
        self.assertEqual(spine.classify(["personas/bard.md"], CURUNIR), "B")

    def test_default_tier_c(self):
        self.assertEqual(spine.classify(["README.md", "docs/notes.md"], CURUNIR), "C")

    def test_mixed_diff_takes_strictest_b_over_c(self):
        self.assertEqual(
            spine.classify(["README.md", "skills/foo/SKILL.md"], CURUNIR), "B"
        )

    def test_mixed_diff_takes_strictest_a_over_b_and_c(self):
        self.assertEqual(
            spine.classify(
                ["skills/foo/SKILL.md", "src/tools/dispatcher.py", "README.md"],
                CURUNIR,
            ),
            "A",
        )

    def test_unconfigured_repo_defaults_to_c(self):
        self.assertEqual(spine.classify(["src/llm.py"], "someone/elsewhere"), "C")

    def test_empty_diff_is_plan_sentinel(self):
        # 56% of the curunir backlog is empty-diff plan drafts (spike 0.2);
        # they must never classify as an auto-mergeable tier.
        self.assertEqual(spine.classify([], CURUNIR), spine.PLAN)
        self.assertEqual(spine.classify([], "someone/elsewhere"), spine.PLAN)
        self.assertNotIn(spine.PLAN, spine.TIERS)

    def test_channels_transport_layer_is_tier_a(self):
        # spike 0.2: security fixes in src/channels/** classified C before.
        self.assertEqual(spine.classify(["src/channels/ws.py"], CURUNIR), "A")
        self.assertEqual(spine.classify(["src/channels/email/inbound.py"], CURUNIR), "A")

    def test_tool_schemas_and_delegate_are_tier_a(self):
        self.assertEqual(spine.classify(["src/tools/schemas.py"], CURUNIR), "A")
        self.assertEqual(spine.classify(["src/tools/delegate.py"], CURUNIR), "A")

    def test_other_tool_shims_stay_tier_c(self):
        self.assertEqual(spine.classify(["src/tools/weather.py"], CURUNIR), "C")

    def test_run_py_is_tier_a(self):
        self.assertEqual(spine.classify(["run.py"], CURUNIR), "A")


class TestSecurityOverride(unittest.TestCase):
    def test_security_title_forces_tier_a_regardless_of_globs(self):
        self.assertEqual(
            spine.classify(["README.md"], CURUNIR, title="[security] path traversal fix"),
            "A",
        )

    def test_security_title_is_case_insensitive(self):
        self.assertEqual(
            spine.classify(["docs/x.md"], CURUNIR, title="[Security] harden rekey"), "A"
        )

    def test_security_label_dicts_force_tier_a(self):
        self.assertEqual(
            spine.classify(["README.md"], CURUNIR, labels=[{"name": "security"}]), "A"
        )

    def test_security_label_strings_force_tier_a(self):
        self.assertEqual(
            spine.classify(["README.md"], CURUNIR, labels=["security-fix"]), "A"
        )

    def test_override_applies_in_unconfigured_repos(self):
        self.assertEqual(
            spine.classify(["x.md"], "someone/elsewhere", title="[security] fix"), "A"
        )

    def test_override_beats_plan_sentinel(self):
        # A security-flagged plan draft goes to human review, not the
        # plan-only bucket — erring on the reviewed side.
        self.assertEqual(spine.classify([], CURUNIR, title="[security] plan"), "A")

    def test_plain_title_and_labels_do_not_override(self):
        self.assertEqual(
            spine.classify(["README.md"], CURUNIR,
                           title="fix docs", labels=[{"name": "bug"}]),
            "C",
        )


class TestBranchParsing(unittest.TestCase):
    def test_extracts_issue_number(self):
        self.assertEqual(spine._issue_num_from_branch("bot/42-fix-the-bug"), 42)

    def test_non_bot_branch(self):
        self.assertIsNone(spine._issue_num_from_branch("feature/42-fix"))

    def test_missing_number(self):
        self.assertIsNone(spine._issue_num_from_branch("bot/fix-the-bug"))

    def test_number_without_slug_separator(self):
        self.assertIsNone(spine._issue_num_from_branch("bot/42"))

    def test_empty_and_none(self):
        self.assertIsNone(spine._issue_num_from_branch(""))
        self.assertIsNone(spine._issue_num_from_branch(None))


class TestSlugifyRoundTrip(unittest.TestCase):
    def test_slug_produces_parseable_branch(self):
        slug = spine.slugify("Fix the Bug! (v2)")
        self.assertEqual(spine._issue_num_from_branch(f"bot/7-{slug}"), 7)

    def test_truncates_at_40(self):
        self.assertEqual(len(spine.slugify("a" * 60)), 40)


class TestOpenPlanCount(unittest.TestCase):
    @patch("spine.gh_json")
    def test_counts_only_verified_labels(self, mock_gh_json):
        # The search index is eventually consistent — a stale hit whose real
        # labels dropped bot:plan-proposed must not count toward the WIP limit.
        mock_gh_json.return_value = [
            {"number": 1, "labels": [{"name": "bot:plan-proposed"}]},
            {"number": 2, "labels": [{"name": "bot:plan-accepted"}]},
            {"number": 3, "labels": [{"name": "bot:plan-proposed"}]},
        ]
        self.assertEqual(spine.open_plan_count("owner/repo"), 2)

    @patch("spine.gh_json")
    def test_empty(self, mock_gh_json):
        mock_gh_json.return_value = []
        self.assertEqual(spine.open_plan_count("owner/repo"), 0)


if __name__ == "__main__":
    unittest.main()
