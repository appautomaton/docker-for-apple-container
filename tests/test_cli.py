from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCKER = ROOT / "bin" / "docker"


FAKE_CONTAINER = r"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

STATE = Path(os.environ["FAKE_CONTAINER_STATE"])
CALL_LOG = os.environ.get("FAKE_CONTAINER_CALL_LOG")


def load():
    try:
        return json.loads(STATE.read_text())
    except FileNotFoundError:
        return {"containers": {}}


def save(data):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(data))


def take(args, i):
    if "=" in args[i] and args[i].startswith("--"):
        return args[i].split("=", 1)[1], i + 1
    return args[i + 1], i + 2


def omit_path(item, path):
    parts = path.split(".")
    current = item
    for part in parts[:-1]:
        current = current.get(part)
        if not isinstance(current, dict):
            return
    current.pop(parts[-1], None)


def image_record(reference):
    fill = "b" if "multi" not in reference else "c"
    digest = "sha256:" + fill * 64
    entrypoint = ["docker-entrypoint.sh"]
    variants = []
    for arch in ("arm64", "amd64"):
        variant_entrypoint = (
            [f"/{arch}-entrypoint"] if "multi" in reference else entrypoint
        )
        variants.append(
            {
                "digest": "sha256:" + ("d" if arch == "arm64" else "e") * 64,
                "platform": {"os": "linux", "architecture": arch},
                "size": 12345678,
                "config": {
                    "architecture": arch,
                    "os": "linux",
                    "created": "2026-01-02T03:04:05Z",
                    "config": {
                        "Entrypoint": variant_entrypoint,
                        "Cmd": ["serve"],
                        "Env": ["PATH=/usr/bin", "APP_ENV=production"],
                        "WorkingDir": "/app",
                        "User": "1000:1000",
                        "Labels": {"org.example.role": "app"},
                    },
                    "rootfs": {
                        "type": "layers",
                        "diff_ids": ["sha256:" + "f" * 64],
                    },
                },
            }
        )
    return {
        "id": digest,
        "configuration": {
            "name": reference,
            "creationDate": "2026-01-02T03:04:05Z",
            "descriptor": {
                "digest": digest,
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "size": 12345678,
            },
        },
        "variants": variants,
    }


def memory_bytes(value):
    units = {"K": 1024, "M": 1024**2, "G": 1024**3}
    suffix = value[-1:].upper()
    return int(value[:-1]) * units[suffix] if suffix in units else int(value)


def mount_record(value):
    fields = {}
    flags = set()
    for part in value.split(","):
        if "=" in part:
            key, val = part.split("=", 1)
            fields[key] = val
        else:
            flags.add(part)
    mount_type = fields.get("type", "bind")
    encoded_type = {"tmpfs": {}} if mount_type == "tmpfs" else {"virtiofs": {}}
    options = ["ro"] if "readonly" in flags else []
    return {
        "type": encoded_type,
        "source": fields.get("source", "tmpfs" if mount_type == "tmpfs" else ""),
        "destination": fields.get("target", ""),
        "options": options,
    }


def publish_record(value):
    base, _, proto = value.partition("/")
    protocol = proto or "tcp"
    parts = base.split(":")
    if len(parts) == 2:
        host_ip = "0.0.0.0"
        host_port, container_port = parts
    else:
        host_ip, host_port, container_port = parts[-3:]
    return {
        "hostAddress": host_ip,
        "hostPort": int(host_port),
        "containerPort": int(container_port),
        "proto": protocol,
        "count": 1,
    }


args = sys.argv[1:]
if CALL_LOG:
    log = Path(CALL_LOG)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as handle:
        handle.write(json.dumps(args) + "\n")
created_mode = False
if args and args[0] == "create":
    created_mode = True
    args = ["run", "-d"] + args[1:]
if args == ["--version"]:
    print("container CLI version 1.1.0 (fake)")
    raise SystemExit(0)

if args[:2] == ["system", "status"]:
    print("apiserver is running")
    raise SystemExit(0)

