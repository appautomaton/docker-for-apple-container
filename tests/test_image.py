from __future__ import annotations

import json

import test_cli


class ImagePresentationTests(test_cli.ShimCLITestCase):
    """Docker image output backed by Apple image JSON.

    Docker behavior references:
    https://docs.docker.com/reference/cli/docker/image/inspect/
    https://docs.docker.com/reference/cli/docker/image/ls/
    """

    def test_image_inspect_default_is_docker_shaped(self) -> None:
        result = self.docker("image", "inspect", "example/app:1.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        [obj] = json.loads(result.stdout)

        self.assertEqual(obj["Id"], "sha256:" + "b" * 64)
        self.assertEqual(obj["RepoTags"], ["example/app:1.0"])
        self.assertEqual(
            obj["RepoDigests"], ["example/app@sha256:" + "b" * 64]
        )
        self.assertEqual(obj["Created"], "2026-01-02T03:04:05Z")
        self.assertEqual(obj["Size"], 12345678)
        self.assertEqual(obj["Architecture"], "arm64")
        self.assertEqual(obj["Os"], "linux")
        self.assertEqual(obj["Config"]["Entrypoint"], ["docker-entrypoint.sh"])
        self.assertEqual(obj["Config"]["Cmd"], ["serve"])
        self.assertEqual(obj["Config"]["WorkingDir"], "/app")
        self.assertEqual(obj["RootFS"]["Layers"], ["sha256:" + "f" * 64])

    def test_image_inspect_formats_multiple_images_in_order(self) -> None:
        result = self.docker(
            "image",
            "inspect",
            "-f",
            "{{.RepoTags}}",
            "example/app:1.0",
            "example/multi:latest",
        )
        self.assertEqual(result.returncode, 64)
        self.assertEqual(result.stdout, "")
        self.assertIn("use '{{json .RepoTags}}'", result.stderr)

        rendered = self.docker(
            "image",
            "inspect",
            "--format",
            "{{json .RepoTags}}",
            "example/app:1.0",
            "example/multi:latest",
        )
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.assertEqual(
            rendered.stdout.splitlines(),
            ['["example/app:1.0"]', '["example/multi:latest"]'],
        )

    def test_image_inspect_platform_and_format_aliases(self) -> None:
        commands = (
            ("-f", "{{json .Config.Entrypoint}}"),
            ("-f={{json .Config.Entrypoint}}",),
            ("--format={{json .Config.Entrypoint}}",),
        )
        for format_args in commands:
            with self.subTest(format_args=format_args):
                result = self.docker(
                    "image",
                    "inspect",
                    "--platform",
                    "linux/amd64",
                    *format_args,
                    "example/multi:latest",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), '["/amd64-entrypoint"]')

        missing = self.docker(
            "image",
            "inspect",
            "--platform=linux/s390x",
            "example/multi:latest",
        )
        self.assertEqual(missing.returncode, 1)
        self.assertIn("no platform matching linux/s390x", missing.stderr)

    def test_image_inspect_json_format_prints_one_object_per_image(self) -> None:
        result = self.docker(
            "image",
            "inspect",
            "--format=json",
            "example/app:1.0",
            "example/multi:latest",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        objects = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(
            [obj["RepoTags"][0] for obj in objects],
            ["example/app:1.0", "example/multi:latest"],
        )

    def test_image_list_default_quiet_format_and_digests(self) -> None:
        default = self.docker("images")
        self.assertEqual(default.returncode, 0, default.stderr)
        lines = default.stdout.splitlines()
        self.assertEqual(lines[0], "REPOSITORY\tTAG\tIMAGE ID\tCREATED\tSIZE")
        self.assertIn("example/app\t1.0\tbbbbbbbbbbbb", lines[1])

        quiet = self.docker("image", "ls", "--quiet")
        self.assertEqual(quiet.returncode, 0, quiet.stderr)
        self.assertEqual(quiet.stdout.splitlines(), ["bbbbbbbbbbbb", "cccccccccccc"])

        formatted = self.docker(
            "images", "--format", "{{.Repository}}:{{.Tag}} {{.Size}}"
        )
        self.assertEqual(formatted.returncode, 0, formatted.stderr)
        self.assertEqual(
            formatted.stdout.splitlines(),
            ["example/app:1.0 12.3MB", "example/multi:latest 12.3MB"],
        )

        digests = self.docker("images", "--digests", "--no-trunc")
        self.assertEqual(digests.returncode, 0, digests.stderr)
        self.assertEqual(
            digests.stdout.splitlines()[0],
            "REPOSITORY\tTAG\tDIGEST\tIMAGE ID\tCREATED\tSIZE",
        )
        self.assertIn("sha256:" + "b" * 64, digests.stdout)

    def test_image_list_json_and_unsupported_fields(self) -> None:
        result = self.docker("images", "--format=json")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(rows[0]["Repository"], "example/app")
        self.assertEqual(rows[0]["Tag"], "1.0")

        unsupported = self.docker("images", "--format", "{{.Containers}}")
        self.assertEqual(unsupported.returncode, 64)
        self.assertEqual(unsupported.stdout, "")
        self.assertIn("unsupported image list field: .Containers", unsupported.stderr)


if __name__ == "__main__":
    import unittest

    unittest.main()
