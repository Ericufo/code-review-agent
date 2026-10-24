from dataclasses import asdict
import json
from time import perf_counter
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from context_review import review_with_context
from diff_parser import FileDiff
from review_events import reason_code, record_event, response_usage
from review_models import ReviewResult
from review_pipeline import review_files
from review_tools import ContextBudget
from test_context_review import call, response
from test_review_tools import context


class TestReviewEvents(unittest.TestCase):
    def test_failure_categories_do_not_confuse_missing_context_with_bad_citations(self):
        self.assertEqual(reason_code("Model reported insufficient repository context: required evidence unavailable"),
                         "insufficient_context")
        self.assertEqual(reason_code("Finding lacks evidence actually returned by a repository tool"), "invalid_evidence")
        self.assertEqual(reason_code("Repository context incomplete: read_file: Tool failed (OSError)"), "tool_error")

    def test_usage_whitelists_numeric_fields_without_guessing_missing_tokens(self):
        for usage in (None, {}, {"input_tokens": True, "output_tokens": -1, "total_tokens": "9"}):
            self.assertIsNone(response_usage(SimpleNamespace(usage=usage)))
        self.assertEqual(response_usage(SimpleNamespace(usage={"input_tokens": 0, "secret": "DO_NOT_LOG"})),
                         {"input_tokens": 0, "output_tokens": None, "total_tokens": None})
        self.assertEqual(response_usage(SimpleNamespace(usage=SimpleNamespace(input_tokens=5, output_tokens=2, total_tokens=7))),
                         {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7})

    def test_context_events_record_usage_even_if_result_is_invalid(self):
        invalid = response()
        invalid.output_text = "RAW_MODEL_BODY_DO_NOT_LOG"
        invalid.usage = {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}
        events = []
        budget = ContextBudget()
        result = review_with_context("SOURCE_DO_NOT_LOG", context=context(), budget=budget,
            request_fn=Mock(side_effect=[invalid, response()]), on_record=events.append)
        self.assertEqual(result.issues, [])
        self.assertEqual([event.node for event in events], ["model_request", "repair_request"])
        self.assertEqual(events[0].api_usage["total_tokens"], 14)
        self.assertIsNone(events[1].api_usage)
        self.assertEqual([event.counters["model_calls"] for event in events], [1, 2])
        serialized = json.dumps([asdict(event) for event in events])
        self.assertNotIn("DO_NOT_LOG", serialized)
        self.assertTrue(all(event.duration_ms >= 0 for event in events))

    def test_api_exception_is_categorized_without_body(self):
        events = []
        with self.assertRaises(Exception):
            review_with_context("diff", context=context(), budget=ContextBudget(),
                request_fn=Mock(side_effect=TimeoutError("PRIVATE_API_BODY")), on_record=events.append)
        self.assertEqual(events[0].termination_reason, "timeout")
        self.assertEqual(events[0].outcome, "error")
        self.assertIsNone(events[0].api_usage)
        self.assertNotIn("PRIVATE_API_BODY", repr(events))

    def test_budget_rejection_is_not_a_dispatched_call(self):
        records, model = [], Mock()
        batch = review_files([FileDiff("app.py", "diff")], engine="langgraph", context=context(),
            context_request_fn=model, max_input_chars=0, on_record=records.append)
        model.assert_not_called()
        self.assertEqual(batch.status, "INCOMPLETE")
        self.assertFalse(any(event.node == "model_request" for event in records))
        terminal = next(event for event in records if event.node == "review_file")
        self.assertEqual((terminal.outcome, terminal.termination_reason), ("skipped", "budget_exhausted"))
        self.assertEqual(terminal.counters["model_calls"], 0)
        self.assertEqual(terminal.counters["attempted"], 0)

    def test_structured_hook_preserves_old_trace_and_results(self):
        def run(on_record=None):
            steps = []
            result = review_files([FileDiff("app.py", "diff"), FileDiff("README.md", "ignored")],
                engine="langgraph", context=context(),
                context_request_fn=Mock(side_effect=[response(call()), response()]),
                on_step=steps.append, on_record=on_record)
            return result, steps
        baseline, baseline_steps = run()
        records = []
        actual, actual_steps = run(records.append)
        self.assertEqual(actual, baseline)
        self.assertEqual(actual_steps, baseline_steps)
        self.assertEqual(next(e for e in records if e.node == "read_file").path, "app.py")
        self.assertEqual(next(e for e in records if e.node == "exclude_file").outcome, "excluded")
        self.assertEqual(records[-1].termination_reason, "COMPLETE")
        self.assertEqual(records[-1].counters["excluded"], 1)

    def test_diff_only_events_do_not_invent_token_or_prompt_counts(self):
        records = []
        with patch("review_pipeline.review_diff", return_value=ReviewResult(summary="", issues=[])):
            review_files([FileDiff("app.py", "diff")], engine="langgraph", on_record=records.append)
        event = next(event for event in records if event.node == "model_request")
        self.assertEqual(event.counters["model_calls"], 1)
        self.assertIsNone(event.counters["input_chars"])
        self.assertIsNone(event.api_usage)
        with self.assertRaisesRegex(ValueError, "Structured events require"):
            review_files([], engine="classic", on_record=records.append)

    def test_diff_only_transport_budget_rejection_is_not_a_model_request(self):
        from review_tools import ModelBudgetError
        records = []
        with patch("review_pipeline.review_diff", side_effect=ModelBudgetError("Run budget exhausted")):
            batch = review_files([FileDiff("app.py", "diff")], engine="langgraph", on_record=records.append)
        self.assertEqual((len(batch.skipped), len(batch.failures)), (1, 0))
        self.assertFalse(any(e.node == "model_request" for e in records))
        rejected = next(e for e in records if e.node == "request_rejected")
        self.assertEqual(rejected.counters["model_calls"], 0)
        self.assertEqual(rejected.termination_reason, "budget_exhausted")

    def test_observer_failure_is_source_free_and_does_not_change_result(self):
        observer = Mock(side_effect=RuntimeError("PRIVATE_OBSERVER_BODY"))
        with self.assertWarnsRegex(RuntimeWarning, "observation was dropped") as caught:
            record_event(observer, node="test", started=perf_counter())
        self.assertNotIn("PRIVATE_OBSERVER_BODY", str(caught.warning))


if __name__ == "__main__":
    unittest.main()
