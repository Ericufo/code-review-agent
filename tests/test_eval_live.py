from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from evals.live import Ledger, LiveModel, LivePlan, ROOT, main, run_live, selected_cases
from evals.mocks import ScriptedModel, reply
from review_tools import ModelBudgetError


def plan(**overrides):
    data = json.loads((ROOT / "evals/live-plan.example.json").read_text(encoding="utf-8"))
    data.update(pricing_checked_on=datetime.now(timezone.utc).date().isoformat())
    data.update(overrides)
    return LivePlan.model_validate_json(json.dumps(data))


def response(text=None, usage=True):
    result = reply(text=text)
    if usage:
        result.usage = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
    return result


class TestEvalLive(unittest.TestCase):
    def test_plan_validation_does_not_create_client_or_spend_calls(self):
        with patch("openai.OpenAI", side_effect=AssertionError("must stay offline")):
            self.assertEqual(main(["--plan", str(ROOT / "evals/live-plan.example.json")]), 0)

    def test_invalid_plan_limits_selection_and_holdout_are_rejected(self):
        for overrides in ({"max_run_calls": True}, {"max_cost_usd": "NaN"},
                          {"max_cost_usd": "0.001"}, {"max_case_calls": 0},
                          {"input_usd_per_million": "0.01"}, {"provider_context_tokens": 100},
                          {"base_url": "https://untrusted.invalid"},
                          {"split": "holdout"}, {"case_ids": ["x", "x"]}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                plan(**overrides)
        with self.assertRaises(ValueError):
            selected_cases(plan(case_ids=["ascii-frame-length"]))
        with self.assertRaises(ValueError):
            selected_cases(plan(dataset_hash="sha256:" + "0" * 64))

    def test_cli_requires_key_and_fresh_prices_before_client_creation(self):
        with tempfile.TemporaryDirectory() as directory, patch("openai.OpenAI") as client:
            path = Path(directory) / "plan.json"
            path.write_text(plan().model_dump_json(), encoding="utf-8")
            with patch.dict("os.environ", {"DEEPSEEK_API_KEY": ""}), self.assertRaises(SystemExit):
                main(["--plan", str(path), "--live"])
            stale = plan(pricing_checked_on=(datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat())
            path.write_text(stale.model_dump_json(), encoding="utf-8")
            with self.assertRaises(SystemExit):
                main(["--plan", str(path), "--live"])
            client.assert_not_called()

    def test_transport_reserves_before_dispatch_and_releases_only_observed_usage(self):
        events = []
        p = plan()
        ledger = Ledger(p, events.append)
        def create(**kwargs):
            self.assertEqual(ledger.calls, 1)
            self.assertEqual(ledger.charged, p.reservation)
            self.assertEqual(events[0]["outcome"], "in_flight")
            self.assertEqual(kwargs["max_output_tokens"], p.max_output_tokens)
            self.assertEqual(kwargs["tools"], [])
            return response()
        model = LiveModel(p, ledger, create, "id")
        model.diff_review("SOURCE_PRIVATE")
        self.assertEqual(ledger.charged, Decimal("0.000054"))
        self.assertEqual(model.resources()["api_usage"]["total_tokens"], 120)
        self.assertEqual(model.resources()["model_calls"], 1)
        self.assertNotIn("SOURCE_PRIVATE", json.dumps(events))
        self.assertIsNone(model.resources()["cost"])

    def test_missing_partial_and_out_of_bound_usage_stop_run_without_refund(self):
        for usage in (None, {"input_tokens": 2}, {"input_tokens": 1048577, "output_tokens": 20},
                      {"input_tokens": 100, "output_tokens": 4097}):
            with self.subTest(usage=usage):
                p = plan()
                ledger = Ledger(p, lambda event: None)
                reply_value = response()
                reply_value.usage = usage
                create = Mock(return_value=reply_value)
                model = LiveModel(p, ledger, create, "id")
                model.diff_review("diff")
                self.assertEqual(ledger.charged, p.reservation)
                self.assertIsNotNone(ledger.stop_reason)
                with self.assertRaises(ModelBudgetError):
                    model.diff_review("diff")
                self.assertEqual(create.call_count, 1)

    def test_request_error_retains_reservation_and_never_exports_body(self):
        p = plan()
        events = []
        ledger = Ledger(p, events.append)
        model = LiveModel(p, ledger, Mock(side_effect=RuntimeError("PRIVATE_KEY_BODY")), "id")
        with self.assertRaisesRegex(Exception, "transport_error"):
            model.diff_review("diff")
        self.assertEqual(ledger.charged, p.reservation)
        self.assertEqual(ledger.stop_reason, "usage_unavailable")
        self.assertNotIn("PRIVATE_KEY_BODY", json.dumps(events))

    def test_recording_failure_prevents_dispatch_without_charging_a_call(self):
        p = plan()
        ledger = Ledger(p, Mock(side_effect=OSError("disk full")))
        create = Mock()
        model = LiveModel(p, ledger, create, "id")
        with self.assertRaises(OSError):
            model.diff_review("diff")
        create.assert_not_called()
        self.assertEqual((ledger.calls, ledger.charged, model.resources()["model_calls"]), (0, 0, 0))
        self.assertEqual(ledger.stop_reason, "recording_error")
        ledger = Ledger(p, Mock(side_effect=[None, OSError("disk full")]))
        create = Mock(return_value=response())
        model = LiveModel(p, ledger, create, "id")
        with self.assertRaises(OSError):
            model.diff_review("diff")
        self.assertEqual(ledger.calls, 1)
        self.assertEqual(ledger.stop_reason, "recording_error")
        with self.assertRaises(ModelBudgetError):
            model.diff_review("diff")
        create.assert_called_once()

    def test_acceptance_must_match_the_frozen_version_and_manifest_keeps_provenance(self):
        original = Path.read_text
        def wrong_acceptance(path, *args, **kwargs):
            if path.name == "annotation-review.json":
                return json.dumps({"dataset_hash": "old", "status": "ai_reviewed_user_accepted"})
            return original(path, *args, **kwargs)
        with patch.object(Path, "read_text", wrong_acceptance), self.assertRaises(ValueError):
            selected_cases(plan())
        with tempfile.TemporaryDirectory() as directory:
            artifact = run_live(plan(case_ids=["guaranteed-profile"]), output=Path(directory) / "run",
                                create=Mock(return_value=response()))
        manifest = artifact["manifest"]
        self.assertTrue(manifest["annotation_review_hash"].startswith("sha256:"))
        self.assertTrue(manifest["comparison_config_hash"].startswith("sha256:"))
        self.assertEqual(manifest["transport_policy"]["sdk_max_retries"], 0)

    def test_run_and_case_limits_count_failed_format_requests_and_repairs(self):
        for overrides, expected in (({"max_run_calls": 1}, 1), ({"max_case_calls": 1}, 1), ({}, 2)):
            p = plan(**overrides)
            ledger = Ledger(p, lambda event: None)
            create = Mock(return_value=response(text="not JSON"))
            model = LiveModel(p, ledger, create, "id")
            with self.assertRaises(Exception):
                model.diff_review("diff")
            self.assertEqual(create.call_count, expected)
            self.assertEqual(ledger.calls, expected)

    def test_character_and_cost_caps_reject_before_dispatch(self):
        p = plan(max_input_chars=1)
        create = Mock()
        ledger = Ledger(p, lambda event: None)
        with self.assertRaises(ModelBudgetError):
            LiveModel(p, ledger, create, "id").diff_review("diff")
        self.assertEqual(ledger.calls, 0)
        create.assert_not_called()
        p = plan(max_cost_usd="0.32")
        ledger = Ledger(p, lambda event: None)
        ledger.reserve()
        with self.assertRaises(ModelBudgetError):
            ledger.reserve()
        self.assertEqual(ledger.calls, 1)

    def test_context_first_two_request_plan_preserves_unexecuted_comparison(self):
        p = plan(case_ids=["shared-list-sort"], mode_order=["context", "diff-only"],
                 max_run_calls=2, max_case_calls=2)
        script = ScriptedModel()
        def create(**kwargs):
            result = script.context_request(kwargs["input"], kwargs["instructions"], tools_enabled=bool(kwargs["tools"]))
            result.usage = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
            return result
        with tempfile.TemporaryDirectory() as directory:
            artifact = run_live(p, output=Path(directory) / "run", create=create)
        context, diff = artifact["results"]
        self.assertEqual((context["mode"], context["status"], len(context["findings"])), ("context", "COMPLETE", 1))
        self.assertEqual(context["resources"]["model_calls"], 2)
        self.assertEqual((diff["mode"], diff["reviewed_files"], diff["resources"]["model_calls"]), ("diff-only", 0, 0))
        self.assertEqual(artifact["manifest"]["resources"]["model_calls"], 2)
        self.assertEqual(artifact["manifest"]["resources"]["stop_reason"], "run_call_budget")
        for order in (["context"], ["context", "context"], ["diff-only", "context", "diff-only"]):
            with self.assertRaises(ValueError):
                plan(mode_order=order)

    def test_repair_budget_stop_is_failed_attempt_not_unstarted_skip(self):
        p = plan(case_ids=["guaranteed-profile"], max_case_calls=1)
        with tempfile.TemporaryDirectory() as directory:
            artifact = run_live(p, output=Path(directory) / "run",
                                create=Mock(return_value=response(text="invalid JSON")))
        row = artifact["results"][0]
        self.assertEqual((row["resources"]["model_calls"], len(row["failures"]), len(row["skipped"])), (1, 1, 0))

    def test_context_run_guard_rolls_back_graph_counters_for_rejected_dispatch(self):
        p = plan(case_ids=["shared-list-sort"], max_run_calls=2)
        script = ScriptedModel()
        def create(**kwargs):
            if kwargs["input"][0]["content"].startswith("<git_diff>"):
                return response()
            result = script.context_request(kwargs["input"], kwargs["instructions"], tools_enabled=bool(kwargs["tools"]))
            result.usage = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
            return result
        with tempfile.TemporaryDirectory() as directory:
            artifact = run_live(p, output=Path(directory) / "run", create=create)
        row = artifact["results"][1]
        self.assertEqual(row["status"], "INCOMPLETE")
        self.assertEqual(row["resources"]["model_calls"], 1)
        self.assertEqual(row["counters"]["model_calls"], 1)

    def test_live_pipeline_uses_production_graph_and_keeps_all_scheduled_records(self):
        p = plan(max_run_calls=2)
        create = Mock(return_value=response())
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            artifact = run_live(p, output=output, create=create)
            self.assertEqual(len(artifact["results"]), 4)
            self.assertEqual(create.call_count, 2)
            self.assertEqual(sum(r["resources"]["model_calls"] for r in artifact["results"]), 2)
            self.assertEqual(artifact["manifest"]["status"], "stopped")
            self.assertTrue(any(r["execution_status"] == "not_run" for r in artifact["results"]))
            self.assertTrue(any(r["status"] == "INCOMPLETE" for r in artifact["results"]))
            events = [json.loads(line) for line in (output / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(sum(e["node"] == "transport_request" for e in events), 2)
            self.assertEqual(sum(e["node"] == "transport_result" for e in events), 2)
            self.assertEqual([e["stream_sequence"] for e in events], list(range(1, len(events) + 1)))
            self.assertEqual(json.loads((output / "results.json").read_text(encoding="utf-8")), artifact)
            with self.assertRaises(FileExistsError):
                run_live(p, output=output, create=create)
            self.assertEqual(create.call_count, 2)

    def test_successful_context_tools_use_same_meter_and_never_receive_gold(self):
        p = plan(case_ids=["shared-list-sort"])
        script = ScriptedModel()
        captured = []
        def create(**kwargs):
            captured.append(kwargs)
            if isinstance(kwargs["input"][0]["content"], str) and kwargs["input"][0]["content"].startswith("<git_diff>"):
                return response()
            result = script.context_request(kwargs["input"], kwargs["instructions"], tools_enabled=bool(kwargs["tools"]))
            result.usage = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
            return result
        with tempfile.TemporaryDirectory() as directory:
            artifact = run_live(p, output=Path(directory) / "run", create=create)
        self.assertEqual([r["status"] for r in artifact["results"]], ["COMPLETE", "COMPLETE"])
        self.assertEqual([r["resources"]["model_calls"] for r in artifact["results"]], [1, 2])
        self.assertEqual(artifact["results"][1]["resources"]["api_usage"]["input_tokens"], 200)
        self.assertNotIn("shared-list-sort", json.dumps(captured))
        self.assertNotIn("expectation", json.dumps(captured))

    def test_keyboard_interrupt_preserves_all_cases_and_pending_reservation(self):
        p = plan()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            artifact = run_live(p, output=output, create=Mock(side_effect=KeyboardInterrupt))
            self.assertEqual(artifact["manifest"]["resources"]["model_calls"], 1)
            self.assertEqual(artifact["manifest"]["resources"]["cost_upper_bound_usd"], str(p.reservation))
            self.assertEqual(artifact["results"][0]["execution_status"], "interrupted")
            self.assertTrue(all(r["execution_status"] == "not_run" for r in artifact["results"][1:]))
            self.assertEqual(len(json.loads((output / "adjudications.json").read_text(encoding="utf-8"))), 4)

    def test_live_cli_disables_sdk_retries_and_sets_timeout(self):
        with tempfile.TemporaryDirectory() as directory, patch("openai.OpenAI") as client:
            p = plan(case_ids=["guaranteed-profile"])
            path = Path(directory) / "plan.json"
            path.write_text(p.model_dump_json(), encoding="utf-8")
            client.return_value.__enter__.return_value.responses.create.return_value = response()
            with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "PRIVATE_FAKE_KEY"}):
                code = main(["--plan", str(path), "--live", "--output", str(Path(directory) / "run")])
            self.assertEqual(code, 0)
            self.assertEqual(client.call_args.kwargs["max_retries"], 0)
            self.assertEqual(client.call_args.kwargs["timeout"], p.timeout_seconds)
            self.assertNotIn("PRIVATE_FAKE_KEY", (Path(directory) / "run/manifest.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