if args[:2] == ["image", "inspect"]:
    images = args[2:]
    print(json.dumps([image_record(image) for image in images]))
    raise SystemExit(0)

if args[:2] == ["image", "list"]:
    print(
        json.dumps(
            [
                image_record("example/app:1.0"),
                image_record("example/multi:latest"),
            ]
        )
    )
    raise SystemExit(0)

if args and args[0] == "build":
    if args[1:] == ["--help"]:
        print("fake container build help")
        raise SystemExit(0)
    Path(os.environ["FAKE_CONTAINER_BUILD_ARGS"]).write_text(json.dumps(args))
    print("built")
    raise SystemExit(0)

if args and args[0] == "run":
    data = load()
    name = None
    labels = {}
    networks = []
    image = None
    detach = False
    command = []
    environment = []
    working_dir = "/"
    user = "0:0"
    terminal = False
    mounts = []
    published_ports = []
    cpus = 4
    memory = 1024**3
    read_only = False
    use_init = False
    cap_add = []
    cap_drop = []
    shm_size = 0
    dns = {"nameservers": [], "searchDomains": [], "options": []}
    platform = {"os": "linux", "architecture": "arm64", "variant": ""}
    entrypoint = None
    i = 1
    value_opts = {
        "--name", "--label", "-w", "--workdir", "--cwd", "-e", "--env",
        "--mount", "--tmpfs", "--memory", "--cpus", "--user", "--cap-add",
        "--cap-drop", "--platform", "--publish", "-p", "--entrypoint",
        "--cidfile", "--dns", "--dns-option", "--dns-search", "--hostname",
        "--runtime", "--shm-size", "--ulimit", "--arch", "--network",
    }
    bool_opts = {"-d", "-i", "-t", "--init", "--rm", "--read-only", "--no-dns"}
    while i < len(args):
        arg = args[i]
        key = arg.split("=", 1)[0] if arg.startswith("--") else arg
        if key in bool_opts:
            if key == "-d":
                detach = True
            elif key == "-t":
                terminal = True
            elif key == "--init":
                use_init = True
            elif key == "--read-only":
                read_only = True
            i += 1
            continue
        if key in value_opts:
            value, i = take(args, i)
            if key == "--cpus" and "." in value:
                print("fake container rejected decimal cpus", file=sys.stderr)
                raise SystemExit(64)
            if key == "--name":
                name = value
            if key == "--label":
                k, _, v = value.partition("=")
                labels[k] = v
            if key == "--network":
                networks.append(value)
            if key in ("-e", "--env"):
                environment.append(value)
            if key in ("-w", "--workdir", "--cwd"):
                working_dir = value
            if key == "--user":
                user = value
            if key == "--mount":
                mounts.append(mount_record(value))
            if key == "--tmpfs":
                mounts.append(
                    {
                        "type": {"tmpfs": {}},
                        "source": "tmpfs",
                        "destination": value,
                        "options": [],
                    }
                )
            if key == "--memory":
                memory = memory_bytes(value)
            if key == "--cpus":
                cpus = int(value)
            if key == "--cap-add":
                cap_add.append(value)
            if key == "--cap-drop":
                cap_drop.append(value)
            if key in ("--publish", "-p"):
                published_ports.append(publish_record(value))
            if key == "--dns":
                dns["nameservers"].append(value)
            if key == "--dns-search":
                dns["searchDomains"].append(value)
            if key == "--dns-option":
                dns["options"].append(value)
            if key == "--shm-size":
                shm_size = memory_bytes(value)
            if key == "--platform":
                os_name, arch, *variant = value.split("/")
                platform = {
                    "os": os_name,
                    "architecture": arch,
                    "variant": variant[0] if variant else "",
                }
            if key == "--entrypoint":
                entrypoint = value
            continue
        image = arg
        command = args[i + 1:]
        break
    if not detach:
        print("RUN " + (image or "") + " " + " ".join(command))
        raise SystemExit(0)
    if not name:
        name = "fake-" + str(len(data["containers"]))
    if not networks:
        networks = ["default"]
    host = len(data["containers"]) + 2
    executable = entrypoint or (command[0] if command else "/bin/sh")
    process_args = command if entrypoint else command[1:]
    if user.replace(":", "").isdigit():
        uid, _, gid = user.partition(":")
        encoded_user = {"id": {"uid": int(uid), "gid": int(gid or uid)}}
    else:
        encoded_user = {"raw": {"userString": user}}
    data["containers"][name] = {
        "id": name,
        "configuration": {
            "id": name,
            "labels": labels,
            "image": {
                "reference": image or "",
                "descriptor": {"digest": "sha256:" + "a" * 64},
            },
            "initProcess": {
                "executable": executable,
                "arguments": process_args,
                "environment": environment,
                "workingDirectory": working_dir,
                "terminal": terminal,
                "user": encoded_user,
                "supplementalGroups": [],
                "rlimits": [],
            },
            "mounts": mounts,
            "publishedPorts": published_ports,
            "resources": {
                "cpus": cpus,
                "memoryInBytes": memory,
                "cpuOverhead": 1,
            },
            "dns": dns,
            "platform": platform,
            "readOnly": read_only,
            "useInit": use_init,
            "capAdd": cap_add,
            "capDrop": cap_drop,
            "shmSize": shm_size,
            "creationDate": "2026-01-01T00:00:00Z",
        },
        "status": {
            "state": "created" if created_mode else "running",
            "startedDate": None if created_mode else "2026-01-01T00:00:05Z",
            "networks": [
                {
                    "network": network,
                    "hostname": name,
                    "ipv4Address": f"192.168.65.{host}/24",
                    "ipv4Gateway": "192.168.65.1",
                    "ipv6Address": f"fd00::{host}/64",
                    "macAddress": f"02:00:00:00:00:{host:02x}",
                }
                for network in networks
            ],
        },
    }
    save(data)
    print(name)
    raise SystemExit(0)

