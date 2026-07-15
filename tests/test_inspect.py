from __future__ import annotations

import datetime
import json

import test_cli


class InspectTests(test_cli.ShimCLITestCase):
    def run_container(
        self, name: str, *, labels: tuple[str, ...] = (), network: str | None = None
    ) -> None:
        args = ["run", "-d", "--name", name]
        for label in labels:
            args.extend(["--label", label])
        if network is not None:
            args.extend(["--network", network])
        args.extend(["alpine", "sleep", "infinity"])
        result = self.docker(*args)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_finished_at_is_real_for_stopped_container(self) -> None:
        self.run_container("reap-me")
        stop = self.docker("stop", "reap-me")
        self.assertEqual(stop.returncode, 0, stop.stderr)

        out = self.docker("inspect", "--format", "{{.State.FinishedAt}}", "reap-me")
        self.assertEqual(out.returncode, 0, out.stderr)
        stamp = out.stdout.strip()
        self.assertFalse(stamp.startswith("0001-01-01"))
        datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))

    def test_short_format_reports_running_state(self) -> None:
        self.run_container("state-probe")

        running = self.docker("inspect", "-f", "{{.State.Running}}", "state-probe")
        self.assertEqual(running.returncode, 0, running.stderr)
        self.assertEqual(running.stdout.strip(), "true")

        stop = self.docker("stop", "state-probe")
        self.assertEqual(stop.returncode, 0, stop.stderr)
        stopped = self.docker("inspect", "-f", "{{.State.Running}}", "state-probe")
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertEqual(stopped.stdout.strip(), "false")

    def test_format_option_spellings_share_one_renderer(self) -> None:
        self.run_container("formats")
        commands = (
            ("inspect", "-f", "{{.State.Status}}", "formats"),
            ("inspect", "-f{{.State.Status}}", "formats"),
            ("inspect", "-f={{.State.Status}}", "formats"),
            ("inspect", "--format", "{{.State.Status}}", "formats"),
            ("inspect", "--format={{.State.Status}}", "formats"),
        )
        for command in commands:
            with self.subTest(command=command):
                result = self.docker(*command)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "running")

    def test_template_supports_whitespace_literals_and_multiple_fields(self) -> None:
        self.run_container("mixed", labels=("role=worker",))
        result = self.docker(
            "inspect",
            "--format",
            "name={{ .Name }} running={{.State.Running}} image={{.Config.Image}}",
            "mixed",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "name=mixed running=true image=alpine",
        )

    def test_json_renders_labels_and_complete_object(self) -> None:
        self.run_container("json-probe", labels=("role=worker", "tier=core"))

        labels = self.docker(
            "inspect", "--format", "{{json .Config.Labels}}", "json-probe"
        )
        self.assertEqual(labels.returncode, 0, labels.stderr)
        self.assertEqual(json.loads(labels.stdout), {"role": "worker", "tier": "core"})

        whole = self.docker("inspect", "--format", "{{json .}}", "json-probe")
        self.assertEqual(whole.returncode, 0, whole.stderr)
        obj = json.loads(whole.stdout)
        self.assertEqual(obj["Id"], "json-probe")
        self.assertEqual(obj["State"]["Running"], True)

    def test_default_json_uses_the_same_docker_shaped_model(self) -> None:
        self.run_container("model", labels=("role=api",), network="private")
        result = self.docker("inspect", "model")
        self.assertEqual(result.returncode, 0, result.stderr)
        [obj] = json.loads(result.stdout)

        self.assertEqual(obj["Id"], "model")
        self.assertEqual(obj["Name"], "model")
        self.assertEqual(obj["Image"], "sha256:" + "a" * 64)
        self.assertEqual(obj["Created"], "2026-01-01T00:00:00Z")
        self.assertEqual(obj["Config"], {"Image": "alpine", "Labels": {"role": "api"}})
        self.assertEqual(
            obj["State"],
            {
                "Status": "running",
                "Running": True,
                "StartedAt": "2026-01-01T00:00:05Z",
                "FinishedAt": "0001-01-01T00:00:00Z",
            },
        )
        self.assertEqual(obj["NetworkSettings"]["IPAddress"], "192.168.65.2")
        self.assertEqual(
            obj["NetworkSettings"]["Networks"]["private"],
            {
                "IPAddress": "192.168.65.2",
                "Gateway": "192.168.65.1",
                "GlobalIPv6Address": "fd00::2",
                "MacAddress": "02:00:00:00:00:02",
            },
        )

    def test_multiple_containers_preserve_request_order(self) -> None:
        self.run_container("first")
        self.run_container("second")
        result = self.docker(
            "inspect", "--format", "{{.Name}}={{.State.Status}}", "second", "first"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(), ["second=running", "first=running"]
        )

    def test_container_inspect_alias_and_type_container(self) -> None:
        self.run_container("alias")
        alias = self.docker(
            "container", "inspect", "-f", "{{.State.Running}}", "alias"
        )
        self.assertEqual(alias.returncode, 0, alias.stderr)
        self.assertEqual(alias.stdout.strip(), "true")

        typed = self.docker(
            "inspect", "--type=container", "--format", "{{.Name}}", "alias"
        )
        self.assertEqual(typed.returncode, 0, typed.stderr)
        self.assertEqual(typed.stdout.strip(), "alias")

    def test_empty_template_prints_one_blank_line_per_container(self) -> None:
        self.run_container("blank")
        result = self.docker("inspect", "-f=", "blank")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "\n")

    def test_created_container_uses_zero_state_timestamps(self) -> None:
        created = self.docker("create", "--name", "created", "alpine", "true")
        self.assertEqual(created.returncode, 0, created.stderr)
        result = self.docker(
            "inspect",
            "--format",
            "{{.State.StartedAt}} {{.State.FinishedAt}}",
            "created",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "0001-01-01T00:00:00Z 0001-01-01T00:00:00Z",
        )

    def test_unknown_or_composite_fields_fail_without_partial_output(self) -> None:
        self.run_container("one")
        self.run_container("two")

        unknown = self.docker(
            "inspect", "--format", "{{.Name}} {{.State.ExitCode}}", "one", "two"
        )
        self.assertEqual(unknown.returncode, 64)
        self.assertEqual(unknown.stdout, "")
        self.assertIn("unsupported inspect field: .State.ExitCode", unknown.stderr)

        composite = self.docker(
            "inspect", "--format", "{{.Config.Labels}}", "one"
        )
        self.assertEqual(composite.returncode, 64)
        self.assertEqual(composite.stdout, "")
        self.assertIn("use '{{json .Config.Labels}}'", composite.stderr)

    def test_unsupported_template_features_and_options_fail_clearly(self) -> None:
        self.run_container("unsupported")
        cases = (
            ("--format", "{{if .State.Running}}yes{{end}}", "unsupported"),
            ("--format", "{{.Config.Labels | json}}", "unsupported"),
            ("--format", "{{index .Config.Labels \"role\"}}", "unsupported"),
            ("--format", "{{.State.Running", "unsupported"),
            ("--type", "image", "unsupported"),
            ("--size", "unsupported"),
        )
        for args in cases:
            with self.subTest(args=args):
                result = self.docker("inspect", *args)
                self.assertEqual(result.returncode, 64)
                self.assertEqual(result.stdout, "")
                self.assertIn("docker-for-apple-container:", result.stderr)


if __name__ == "__main__":
    import unittest

    unittest.main()
