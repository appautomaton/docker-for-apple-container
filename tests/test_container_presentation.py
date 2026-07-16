from __future__ import annotations

import json

import test_cli


class ContainerPresentationTests(test_cli.ShimCLITestCase):
    """Docker container inspect, list, filter, and port presentation."""

    def run_model(self) -> None:
        result = self.docker(
            "run",
            "-d",
            "--name",
            "model",
            "--label",
            "role=api",
            "-e",
            "APP_ENV=production",
            "-w",
            "/app",
            "-u",
            "1000:1000",
            "--cpus",
            "2",
            "--memory",
            "512m",
            "--read-only",
            "--init",
            "--cap-add",
            "NET_BIND_SERVICE",
            "--cap-drop",
            "ALL",
            "--shm-size",
            "64m",
            "--dns",
            "1.1.1.1",
            "--dns-search",
            "svc.local",
            "--dns-option",
            "use-vc",
            "-v",
            "/host:/data:ro",
            "-p",
            "127.0.0.1:8080:80/tcp",
            "--network",
            "private",
            "alpine",
            "python",
            "app.py",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_container_inspect_maps_verified_apple_fields(self) -> None:
        self.run_model()
        result = self.docker("inspect", "model")
        self.assertEqual(result.returncode, 0, result.stderr)
        [obj] = json.loads(result.stdout)

        self.assertEqual(obj["Path"], "python")
        self.assertEqual(obj["Args"], ["app.py"])
        self.assertEqual(obj["Platform"], "linux")
        self.assertEqual(obj["Config"]["Env"], ["APP_ENV=production"])
        self.assertEqual(obj["Config"]["WorkingDir"], "/app")
        self.assertEqual(obj["Config"]["User"], "1000:1000")
        self.assertEqual(obj["HostConfig"]["Memory"], 512 * 1024**2)
        self.assertEqual(obj["HostConfig"]["NanoCpus"], 2_000_000_000)
        self.assertEqual(obj["HostConfig"]["ReadonlyRootfs"], True)
        self.assertEqual(obj["HostConfig"]["Init"], True)
        self.assertEqual(obj["HostConfig"]["CapAdd"], ["NET_BIND_SERVICE"])
        self.assertEqual(obj["HostConfig"]["CapDrop"], ["ALL"])
        self.assertEqual(obj["HostConfig"]["Dns"], ["1.1.1.1"])
        self.assertEqual(obj["HostConfig"]["DnsSearch"], ["svc.local"])
        self.assertEqual(obj["HostConfig"]["DnsOptions"], ["use-vc"])
        self.assertEqual(
            obj["NetworkSettings"]["Ports"]["80/tcp"],
            [{"HostIp": "127.0.0.1", "HostPort": "8080"}],
        )
        self.assertEqual(
            obj["Mounts"][0],
            {
                "Type": "bind",
                "Name": "",
                "Source": "/host",
                "Destination": "/data",
                "Driver": "",
                "Mode": "ro",
                "RW": False,
                "Propagation": "",
            },
        )

    def test_ps_uses_docker_fields_and_json(self) -> None:
        self.run_model()
        formatted = self.docker(
            "ps",
            "--format",
            "{{.Names}}|{{.Command}}|{{.Ports}}|{{.Mounts}}|{{.Networks}}",
        )
        self.assertEqual(formatted.returncode, 0, formatted.stderr)
        self.assertEqual(
            formatted.stdout.strip(),
            'model|"python app.py"|127.0.0.1:8080->80/tcp|/host|private',
        )

        default = self.docker("ps")
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertEqual(
            default.stdout.splitlines()[0],
            "CONTAINER ID\tIMAGE\tCOMMAND\tCREATED\tSTATUS\tPORTS\tNAMES",
        )

        json_rows = self.docker("ps", "--format=json")
        self.assertEqual(json_rows.returncode, 0, json_rows.stderr)
        row = json.loads(json_rows.stdout)
        self.assertEqual(row["Names"], "model")
        self.assertEqual(row["Labels"], "role=api")

    def test_ps_filters_use_apple_configuration(self) -> None:
        self.run_model()
        filters = (
            "ancestor=alpine",
            "network=private",
            "volume=/host",
            "volume=/data",
        )
        for filt in filters:
            with self.subTest(filt=filt):
                result = self.docker(
                    "ps", "--filter", filt, "--format", "{{.ID}}"
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "model")

        unsupported = self.docker("ps", "--filter", "health=healthy")
        self.assertEqual(unsupported.returncode, 64)
        self.assertIn("unsupported docker ps filter", unsupported.stderr)

    def test_port_lists_all_or_one_published_port(self) -> None:
        self.run_model()
        all_ports = self.docker("port", "model")
        self.assertEqual(all_ports.returncode, 0, all_ports.stderr)
        self.assertEqual(all_ports.stdout.strip(), "80/tcp -> 127.0.0.1:8080")

        one = self.docker("container", "port", "model", "80")
        self.assertEqual(one.returncode, 0, one.stderr)
        self.assertEqual(one.stdout.strip(), "127.0.0.1:8080")

        missing = self.docker("port", "model", "443/tcp")
        self.assertEqual(missing.returncode, 1)
        self.assertIn("no published port 443/tcp", missing.stderr)


if __name__ == "__main__":
    import unittest

    unittest.main()
