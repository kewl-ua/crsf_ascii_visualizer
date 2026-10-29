"""Noninteractive command contract; help does not need a terminal or curses."""

from pathlib import Path
import subprocess
import sys
import unittest

from crsf_ascii_visualizer import __version__


ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def run_cli(self, *arguments):
        return subprocess.run(
            [sys.executable, "-m", "crsf_ascii_visualizer", *arguments],
            cwd=ROOT, capture_output=True, text=True, timeout=10,
        )

    def test_help_works_without_a_tty(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        for option in ("--stdin", "--serial", "--udp", "--unix-dgram", "--unix-stream"):
            self.assertIn(option, result.stdout)
        self.assertTrue(result.stdout.isascii())
        self.assertNotIn("Traceback", result.stderr)

    def test_version_works_without_a_tty(self):
        result = self.run_cli("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"crsf-viz {__version__}")

    def test_sources_are_mutually_exclusive(self):
        result = self.run_cli("--udp", "127.0.0.1:5555", "--stdin")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)

    def test_invalid_udp_endpoints_are_argument_errors(self):
        for value in ("localhost", "127.0.0.1:nope", "127.0.0.1:0",
                      "127.0.0.1:-1", "127.0.0.1:65536", ":5555"):
            with self.subTest(endpoint=value):
                result = self.run_cli("--udp", value)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_invalid_baud_rates_are_argument_errors(self):
        for value in ("0", "-1", "fast"):
            with self.subTest(baud=value):
                result = self.run_cli("--baud", value)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_legacy_script_still_exposes_help(self):
        result = subprocess.run(
            [sys.executable, "main.py", "--help"], cwd=ROOT,
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--udp", result.stdout)


if __name__ == "__main__":
    unittest.main()
