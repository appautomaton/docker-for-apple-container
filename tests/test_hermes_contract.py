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


args = sys.argv[1:]
created_mode = False
if args and args[0] == "create":
    created_mode = True
    args = ["run", "-d"] + args[1:]
if args == ["--version"]:
    print("container CLI version 1.0.0 (fake)")
    raise SystemExit(0)

if args[:2] == ["system", "status"]:
    print("apiserver is running")
    raise SystemExit(0)

if args[:2] == ["image", "inspect"]:
    print(json.dumps([{"variants": [{"config": {"config": {"Entrypoint": ["docker-entrypoint.sh"]}}}]}]))
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
    image = None
    detach = False
    command = []
    i = 1
    value_opts = {
        "--name", "--label", "-w", "--workdir", "--cwd", "-e", "--env",
        "--mount", "--tmpfs", "--memory", "--cpus", "--user", "--cap-add",
        "--cap-drop", "--platform", "--publish", "-p", "--entrypoint",
        "--cidfile", "--dns", "--dns-option", "--dns-search", "--hostname",
        "--runtime", "--shm-size", "--ulimit", "--arch",
    }
    bool_opts = {"-d", "-i", "-t", "--init", "--rm", "--read-only", "--no-dns"}
    while i < len(args):
        arg = args[i]
        key = arg.split("=", 1)[0] if arg.startswith("--") else arg
        if key in bool_opts:
            if key == "-d":
                detach = True
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
            continue
        image = arg
        command = args[i + 1:]
        break
    if not detach:
        print("RUN " + (image or "") + " " + " ".join(command))
        raise SystemExit(0)
    if not name:
        name = "fake-" + str(len(data["containers"]))
    data["containers"][name] = {
        "id": name,
        "name": name,
        "image": image or "",
        "labels": labels,
        "configuration": {
            "id": name,
            "labels": labels,
            "image": {"reference": image or ""},
        },
        "status": {"state": "created" if created_mode else "running"},
    }
    save(data)
    print(name)
    raise SystemExit(0)

if args and args[0] == "list":
    data = load()
    include_all = "--all" in args
    rows = []
    for item in data["containers"].values():
        if include_all or item["status"]["state"] == "running":
            rows.append(item)
    print(json.dumps(rows))
    raise SystemExit(0)

if args and args[0] == "inspect":
    data = load()
    ident = args[-1]
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

if args and args[:2] == ["image", "list"]:
    print("IMAGE-LIST " + " ".join(args[2:]))
    raise SystemExit(0)

if args and args[:2] == ["image", "rm"]:
    print("IMAGE-RM " + " ".join(args[2:]))
    raise SystemExit(0)

if args and args[:2] == ["image", "pull"]:
    print("IMAGE-PULL " + " ".join(args[2:]))
    raise SystemExit(0)

if args and len(args) >= 2 and args[0] == "image" and args[1] in ("tag", "push", "save", "load"):
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