if args and args[0] == "list":
    data = load()
    include_all = "--all" in args
    rows = []
    for ident, item in data["containers"].items():
        if include_all or item["status"]["state"] == "running":
            row = json.loads(json.dumps(item))
            for path in (data.get("list_omit") or {}).get(ident, []):
                omit_path(row, path)
            rows.append(row)
    print(json.dumps(rows))
    raise SystemExit(0)

if args and args[0] == "inspect":
    data = load()
    ident = args[-1]
    if ident in data.get("inspect_fail", []):
        print("Inspect failed", file=sys.stderr)
        raise SystemExit(1)
    if ident in data.get("inspect_malformed", []):
        print("not-json")
        raise SystemExit(0)
    item = data["containers"].get(ident)
    if not item:
        print("No such container", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps([item]))
    raise SystemExit(0)

if args and args[0] == "exec":
    i = 1
    interactive = False
    while i < len(args):
        arg = args[i]
        if arg == "-i":
            interactive = True
            i += 1
        elif arg == "-t":
            i += 1
        elif arg in ("-e", "-w"):
            i += 2
        else:
            break
    ident = args[i]
    cmd = args[i + 1:]
    if ident not in load()["containers"]:
        print("No such container", file=sys.stderr)
        raise SystemExit(1)
    if interactive and cmd[-1:] == ["cat"]:
        sys.stdout.write(sys.stdin.read())
    else:
        print("EXEC " + " ".join(cmd))
    raise SystemExit(0)

if args and args[0] == "stop":
    data = load()
    ident = args[-1]
    data["containers"][ident]["status"]["state"] = "stopped"
    save(data)
    print(ident)
    raise SystemExit(0)

if args and args[0] == "start":
    data = load()
    ident = args[-1]
    data["containers"][ident]["status"]["state"] = "running"
    data["containers"][ident]["status"]["startedDate"] = "2026-01-01T00:00:05Z"
    save(data)
    print(ident)
    raise SystemExit(0)

if args and args[0] == "rm":
    data = load()
    ident = args[-1]
    data["containers"].pop(ident, None)
    save(data)
    print(ident)
    raise SystemExit(0)

if args and args[0] == "logs":
    print("LOGS " + " ".join(args[1:]))
    raise SystemExit(0)

