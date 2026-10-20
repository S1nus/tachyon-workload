"""Offline probe regressions; real-binary compatibility is a separate smoke check."""

import subprocess
import unittest

from .compatibility import COMMANDS, check


def help_output(command):
    tokens = COMMANDS[command]
    usage = " ".join(token for token in tokens if not token.startswith("--"))
    options = "\n".join("  " + token + " <VALUE>" for token in tokens if token.startswith("--"))
    return f"Usage: devtool tachyon --wallet-dir <WALLET_DIR> {command} {usage}\nOptions:\n{options}\n"


class CompatibilityTests(unittest.TestCase):
    def test_all_commands_use_help_only(self):
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            self.assertEqual(args[:4], ["devtool", "tachyon", "--wallet-dir", "/unused-help-only"])
            self.assertEqual(args[-1], "--help")
            self.assertEqual(len(args), 6)
            self.assertEqual(kwargs, dict(capture_output=True, text=True, timeout=30))
            return subprocess.CompletedProcess(args, 0, help_output(args[-2]))

        self.assertEqual(check("devtool", run), 13)
        self.assertEqual({args[-2] for args in calls}, set(COMMANDS))

    def test_missing_idempotency_flag_fails_compatibility(self):
        def run(args, **kwargs):
            return subprocess.CompletedProcess(args, 0,
                help_output(args[-2]).replace("  --operation-id <VALUE>\n", ""))
        with self.assertRaisesRegex(RuntimeError, "operation-id"):
            check("devtool", run)

    def test_similarly_named_option_is_not_a_match(self):
        def run(args, **kwargs):
            return subprocess.CompletedProcess(args, 0,
                help_output(args[-2]).replace("--operation-id", "--operation-id-file"))
        with self.assertRaisesRegex(RuntimeError, "operation-id"):
            check("devtool", run)

    def test_option_mentioned_only_in_prose_is_not_a_match(self):
        def run(args, **kwargs):
            return subprocess.CompletedProcess(args, 0,
                help_output(args[-2]).replace("  --operation-id <VALUE>",
                                             "  Previously used --operation-id here"))
        with self.assertRaisesRegex(RuntimeError, "operation-id"):
            check("devtool", run)

    def test_generic_help_is_not_command_compatibility(self):
        with self.assertRaisesRegex(RuntimeError, "unexpected usage"):
            check("devtool", lambda *a, **k: subprocess.CompletedProcess(a, 0,
                  "Usage: devtool tachyon --wallet-dir <WALLET_DIR> <COMMAND>"))

    def test_missing_tachyon_feature_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "exit 2"):
            check("devtool", lambda *a, **k: subprocess.CompletedProcess(a, 2, ""))

    def test_short_alias_is_accepted(self):
        def run(args, **kwargs):
            return subprocess.CompletedProcess(args, 0,
                help_output(args[-2]).replace("  --operation-id", "  -o, --operation-id"))
        self.assertEqual(check("devtool", run), 13)

    def test_reports_all_command_failures(self):
        calls = []

        def run(args, **kwargs):
            command = args[-2]
            calls.append(command)
            if command == "init":
                return subprocess.CompletedProcess(args, 2, "")
            if command == "address":
                return subprocess.CompletedProcess(args, 0, "Usage: devtool")
            if command == "sync":
                raise subprocess.TimeoutExpired(args, 30)
            output = help_output(command)
            if command == "send":
                output = output.replace("  --operation-id <VALUE>\n", "")
            return subprocess.CompletedProcess(args, 0, output)

        with self.assertRaises(RuntimeError) as caught:
            check("devtool", run)
        self.assertEqual(str(caught.exception).splitlines(), [
            "CLI help failed for init: exit 2",
            "CLI address help has unexpected usage",
            "CLI help timed out for sync after 30s",
            "CLI send help missing: --operation-id",
        ])
        self.assertEqual(calls, list(COMMANDS))

    def test_unavailable_binary_fails_without_retry(self):
        calls = []

        def run(*args, **kwargs):
            calls.append(args)
            raise FileNotFoundError()

        with self.assertRaisesRegex(RuntimeError, "help unavailable"):
            check("devtool", run)
        self.assertEqual(len(calls), 1)
