from __future__ import annotations

import test_cli


class OpenClawContractTests(test_cli.ShimCLITestCase):
    def test_sandbox_running_probe(self) -> None:
        run = self.docker(
            "run", "-d", "--name", "openclaw-sandbox", "alpine", "sleep", "infinity"
        )
        self.assertEqual(run.returncode, 0, run.stderr)

        running = self.docker(
            "inspect", "-f", "{{.State.Running}}", "openclaw-sandbox"
        )
        self.assertEqual(running.returncode, 0, running.stderr)
        self.assertEqual(running.stdout.strip(), "true")

        stop = self.docker("stop", "openclaw-sandbox")
        self.assertEqual(stop.returncode, 0, stop.stderr)
        stopped = self.docker(
            "inspect", "-f", "{{.State.Running}}", "openclaw-sandbox"
        )
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertEqual(stopped.stdout.strip(), "false")


if __name__ == "__main__":
    import unittest

    unittest.main()
