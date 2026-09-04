"""
Smoke tests for the Step 1 scaffold.

Deliberately thin: at this stage there is one module and it is a surface.
Real coverage arrives with the job contract and registry in Phase 0.

    python3 -m unittest discover -s tests -v
"""
import importlib.util
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DARKWEB = os.path.join(ROOT, "bin", "darkweb")


def load_darkweb():
    spec = importlib.util.spec_from_loader(
        "darkweb", importlib.machinery.SourceFileLoader("darkweb", DARKWEB))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestLayout(unittest.TestCase):
    def test_scaffold_directories_exist(self):
        for d in ("bin", "core", "collectors", "surfaces", "tests"):
            self.assertTrue(os.path.isdir(os.path.join(ROOT, d)), d)

    def test_darkweb_is_executable(self):
        self.assertTrue(os.access(DARKWEB, os.X_OK), "bin/darkweb not +x")

    def test_secrets_are_ignored(self):
        with open(os.path.join(ROOT, ".gitignore")) as fh:
            gi = fh.read()
        for pat in (".env", "*.db", "*.sqlite", "__pycache__/", ".DS_Store"):
            self.assertIn(pat, gi, f"{pat} not gitignored")

    def test_env_example_has_no_populated_secret(self):
        """Every key in .env.example must be empty. A committed key is the
        one mistake a public-from-day-one repo cannot make."""
        with open(os.path.join(ROOT, ".env.example")) as fh:
            lines = fh.readlines()
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            if any(t in key for t in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
                self.assertEqual(val, "", f"{key} has a value committed")


class TestConsole(unittest.TestCase):
    def setUp(self):
        self.dw = load_darkweb()

    def test_renders_without_color(self):
        self.dw.P.off()
        out = self.dw.render()
        self.assertIn("NickOS", out)
        self.assertIn("SYSTEM", out)
        self.assertIn("INTEGRITY", out)

    def test_every_framed_line_is_the_same_width(self):
        """The bezel must not ragged-edge. Checked with color OFF so the
        comparison is on visible characters."""
        self.dw.P.off()
        lines = self.dw.render().splitlines()
        widths = {len(x) for x in lines}
        self.assertEqual(len(widths), 1,
                         f"ragged frame, widths seen: {sorted(widths)}")

    def test_padding_holds_with_color_on(self):
        """With ANSI on, visible width must still be constant once escapes
        are stripped — this is what Row.render exists to guarantee."""
        import re
        mod = load_darkweb()      # fresh module, palette re-enabled
        strip = re.compile(r"\033\[[0-9;]*m")
        lines = [strip.sub("", x) for x in mod.render().splitlines()]
        self.assertEqual(len({len(x) for x in lines}), 1,
                         "frame ragged once escapes are stripped")

    def test_reports_gaps_instead_of_inventing_numbers(self):
        self.dw.P.off()
        out = self.dw.render()
        # Every panel is now built, so every panel shows real data or an
        # honest empty state — never an invented number.
        self.assertIn("heartbeat", out)          # registry, Phase 0
        self.assertIn("cap", out)                # cost accounting, Phase 1
        self.assertIn("ITEM STORE", out)         # the item store, Phase 2
        for invented in ("TODO", "example.com", "lorem", "0.00 of 0.00"):
            self.assertNotIn(invented, out)

    def test_runs_as_a_subprocess(self):
        r = subprocess.run([sys.executable, DARKWEB, "--no-color"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("D A R K W E B", r.stdout)


if __name__ == "__main__":
    unittest.main()