if args and args[0] == "copy":
    print("COPY " + " ".join(args[1:]))
    raise SystemExit(0)

if args and args[0] == "stats":
    print("STATS " + " ".join(args[1:]))
    raise SystemExit(0)

if args and args[:2] == ["image", "rm"]:
    print("IMAGE-RM " + " ".join(args[2:]))
    raise SystemExit(0)

if args and args[:2] == ["image", "pull"]:
    print("IMAGE-PULL " + " ".join(args[2:]))
    raise SystemExit(0)

if (
    args
    and len(args) >= 2
    and args[0] == "image"
    and args[1] in ("tag", "push", "save", "load")
):
    print(args[1].upper() + " " + " ".join(args[2:]))
    raise SystemExit(0)

if args and args[:2] == ["image", "prune"]:
    print("IMAGE-PRUNE")
    raise SystemExit(0)

if args and args[0] == "kill":
    print("KILL " + " ".join(args[1:]))
    raise SystemExit(0)

if args and args[0] == "export":
    print("EXPORT " + " ".join(args[1:]))
    raise SystemExit(0)

if args and args[0] in ("network", "volume"):
    print(args[0].upper() + " " + " ".join(args[1:]))
    raise SystemExit(0)

if args and args[0] == "registry":
    print("REGISTRY " + " ".join(args[1:]))
    raise SystemExit(0)

if args and args[0] == "prune":
    print("PRUNE")
    raise SystemExit(0)

