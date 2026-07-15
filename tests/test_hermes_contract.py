from __future__ import annotations

import datetime

import test_cli


class HermesContractTests(test_cli.ShimCLITestCase):
    def test_hermes_lifecycle_contract(self) -> None:
        version = self.docker("version")
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertIn("container CLI version 1.1.0", version.stdout)

        image = self.docker(
            "image",
            "inspect",
            "nikolaik/python-nodejs:python3.11-nodejs20",
            "--format",
            "{{json .Config.Entrypoint}}",
        )
        self.assertEqual(image.returncode, 0, image.stderr)
        self.assertEqual(image.stdout.strip(), '["docker-entrypoint.sh"]')

        run = self.docker(
            "run",
            "-d",
            "--init",
            "--name",
            "hermes-test",
            "--label",
            "hermes-agent=1",
            "--label",
            "hermes-task-id=default",
            "--label",
            "hermes-profile=default",
            "-w",
            "/root",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "DAC_OVERRIDE",
            "--cap-add",
            "CHOWN",
            "--cap-add",
            "FOWNER",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "256",
            "--tmpfs",
            "/tmp:rw,nosuid,size=512m",
            "--tmpfs",
            "/var/tmp:rw,noexec,nosuid,size=256m",
            "--tmpfs",
            "/run:rw,noexec,nosuid,size=64m",
            "--tmpfs",
            "/workspace:rw,exec,size=10g",
            "--memory",
            "512m",
            "--cpus",
            "1.0",
            "-v",
            "/host:/workspace:ro",
            "-e",
            "FOO=bar",
            "nikolaik/python-nodejs:python3.11-nodejs20",
            "sleep",
            "infinity",
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "hermes-test")

        ps_ids = self.docker(
            "ps", "-a", "--filter", "label=hermes-agent=1", "--format", "{{.ID}}"
        )
        self.assertEqual(ps_ids.returncode, 0, ps_ids.stderr)
        self.assertEqual(ps_ids.stdout.strip(), "hermes-test")

        ps_state = self.docker(
            "ps",
            "-a",
            "--filter",
            "label=hermes-task-id=default",
            "--filter",
            "label=hermes-profile=default",
            "--format",
            "{{.ID}}\t{{.State}}",
        )
        self.assertEqual(ps_state.returncode, 0, ps_state.stderr)
        self.assertEqual(ps_state.stdout.strip(), "hermes-test\trunning")

        before_stop = self.docker(
            "inspect", "--format", "{{.State.FinishedAt}}", "hermes-test"
        )
        self.assertEqual(before_stop.returncode, 0, before_stop.stderr)
        self.assertEqual(before_stop.stdout.strip(), "0001-01-01T00:00:00Z")

        exec_result = self.docker(
            "exec",
            "-i",
            "-e",
            "TEST=1",
            "hermes-test",
            "bash",
            "-c",
            "cat",
            input_text="hello\n",
        )
        self.assertEqual(exec_result.returncode, 0, exec_result.stderr)
        self.assertEqual(exec_result.stdout, "hello\n")

        stop = self.docker("stop", "-t", "10", "hermes-test")
        self.assertEqual(stop.returncode, 0, stop.stderr)

        after_stop = self.docker(
            "inspect", "--format", "{{.State.FinishedAt}}", "hermes-test"
        )
        self.assertEqual(after_stop.returncode, 0, after_stop.stderr)
        self.assertEqual(after_stop.stdout.strip(), "2026-01-01T00:00:05Z")
        self.assertFalse(after_stop.stdout.strip().startswith("0001-01-01"))

        exited = self.docker(
            "ps", "-a", "--filter", "status=exited", "--format", "{{.ID}}"
        )
        self.assertEqual(exited.returncode, 0, exited.stderr)
        self.assertEqual(exited.stdout.strip(), "hermes-test")

        start = self.docker("start", "hermes-test")
        self.assertEqual(start.returncode, 0, start.stderr)

        rm = self.docker("rm", "-f", "hermes-test")
        self.assertEqual(rm.returncode, 0, rm.stderr)

        gone = self.docker(
            "ps", "-a", "--filter", "label=hermes-agent=1", "--format", "{{.ID}}"
        )
        self.assertEqual(gone.returncode, 0, gone.stderr)
        self.assertEqual(gone.stdout.strip(), "")

    def test_orphan_reaper_receives_a_real_finished_time(self) -> None:
        run = self.docker(
            "run",
            "-d",
            "--name",
            "reap-me",
            "--label",
            "hermes-agent=1",
            "alpine",
            "sleep",
            "infinity",
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        stop = self.docker("stop", "reap-me")
        self.assertEqual(stop.returncode, 0, stop.stderr)

        out = self.docker("inspect", "--format", "{{.State.FinishedAt}}", "reap-me")
        self.assertEqual(out.returncode, 0, out.stderr)
        stamp = out.stdout.strip()
        self.assertFalse(stamp.startswith("0001-01-01"))
        datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))


if __name__ == "__main__":
    import unittest

    unittest.main()
