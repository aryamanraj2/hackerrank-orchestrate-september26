"""Gemini image resolver: parsing, target selection and key handling, offline."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import CODE_DIR, build_dataset, default_tables

sys.path.insert(0, str(CODE_DIR / "evidence"))

import resolve_images  # noqa: E402
from dataset_loader import load_dataset  # noqa: E402

SHA = "ab" * 32
SECRET = "sk-test-do-not-leak"


def payload(**answer) -> dict:
    body = {
        "status": "resolved",
        "amount": "822.05",
        "currency": "ZAR",
        "field_label": "Amount due",
        "selection_rationale": "matches the outstanding bill",
        "reason": "",
    }
    body.update(answer)
    return {
        "candidates": [{"content": {"parts": [{"text": json.dumps(body)}]}}],
        "usageMetadata": {"promptTokenCount": 10, "totalTokenCount": 12},
    }


class ParseTests(unittest.TestCase):
    def setUp(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_dataset(Path(tmp))
            self.event = load_dataset(Path(tmp) / "dataset").event_by_id["event_b"]

    def parse(self, **answer):
        return resolve_images.parse_response(payload(**answer), "image_a", self.event, SHA)

    def test_resolved(self):
        record = self.parse()
        self.assertEqual(record["status"], "resolved")
        self.assertEqual(record["amount"], "822.05")
        self.assertEqual(
            (record["image_id"], record["event_id"], record["image_sha256"]),
            ("image_a", "event_b", SHA),
        )

    def test_identity_fields_never_come_from_the_model(self):
        record = self.parse(image_id="image_99", event_id="event_99", image_sha256="00")
        self.assertEqual((record["image_id"], record["event_id"]), ("image_a", "event_b"))
        self.assertEqual(record["image_sha256"], SHA)

    def test_unresolved(self):
        record = self.parse(status="unresolved", amount=None, reason="total is cut off")
        self.assertEqual(record["status"], "unresolved")
        self.assertIsNone(record["amount"])
        self.assertEqual(record["reason"], "total is cut off")

    def test_bad_amount_becomes_unresolved_without_repair(self):
        for amount in ("1,00,000", "₹822", "822.05 ", 822.05):
            with self.subTest(amount=amount):
                record = self.parse(amount=amount)
                self.assertEqual(record["status"], "unresolved")
                self.assertIsNone(record["amount"])
                self.assertIn("not a plain decimal", record["reason"])

    def test_currency_mismatch_becomes_unresolved(self):
        record = self.parse(currency="INR")
        self.assertEqual(record["status"], "unresolved")
        self.assertIn("currency", record["reason"])

    def test_unparsable_payload_becomes_unresolved(self):
        record = resolve_images.parse_response({"candidates": []}, "image_a", self.event, SHA)
        self.assertEqual(record["status"], "unresolved")


class TargetTests(unittest.TestCase):
    def test_targets_are_blank_events_with_linked_images(self):
        tables = default_tables()
        tables["images"].append(
            {"image_id": "image_b", "user_id": "user_a", "request_id": "",
             "related_event_id": "event_a"}  # event_a has a supplied amount
        )
        tables["images"].append(
            {"image_id": "image_c", "user_id": "user_a", "request_id": "request_a",
             "related_event_id": ""}
        )
        with tempfile.TemporaryDirectory() as tmp:
            build_dataset(Path(tmp), tables=tables)
            dataset = load_dataset(Path(tmp) / "dataset")
            found = resolve_images.targets(dataset)
            self.assertEqual(
                [(image.image_id, event.event_id) for image, event in found],
                [("image_a", "event_b")],
            )
            self.assertEqual(resolve_images.targets(dataset, {"image_zz"}), [])


class KeyTests(unittest.TestCase):
    def test_missing_key_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(resolve_images.MissingKeyError, "GEMINI_API_KEY"):
                resolve_images.api_key(env={}, dotenv=Path(tmp) / ".env")

    def test_dotenv_fallback_and_no_leak(self):
        with tempfile.TemporaryDirectory() as tmp:
            dotenv = Path(tmp) / ".env"
            dotenv.write_text(f"OTHER=x\nexport GEMINI_API_KEY=\"{SECRET}\"\n")
            self.assertEqual(resolve_images.api_key(env={}, dotenv=dotenv), SECRET)
            dotenv.write_text(f"GEMINI_KEY={SECRET}\n")
            with self.assertRaises(resolve_images.MissingKeyError) as caught:
                resolve_images.api_key(env={}, dotenv=dotenv)
            self.assertNotIn(SECRET, str(caught.exception))


class DryRunTests(unittest.TestCase):
    def test_dry_run_makes_no_network_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset_dir = build_dataset(Path(tmp))
            out = Path(tmp) / "artifact.json"
            stdout = io.StringIO()
            with mock.patch.object(
                resolve_images.urllib.request, "urlopen", side_effect=AssertionError("network")
            ), contextlib.redirect_stdout(stdout):
                code = resolve_images.main(
                    ["--dry-run", "--dataset", str(dataset_dir), "--out", str(out)]
                )
            self.assertEqual(code, 0)
            self.assertIn("=== image_a -> event_b", stdout.getvalue())
            self.assertFalse(out.exists())


class ResolveTests(unittest.TestCase):
    """Mocked HTTP: retries, failures, and an artifact the overlay accepts."""

    def run_resolver(self, responses):
        calls = []

        def urlopen(request, timeout):
            calls.append(request)
            outcome = responses.pop(0)
            if isinstance(outcome, int):
                raise resolve_images.urllib.error.HTTPError(
                    request.full_url, outcome, "error", {}, io.BytesIO(b"{}")
                )
            body = io.BytesIO(json.dumps(outcome).encode())
            body.status = 200
            return body

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dataset_dir = build_dataset(Path(tmp.name))
        out = Path(tmp.name) / "image_amounts.json"
        with mock.patch.object(resolve_images.urllib.request, "urlopen", urlopen), \
                mock.patch.object(resolve_images.time, "sleep"), \
                mock.patch.object(resolve_images, "api_key", return_value=SECRET), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            code = resolve_images.main(
                ["--backend", "api", "--model", "m", "--dataset", str(dataset_dir), "--out", str(out)]
            )
        self.assertEqual(code, 0)
        self.assertNotIn(SECRET, stderr.getvalue())
        for request in calls:
            self.assertEqual(request.get_header("X-goog-api-key"), SECRET)
            self.assertNotIn(SECRET, request.full_url)
        usage = json.loads(out.with_name("image_amounts_usage.json").read_text())
        return dataset_dir, out, json.loads(out.read_text()), usage

    def test_retried_success_is_applied_by_the_overlay(self):
        dataset_dir, out, artifact, usage = self.run_resolver(
            [503, 429, payload(amount="45.10")]
        )
        [record] = artifact["records"]
        self.assertEqual(record["status"], "resolved")
        self.assertEqual(usage["calls"][0]["attempts"], 3)
        self.assertEqual(usage["totals"]["promptTokenCount"], 10)
        self.assertEqual(usage["calls"][0]["thoughtsTokenCount"], 0)

        from cashflow import build_forecast
        from datetime import date

        dataset = load_dataset(dataset_dir)
        # event_b settled before this start date, so book from an earlier one.
        forecast = build_forecast(dataset, "user_a", date(2026, 1, 1), image_evidence=out)
        self.assertEqual(forecast.blockers, ())
        self.assertIn("image_a", " ".join(note.reason for note in forecast.notes))

    def test_persistent_failure_is_written_unresolved(self):
        _, _, artifact, usage = self.run_resolver([500, 500, 500, 500])
        [record] = artifact["records"]
        self.assertEqual(record["status"], "unresolved")
        self.assertEqual(record["reason"], "resolver call failed: 500")
        self.assertEqual(usage["calls"][0]["attempts"], 4)


def agy_output(answer=None, **overrides) -> str:
    answer = answer if answer is not None else json.loads(
        payload()["candidates"][0]["content"]["parts"][0]["text"]
    )
    output = {
        "conversation_id": "c",
        "status": "SUCCESS",
        "response": json.dumps({**answer, "toolAction": "Finishing task"}),
        "structured_output": answer,
        "usage": {"input_tokens": 100, "output_tokens": 20, "thinking_tokens": 5,
                  "cache_read_tokens": 50, "total_tokens": 120},
    }
    output.update(overrides)
    return json.dumps(output)


class AgyTests(unittest.TestCase):
    """Mocked ``subprocess.run``: the agent's sandbox, and every failure shape."""

    def run_agy(self, outcome):
        seen = []

        def run(command, **kwargs):
            if command[-1] == "--version":
                return subprocess.CompletedProcess(command, 0, "1.2.2\n", "")
            seen.append(
                {**kwargs, "command": command, "files": sorted(os.listdir(kwargs["cwd"]))}
            )
            if isinstance(outcome, BaseException):
                raise outcome
            code, stdout = outcome
            return subprocess.CompletedProcess(command, code, stdout, "")

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dataset_dir = build_dataset(Path(tmp.name))
        out = Path(tmp.name) / "image_amounts.json"
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": SECRET, "GOOGLE_API_KEY": SECRET}), \
                mock.patch.object(resolve_images.subprocess, "run", side_effect=run), \
                mock.patch.object(resolve_images.urllib.request, "urlopen",
                                  side_effect=AssertionError("network")), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = resolve_images.main(
                ["--model", "gemini-3.1-pro-high", "--dataset", str(dataset_dir),
                 "--out", str(out)]
            )
        self.assertEqual(code, 0)
        [call] = seen
        artifact = json.loads(out.read_text())
        usage = json.loads(out.with_name("image_amounts_usage.json").read_text())
        return call, artifact, usage

    def test_sandboxed_command_isolated_workspace_and_stripped_keys(self):
        call, artifact, usage = self.run_agy((0, agy_output()))
        command = call["command"]
        self.assertIsInstance(command, list)
        self.assertFalse(call.get("shell"))
        self.assertIn("--sandbox", command)
        self.assertEqual(command[command.index("--mode") + 1], "plan")
        self.assertFalse(any("dangerously" in part for part in command))
        self.assertIn("with your file viewing tool", command[command.index("-p") + 1])
        self.assertEqual(call["files"], ["image_a.png"])
        self.assertNotIn("GEMINI_API_KEY", call["env"])
        self.assertNotIn("GOOGLE_API_KEY", call["env"])
        schema_arg = command[command.index("--json-schema") + 1]
        self.assertFalse(Path(schema_arg).exists())  # temp schema is cleaned up
        self.assertEqual(
            (artifact["backend"], artifact["agy_version"], artifact["prompt_version"]),
            ("agy", "1.2.2", "image-amount-v2"),
        )
        self.assertEqual(usage["totals"]["total_tokens"], 120)

    def test_canned_answers_go_through_the_post_checks(self):
        base = json.loads(payload()["candidates"][0]["content"]["parts"][0]["text"])
        cases = [
            ({}, "resolved", ""),
            ({"status": "unresolved", "amount": None, "reason": "cut off"}, "unresolved",
             "cut off"),
            ({"amount": "1,00,000"}, "unresolved", "not a plain decimal"),
            ({"currency": "INR"}, "unresolved", "currency"),
        ]
        for change, status, reason in cases:
            with self.subTest(change=change):
                _, artifact, _ = self.run_agy((0, agy_output({**base, **change})))
                [record] = artifact["records"]
                self.assertEqual(record["status"], status)
                self.assertIn(reason, record["reason"])

    def test_failures_become_unresolved(self):
        cases = [
            ((0, agy_output(response="", structured_output=None)), "empty response"),
            ((0, agy_output(denied_actions=[{"action": "command",
                                             "display_name": "RunCommand"}])),
             "denied RunCommand"),
            ((0, agy_output(status="ERROR")), "status 'ERROR'"),
            ((1, ""), "exit 1"),
            (subprocess.TimeoutExpired(["agy"], 600), "timeout"),
            ((0, "not json at all"), "unparseable output"),
        ]
        for outcome, cause in cases:
            with self.subTest(cause=cause):
                _, artifact, usage = self.run_agy(outcome)
                [record] = artifact["records"]
                self.assertEqual(record["status"], "unresolved")
                self.assertEqual(record["reason"], f"agy call failed: {cause}")
                self.assertIsNone(record["amount"])

    def test_missing_usage_is_recorded_as_not_exposed(self):
        _, _, usage = self.run_agy((0, agy_output(usage=None)))
        [row] = usage["calls"]
        self.assertIsNone(row["total_tokens"])
        self.assertEqual(row["usage_source"], "not exposed by agy")


if __name__ == "__main__":
    unittest.main()