class HermesContractTests(unittest.TestCase):
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
            }
        )

    def docker(self, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(DOCKER), *args],
            input=input_text,
            text=True,
            capture_output=True,
            env=self.env,
            check=False,
        )

    def test_hermes_lifecycle_contract(self) -> None:
        version = self.docker("version")
        self.assertEqual(version.returncode, 0, version.stderr)

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

        ps_ids = self.docker("ps", "-a", "--filter", "label=hermes-agent=1", "--format", "{{.ID}}")
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

        before_stop = self.docker("inspect", "--format", "{{.State.FinishedAt}}", "hermes-test")
        self.assertEqual(before_stop.returncode, 0, before_stop.stderr)
        self.assertEqual(before_stop.stdout.strip(), "0001-01-01T00:00:00Z")

        exec_result = self.docker("exec", "-i", "-e", "TEST=1", "hermes-test", "bash", "-c", "cat", input_text="hello\n")
        self.assertEqual(exec_result.returncode, 0, exec_result.stderr)
        self.assertEqual(exec_result.stdout, "hello\n")

        stop = self.docker("stop", "-t", "10", "hermes-test")
        self.assertEqual(stop.returncode, 0, stop.stderr)

        after_stop = self.docker("inspect", "--format", "{{.State.FinishedAt}}", "hermes-test")
        self.assertEqual(after_stop.returncode, 0, after_stop.stderr)
        self.assertEqual(after_stop.stdout.strip(), "0001-01-01T00:00:00Z")

        exited = self.docker("ps", "-a", "--filter", "status=exited", "--format", "{{.ID}}")
        self.assertEqual(exited.returncode, 0, exited.stderr)
        self.assertEqual(exited.stdout.strip(), "hermes-test")

        start = self.docker("start", "hermes-test")
        self.assertEqual(start.returncode, 0, start.stderr)

        rm = self.docker("rm", "-f", "hermes-test")
        self.assertEqual(rm.returncode, 0, rm.stderr)

        gone = self.docker("ps", "-a", "--filter", "label=hermes-agent=1", "--format", "{{.ID}}")
        self.assertEqual(gone.returncode, 0, gone.stderr)
        self.assertEqual(gone.stdout.strip(), "")

    def test_network_none_is_refused(self) -> None:
        result = self.docker("run", "-d", "--network=none", "alpine", "sleep", "infinity")
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
            "hermes-python-node:py3.13-node22",
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
                "hermes-python-node:py3.13-node22",
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
        result = self.docker("run", "--rm", "hermes-python-node:py3.13-node22", "python", "--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "RUN hermes-python-node:py3.13-node22 python --version")

    def test_shim_does_not_write_persistent_state(self) -> None:
        run = self.docker("run", "-d", "--name", "stateless-test", "--label", "hermes-agent=1", "alpine", "sleep", "infinity")
        self.assertEqual(run.returncode, 0, run.stderr)

        shim_state = self.root / "shim-state"
        self.assertFalse(shim_state.exists(), "shim must not write its own state directory")

    def test_direct_apple_removal_does_not_leave_phantom_container(self) -> None:
        run = self.docker("run", "-d", "--name", "stale-test", "--label", "hermes-agent=1", "alpine", "sleep", "infinity")
        self.assertEqual(run.returncode, 0, run.stderr)

        fake_state = json.loads((self.root / "fake-state.json").read_text())
        fake_state["containers"].pop("stale-test")
        (self.root / "fake-state.json").write_text(json.dumps(fake_state))

        ps_ids = self.docker("ps", "-a", "--filter", "label=hermes-agent=1", "--format", "{{.ID}}")
        self.assertEqual(ps_ids.returncode, 0, ps_ids.stderr)
        self.assertEqual(ps_ids.stdout.strip(), "")

    def test_run_flags_apple_lacks_are_refused(self) -> None:
        for flag, value in (("--add-host", "db:10.0.0.1"), ("--hostname", "box")):
            result = self.docker("run", "-d", flag, value, "alpine", "sleep", "infinity")
            self.assertEqual(result.returncode, 64, f"{flag}: {result.stderr}")
            self.assertIn("Apple container", result.stderr)

    def test_logs_translates_tail_and_follow(self) -> None:
        result = self.docker("logs", "-f", "--tail", "100", "hermes-test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "LOGS --follow -n 100 hermes-test")

    def test_logs_tail_all_omits_dash_n(self) -> None:
        result = self.docker("logs", "--tail", "all", "hermes-test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "LOGS hermes-test")

    def test_logs_unsupported_flag_is_refused(self) -> None:
        result = self.docker("logs", "--since", "1h", "hermes-test")
        self.assertEqual(result.returncode, 64)

    def test_stats_no_stream_passes_through(self) -> None:
        result = self.docker("stats", "--no-stream", "hermes-test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "STATS --no-stream hermes-test")

    def test_stats_go_template_format_is_refused(self) -> None:
        result = self.docker("stats", "--format", "{{.CPUPerc}}", "hermes-test")
        self.assertEqual(result.returncode, 64)
        self.assertIn("json|table|yaml|toml", result.stderr)

    def test_cp_basic_passes_through(self) -> None:
        result = self.docker("cp", "foo.txt", "hermes-test:/tmp/foo.txt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "COPY foo.txt hermes-test:/tmp/foo.txt")

    def test_cp_archive_flag_is_refused(self) -> None:
        result = self.docker("cp", "-a", "foo.txt", "hermes-test:/tmp")
        self.assertEqual(result.returncode, 64)

    def test_images_go_template_format_is_refused(self) -> None:
        result = self.docker("images", "--format", "{{.Repository}}")
        self.assertEqual(result.returncode, 64)
        self.assertIn("json|table|yaml|toml", result.stderr)

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
        self.assertEqual(result.stdout.strip(), "REGISTRY login --username me reg.example.com")

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
            "create", "--name", "c1", "--label", "hermes-agent=1", "alpine", "sleep", "infinity"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "c1")
        # Created but not running: it appears in `ps -a` with state "created".
        ps_all = self.docker(
            "ps", "-a", "--filter", "label=hermes-agent=1", "--format", "{{.ID}}\t{{.State}}"
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