print("unsupported fake container args: " + " ".join(args), file=sys.stderr)
raise SystemExit(2)
"""


class ShimCLITestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fake = self.root / "container"
        self.fake.write_text(FAKE_CONTAINER)
        self.fake.chmod(0o755)
        self.env = os.environ.copy()
        self.env.update(
            {
                "CONTAINER_DOCKER_SHIM_CONTAINER": str(self.fake),
                # Deliberately set: tests assert the stateless shim never creates it.
                "CONTAINER_DOCKER_SHIM_STATE_DIR": str(self.root / "shim-state"),
                "FAKE_CONTAINER_STATE": str(self.root / "fake-state.json"),
                "FAKE_CONTAINER_CALL_LOG": str(self.root / "fake-calls.jsonl"),
            }
        )

    def docker(
        self, *args: str, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(DOCKER), *args],
            input=input_text,
            text=True,
            capture_output=True,
            env=self.env,
            check=False,
        )

    def container_calls(self) -> list[list[str]]:
        path = Path(self.env["FAKE_CONTAINER_CALL_LOG"])
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def clear_container_calls(self) -> None:
        Path(self.env["FAKE_CONTAINER_CALL_LOG"]).unlink(missing_ok=True)

    def update_fake_state(self, **updates: object) -> None:
        path = Path(self.env["FAKE_CONTAINER_STATE"])
        data = json.loads(path.read_text())
        data.update(updates)
        path.write_text(json.dumps(data))


class ContainerQueryTests(ShimCLITestCase):
    """Docker container-list subset backed by Apple list JSON.

    Docker behavior reference:
    https://docs.docker.com/reference/cli/docker/container/ls/
    """

    def run_container(self, name: str, *, label: str | None = None) -> None:
        args = ["run", "-d", "--name", name]
        if label is not None:
            args.extend(["--label", label])
        args.extend(["alpine", "sleep", "infinity"])
        result = self.docker(*args)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_complete_list_rows_do_not_trigger_inspect(self) -> None:
        self.run_container("running", label="role=worker")
        created = self.docker("create", "--name", "created", "alpine", "true")
        self.assertEqual(created.returncode, 0, created.stderr)
        self.clear_container_calls()

        running = self.docker("ps", "--format", "{{.ID}}")
        self.assertEqual(running.returncode, 0, running.stderr)
        self.assertEqual(running.stdout.strip(), "running")

        all_rows = self.docker(
            "ps",
            "-a",
            "--filter",
            "label=role=worker",
            "--format",
            "{{.ID}}\t{{.State}}",
        )
        self.assertEqual(all_rows.returncode, 0, all_rows.stderr)
        self.assertEqual(all_rows.stdout.strip(), "running\trunning")

        quiet = self.docker("ps", "-a", "--quiet")
        self.assertEqual(quiet.returncode, 0, quiet.stderr)
        self.assertEqual(quiet.stdout.splitlines(), ["running", "created"])

        self.assertEqual(
            self.container_calls(),
            [
                ["list", "--format", "json"],
                ["list", "--all", "--format", "json"],
                ["list", "--all", "--format", "json"],
            ],
        )

    def test_only_incomplete_list_rows_are_inspected(self) -> None:
        paths = (
            "id",
            "configuration.image.reference",
            "status.state",
            "configuration.labels",
        )
        for path in paths:
            with self.subTest(path=path):
                self.clear_container_calls()
                Path(self.env["FAKE_CONTAINER_STATE"]).unlink(missing_ok=True)
                self.run_container("complete", label="role=worker")
                self.run_container("fallback", label="role=worker")
                self.update_fake_state(list_omit={"fallback": [path]})
                self.clear_container_calls()

                result = self.docker(
                    "ps",
                    "-a",
                    "--filter",
                    "label=role=worker",
                    "--format",
                    "{{.ID}}",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    result.stdout.splitlines(), ["complete", "fallback"]
                )
                self.assertEqual(
                    self.container_calls(),
                    [
                        ["list", "--all", "--format", "json"],
                        ["inspect", "fallback"],
                    ],
                )

    def test_failed_or_malformed_fallback_keeps_list_data(self) -> None:
        for mode in ("inspect_fail", "inspect_malformed"):
            with self.subTest(mode=mode):
                self.clear_container_calls()
                Path(self.env["FAKE_CONTAINER_STATE"]).unlink(missing_ok=True)
                self.run_container("partial")
                self.update_fake_state(
                    list_omit={"partial": ["configuration.labels"]},
                    **{mode: ["partial"]},
                )
                self.clear_container_calls()

                result = self.docker(
                    "ps", "-a", "--format", "{{.ID}}\t{{.Image}}\t{{.State}}"
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "partial\talpine\trunning")
                self.assertEqual(
                    self.container_calls(),
                    [
                        ["list", "--all", "--format", "json"],
                        ["inspect", "partial"],
                    ],
                )

    def test_direct_inspect_still_calls_apple_inspect(self) -> None:
        self.run_container("direct")
        self.clear_container_calls()

        result = self.docker(
            "inspect", "--format", "{{.State.Running}}", "direct"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "true")
        self.assertEqual(self.container_calls(), [["inspect", "direct"]])


class CLIContractTests(ShimCLITestCase):
    def test_network_none_is_refused(self) -> None:
        result = self.docker(
            "run", "-d", "--network=none", "alpine", "sleep", "infinity"
        )
        self.assertEqual(result.returncode, 64)
        self.assertIn("no verified Apple container equivalent", result.stderr)

    def test_unsupported_command_is_explicit(self) -> None:
        # `commit` has no Apple container equivalent and stays refused.
        result = self.docker("commit", "abc", "img:tag")
        self.assertEqual(result.returncode, 64)
        self.assertIn("unsupported Docker command", result.stderr)

    def test_build_translates_to_apple_container_build(self) -> None:
        self.env["FAKE_CONTAINER_BUILD_ARGS"] = str(self.root / "build-args.json")
        result = self.docker(
            "build",
            "-f",
            "images/Dockerfile.py3.13-node22",
            "-t",
            "example/python-node:py3.13-node22",
            "--build-arg",
            "FOO=bar",
            "--target",
            "runtime",
            "--platform",
            "linux/arm64",
            "--no-cache",
            "--pull",
            "--progress",
            "plain",
            "--cpus",
            "1.0",
            "--memory",
            "2g",
            "images",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "built")
        args = json.loads((self.root / "build-args.json").read_text())
        self.assertEqual(
            args,
            [
                "build",
                "--file",
                "images/Dockerfile.py3.13-node22",
                "--tag",
                "example/python-node:py3.13-node22",
                "--build-arg",
                "FOO=bar",
                "--target",
                "runtime",
                "--platform",
                "linux/arm64",
                "--no-cache",
                "--pull",
                "--progress",
                "plain",
                "--cpus",
                "1",
                "--memory",
                "2G",
                "images",
            ],
        )

    def test_build_help_forwards_to_container_build_help(self) -> None:
        result = self.docker("build", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "fake container build help")

    def test_foreground_run_passes_through_output(self) -> None:
        result = self.docker(
            "run",
            "--rm",
            "example/python-node:py3.13-node22",
            "python",
            "--version",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "RUN example/python-node:py3.13-node22 python --version",
        )

    def test_shim_does_not_write_persistent_state(self) -> None:
        run = self.docker(
            "run",
            "-d",
            "--name",
            "stateless-test",
            "--label",
            "shim-test=1",
            "alpine",
            "sleep",
            "infinity",
        )
        self.assertEqual(run.returncode, 0, run.stderr)

        shim_state = self.root / "shim-state"
        self.assertFalse(
            shim_state.exists(), "shim must not write its own state directory"
        )

    def test_direct_apple_removal_does_not_leave_phantom_container(self) -> None:
        run = self.docker(
            "run",
            "-d",
            "--name",
            "stale-test",
            "--label",
            "shim-test=1",
            "alpine",
            "sleep",
            "infinity",
        )
        self.assertEqual(run.returncode, 0, run.stderr)

        fake_state = json.loads((self.root / "fake-state.json").read_text())
        fake_state["containers"].pop("stale-test")
        (self.root / "fake-state.json").write_text(json.dumps(fake_state))

        ps_ids = self.docker(
            "ps", "-a", "--filter", "label=shim-test=1", "--format", "{{.ID}}"
        )
        self.assertEqual(ps_ids.returncode, 0, ps_ids.stderr)
        self.assertEqual(ps_ids.stdout.strip(), "")

    def test_run_flags_apple_lacks_are_refused(self) -> None:
        for flag, value in (("--add-host", "db:10.0.0.1"), ("--hostname", "box")):
            result = self.docker(
                "run", "-d", flag, value, "alpine", "sleep", "infinity"
            )
            self.assertEqual(result.returncode, 64, f"{flag}: {result.stderr}")
            self.assertIn("Apple container", result.stderr)

    def test_logs_translates_tail_and_follow(self) -> None:
        result = self.docker("logs", "-f", "--tail", "100", "test-container")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "LOGS --follow -n 100 test-container")

    def test_logs_tail_all_omits_dash_n(self) -> None:
        result = self.docker("logs", "--tail", "all", "test-container")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "LOGS test-container")

    def test_logs_unsupported_flag_is_refused(self) -> None:
        result = self.docker("logs", "--since", "1h", "test-container")
        self.assertEqual(result.returncode, 64)

    def test_stats_no_stream_passes_through(self) -> None:
        result = self.docker("stats", "--no-stream", "test-container")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "STATS --no-stream test-container")

    def test_stats_go_template_format_is_refused(self) -> None:
        result = self.docker(
            "stats", "--format", "{{.CPUPerc}}", "test-container"
        )
        self.assertEqual(result.returncode, 64)
        self.assertIn("json|table|yaml|toml", result.stderr)

    def test_cp_basic_passes_through(self) -> None:
        result = self.docker("cp", "foo.txt", "test-container:/tmp/foo.txt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(), "COPY foo.txt test-container:/tmp/foo.txt"
        )

    def test_cp_archive_flag_is_refused(self) -> None:
        result = self.docker("cp", "-a", "foo.txt", "test-container:/tmp")
        self.assertEqual(result.returncode, 64)

    def test_restart_composes_stop_then_start(self) -> None:
        self.docker("run", "-d", "--name", "r1", "alpine", "sleep", "infinity")
        result = self.docker("restart", "r1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "r1")

    def test_rmi_aliases_image_rm(self) -> None:
        result = self.docker("rmi", "alpine:latest")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "IMAGE-RM alpine:latest")

    def test_export_maps_output_flag(self) -> None:
        result = self.docker("export", "-o", "/tmp/x.tar", "r1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "EXPORT -o /tmp/x.tar r1")

    def test_login_delegates_to_registry(self) -> None:
        result = self.docker("login", "-u", "me", "reg.example.com")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(), "REGISTRY login --username me reg.example.com"
        )

    def test_login_password_flag_is_refused(self) -> None:
        result = self.docker("login", "-p", "secret", "reg.example.com")
        self.assertEqual(result.returncode, 64)
        self.assertIn("--password-stdin", result.stderr)

    def test_logout_delegates_to_registry(self) -> None:
        result = self.docker("logout", "reg.example.com")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "REGISTRY logout reg.example.com")

    def test_network_ls_passes_through(self) -> None:
        result = self.docker("network", "ls")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "NETWORK ls")

    def test_network_connect_is_refused(self) -> None:
        result = self.docker("network", "connect", "net1", "ctr1")
        self.assertEqual(result.returncode, 64)
        self.assertIn("subcommand", result.stderr)

    def test_network_ls_go_template_is_refused(self) -> None:
        result = self.docker("network", "ls", "--format", "{{.Name}}")
        self.assertEqual(result.returncode, 64)
        self.assertIn("json|table|yaml|toml", result.stderr)

    def test_volume_create_passes_through(self) -> None:
        result = self.docker("volume", "create", "vol1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "VOLUME create vol1")

    def test_system_prune_runs_apple_prunes(self) -> None:
        result = self.docker("system", "prune")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PRUNE", result.stdout)
        self.assertIn("IMAGE-PRUNE", result.stdout)
        self.assertIn("NETWORK prune", result.stdout)

    def test_system_prune_volumes_adds_volume_prune(self) -> None:
        result = self.docker("system", "prune", "--volumes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("VOLUME prune", result.stdout)

    def test_system_events_is_refused(self) -> None:
        result = self.docker("system", "events")
        self.assertEqual(result.returncode, 64)

    def test_info_reports_apple_driver(self) -> None:
        result = self.docker("info", "--format", "{{.Driver}}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "apple-container")

    def test_kill_passes_through(self) -> None:
        result = self.docker("kill", "-s", "TERM", "k1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "KILL -s TERM k1")

    def test_pull_forwards_to_image_pull(self) -> None:
        result = self.docker("pull", "alpine:latest")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "IMAGE-PULL alpine:latest")

    def test_create_maps_to_container_create(self) -> None:
        result = self.docker(
            "create",
            "--name",
            "c1",
            "--label",
            "shim-test=1",
            "alpine",
            "sleep",
            "infinity",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "c1")
        # Created but not running: it appears in `ps -a` with state "created".
        ps_all = self.docker(
            "ps",
            "-a",
            "--filter",
            "label=shim-test=1",
            "--format",
            "{{.ID}}\t{{.State}}",
        )
        self.assertEqual(ps_all.stdout.strip(), "c1\tcreated")

    def test_create_detach_is_refused(self) -> None:
        result = self.docker("create", "-d", "alpine")
        self.assertEqual(result.returncode, 64)

    def test_create_storage_opt_probe_returns_125(self) -> None:
        result = self.docker("create", "--storage-opt", "size=10G", "alpine")
        self.assertEqual(result.returncode, 125)

    def test_tag_forwards_to_image_tag(self) -> None:
        result = self.docker("tag", "alpine:latest", "alpine:mine")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "TAG alpine:latest alpine:mine")

    def test_push_forwards_to_image_push(self) -> None:
        result = self.docker("push", "reg.example.com/img:tag")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "PUSH reg.example.com/img:tag")

    def test_save_forwards_to_image_save(self) -> None:
        result = self.docker("save", "-o", "/tmp/img.tar", "alpine:latest")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "SAVE -o /tmp/img.tar alpine:latest")

    def test_load_forwards_to_image_load(self) -> None:
        result = self.docker("load", "-i", "/tmp/img.tar")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "LOAD -i /tmp/img.tar")


if __name__ == "__main__":
    unittest.main()
