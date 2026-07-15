"""Tests for the `docker compose` subset.

Two layers:

* Pure-unit tests for the YAML parser, interpolation, and translation
  helpers — no subprocess, no fake binary.
* End-to-end tests that drive `bin/docker compose ...` against a fake
  `container` binary which records state in a JSON file, so we can assert the
  stateless label-based orchestration without starting real VMs.
"""

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

sys.path.insert(0, str(ROOT / "src"))

from container_docker_shim import compose  # noqa: E402


# A fake `container` that models just enough of Apple's CLI for compose:
# run (records labels, entrypoint/cmd + a synthetic IP), list --format json,
# inspect (with a status.networks IPv4), image inspect (a fixed
# ENTRYPOINT/CMD), stop/rm, network/volume create/inspect/list/rm, exec.
FAKE_CONTAINER = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

STATE = Path(os.environ["FAKE_CONTAINER_STATE"])


def load():
    try:
        return json.loads(STATE.read_text())
    except FileNotFoundError:
        return {"containers": {}, "networks": {}, "volumes": {}, "ip": 1, "exec_log": []}


def save(d):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(d))


def take(a, i):
    if a[i].startswith("--") and "=" in a[i]:
        return a[i].split("=", 1)[1], i + 1
    return a[i + 1], i + 2


a = sys.argv[1:]

if a[:1] == ["--version"]:
    print("container CLI version 1.1.0 (fake)"); raise SystemExit(0)
if a[:2] == ["system", "status"]:
    print("apiserver is running"); raise SystemExit(0)

if a[:1] == ["run"]:
    d = load()
    name = None; labels = {}; networks = []; image = None; cmd = []; entry = None
    value = {"--name","--label","--network","-e","--env","-p","--publish","-v",
             "--mount","--tmpfs","--cpus","--memory","--user","-w","--workdir",
             "--cwd","--entrypoint","--platform","--cap-add","--cap-drop",
             "--ulimit","--shm-size"}
    boolean = {"-d","-i","-t","--init","--read-only","--rm","--no-dns"}
    i = 1
    while i < len(a):
        arg = a[i]
        key = arg.split("=",1)[0] if arg.startswith("--") else arg
        if key in boolean:
            i += 1; continue
        if key in value:
            v, i = take(a, i)
            if key == "--name": name = v
            elif key == "--label":
                k, _, val = v.partition("="); labels[k] = val
            elif key == "--network": networks.append(v)
            elif key == "--entrypoint": entry = v
            continue
        image = arg; cmd = a[i+1:]; break
    if not name:
        name = "fake-%d" % len(d["containers"])
    ip = "192.168.65.%d" % d["ip"]; d["ip"] += 1
    d["containers"][name] = {
        "id": name, "name": name, "image": image or "",
        "labels": labels, "networks": networks,
        "entrypoint": entry, "cmd": cmd,
        "configuration": {"id": name, "labels": labels,
                          "image": {"reference": image or ""}},
        "status": {"state": "running",
                   "networks": [{"network": networks[0] if networks else "default",
                                 "ipv4Address": ip + "/24",
                                 "ipv4Gateway": "192.168.65.1"}]},
    }
    save(d); print(name); raise SystemExit(0)

if a[:2] == ["image", "inspect"]:
    print(json.dumps([{"name": a[-1], "variants": [
        {"platform": {"os": "linux", "architecture": "arm64"},
         "config": {"config": {"Entrypoint": ["/entry"], "Cmd": ["serve"]}}}]}]))
    raise SystemExit(0)

if a[:1] == ["list"]:
    d = load(); allc = "--all" in a
    rows = [c for c in d["containers"].values()
            if allc or c["status"]["state"] == "running"]
    print(json.dumps(rows)); raise SystemExit(0)

if a[:1] == ["inspect"]:
    d = load(); ident = a[-1]; c = d["containers"].get(ident)
    if not c:
        print("No such container", file=sys.stderr); raise SystemExit(1)
    print(json.dumps([c])); raise SystemExit(0)

if a[:1] == ["stop"]:
    d = load(); ident = a[-1]
    if ident in d["containers"]:
        d["containers"][ident]["status"]["state"] = "stopped"
    save(d); print(ident); raise SystemExit(0)

if a[:1] == ["rm"]:
    d = load(); ident = a[-1]; d["containers"].pop(ident, None)
    save(d); print(ident); raise SystemExit(0)

if a[:1] == ["exec"]:
    d = load()
    # skip exec opts, find container id then command
    i = 1
    while i < len(a) and a[i].startswith("-"):
        i += 2 if a[i] in ("-e","-w") else 1
    ident = a[i]; cmd = a[i+1:]
    if ident in os.environ.get("FAKE_NO_SHELL", "").split(","):
        print("no such executable: sh", file=sys.stderr); raise SystemExit(1)
    d["exec_log"].append({"container": ident, "cmd": cmd})
    save(d); raise SystemExit(0)

if a[:1] in (["cp"], ["copy"]):
    d = load(); src, dst = a[1], a[2]
    if ":" in src:  # container -> local
        ident, _, path = src.partition(":")
        files = d["containers"].get(ident, {}).get("files", {})
        Path(dst).write_text(files.get(path, "127.0.0.1 localhost\n"))
    else:           # local -> container
        ident, _, path = dst.partition(":")
        d["containers"].setdefault(ident, {}).setdefault("files", {})[path] = Path(src).read_text()
        save(d)
    raise SystemExit(0)

if a[:1] == ["logs"]:
    print("LOGS " + " ".join(x for x in a[1:] if not x.startswith("-")))
    raise SystemExit(0)

if a[:2] == ["network", "create"]:
    d = load(); labels = {}; name = None; i = 2
    while i < len(a):
        if a[i] == "--label":
            v, i = take(a, i); k,_,val = v.partition("="); labels[k]=val; continue
        if a[i] == "--internal": i += 1; continue
        name = a[i]; i += 1
    d["networks"][name] = {"configuration": {"name": name, "labels": labels}, "id": name}
    save(d); print(name); raise SystemExit(0)

if a[:2] == ["network", "inspect"]:
    d = load(); name = a[-1]
    if name not in d["networks"]:
        print("no such network", file=sys.stderr); raise SystemExit(1)
    print(json.dumps([d["networks"][name]])); raise SystemExit(0)

if a[:2] == ["network", "list"]:
    d = load(); print(json.dumps(list(d["networks"].values()))); raise SystemExit(0)

if a[:2] == ["network", "rm"]:
    d = load(); name = a[-1]; d["networks"].pop(name, None)
    save(d); print(name); raise SystemExit(0)

if a[:2] == ["volume", "create"]:
    d = load(); labels = {}; name = None; i = 2
    while i < len(a):
        if a[i] == "--label":
            v, i = take(a, i); k,_,val = v.partition("="); labels[k]=val; continue
        name = a[i]; i += 1
    d["volumes"][name] = {"configuration": {"name": name, "labels": labels}, "id": name}
    save(d); print(name); raise SystemExit(0)

if a[:2] == ["volume", "inspect"]:
    d = load(); name = a[-1]
    if name not in d["volumes"]:
        print("no such volume", file=sys.stderr); raise SystemExit(1)
    print(json.dumps([d["volumes"][name]])); raise SystemExit(0)

if a[:2] == ["volume", "list"]:
    d = load(); print(json.dumps(list(d["volumes"].values()))); raise SystemExit(0)

if a[:2] == ["volume", "rm"]:
    d = load(); name = a[-1]; d["volumes"].pop(name, None)
    save(d); print(name); raise SystemExit(0)

print("unsupported fake container args: " + " ".join(a), file=sys.stderr)
raise SystemExit(2)
"""


# --------------------------------------------------------------------------- #
# Pure-unit tests (no subprocess)
# --------------------------------------------------------------------------- #


class ParserTests(unittest.TestCase):
    def test_nested_map_and_seq(self) -> None:
        doc = compose.parse_yaml(
            textwrap.dedent(
                """
                services:
                  web:
                    image: nginx:latest
                    ports:
                      - "8080:80"
                    environment:
                      FOO: bar
                """
            )
        )
        self.assertEqual(doc["services"]["web"]["image"], "nginx:latest")
        self.assertEqual(doc["services"]["web"]["ports"], ["8080:80"])
        self.assertEqual(doc["services"]["web"]["environment"], {"FOO": "bar"})

    def test_flow_collections(self) -> None:
        doc = compose.parse_yaml("services:\n  a:\n    depends_on: [x, y]\n    networks: {n: null}\n")
        self.assertEqual(doc["services"]["a"]["depends_on"], ["x", "y"])
        self.assertEqual(doc["services"]["a"]["networks"], {"n": None})

    def test_scalar_typing(self) -> None:
        doc = compose.parse_yaml("a: true\nb: 42\nc: 1.5\nd: ~\ne: 'quoted'\n")
        self.assertEqual(doc, {"a": True, "b": 42, "c": 1.5, "d": None, "e": "quoted"})

    def test_comments_and_blank_lines(self) -> None:
        doc = compose.parse_yaml("# top\nservices:\n  a:  # inline\n    image: x  # trailing\n")
        self.assertEqual(doc["services"]["a"]["image"], "x")

    def test_list_of_maps(self) -> None:
        doc = compose.parse_yaml(
            textwrap.dedent(
                """
                services:
                  a:
                    image: x
                    ulimits:
                      - nofile=1024
                """
            )
        )
        self.assertEqual(doc["services"]["a"]["ulimits"], ["nofile=1024"])


class InterpolationTests(unittest.TestCase):
    def test_default_value(self) -> None:
        self.assertEqual(compose._interpolate_str("${X:-fallback}", {}), "fallback")
        self.assertEqual(compose._interpolate_str("${X:-fallback}", {"X": "set"}), "set")

    def test_required_missing_raises(self) -> None:
        with self.assertRaises(compose.ShimError):
            compose._interpolate_str("${X:?must set}", {})

    def test_literal_dollar(self) -> None:
        self.assertEqual(compose._interpolate_str("price is $$5", {}), "price is $5")

    def test_bare_var(self) -> None:
        self.assertEqual(compose._interpolate_str("$HOME/x", {"HOME": "/root"}), "/root/x")


class TopoSortTests(unittest.TestCase):
    def test_dependency_order(self) -> None:
        doc = compose.parse_yaml(
            textwrap.dedent(
                """
                services:
                  web:
                    image: w
                    depends_on: [db, cache]
                  db:
                    image: d
                  cache:
                    image: c
                """
            )
        )
        proj = compose.Project("p", doc, "/tmp/p")
        order = [s.name for s in proj.topo_sorted()]
        self.assertLess(order.index("db"), order.index("web"))
        self.assertLess(order.index("cache"), order.index("web"))

    def test_cycle_detected(self) -> None:
        doc = compose.parse_yaml(
            textwrap.dedent(
                """
                services:
                  a:
                    image: a
                    depends_on: [b]
                  b:
                    image: b
                    depends_on: [a]
                """
            )
        )
        proj = compose.Project("p", doc, "/tmp/p")
        with self.assertRaises(compose.ShimError):
            proj.topo_sorted()


class TranslationTests(unittest.TestCase):
    def test_port_forms(self) -> None:
        self.assertEqual(compose._compose_port_to_publish("6379"), "0.0.0.0:6379:6379")
        self.assertEqual(compose._compose_port_to_publish("8080:80"), "0.0.0.0:8080:80")
        self.assertEqual(compose._compose_port_to_publish("443:443/tcp"), "0.0.0.0:443:443/tcp")
        self.assertEqual(
            compose._compose_port_to_publish("127.0.0.1:8080:80"), "127.0.0.1:8080:80"
        )

    def test_project_name_sanitize(self) -> None:
        self.assertEqual(compose._sanitize_project_name("My.App"), "my_app")
        self.assertEqual(compose._sanitize_project_name("---"), "default")

    def test_command_string_is_shell_split(self) -> None:
        # Compose splits string commands with shell lexical rules; it does NOT
        # wrap them in `sh -c` — that would smuggle `/bin/sh -c ...` into the
        # image ENTRYPOINT's argv (Dockerfile shell form is a different layer).
        self.assertEqual(
            compose._command_list("nginx -g 'daemon off;'"),
            ["nginx", "-g", "daemon off;"],
        )

    def test_command_list_passthrough(self) -> None:
        self.assertEqual(compose._command_list(["redis-server", "--port", "6379"]),
                         ["redis-server", "--port", "6379"])


# --------------------------------------------------------------------------- #
# End-to-end tests against the fake container binary
# --------------------------------------------------------------------------- #


class ComposeE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fake = self.root / "container"
        self.fake.write_text(FAKE_CONTAINER)
        self.fake.chmod(0o755)
        self.state = self.root / "fake-state.json"
        self.project = self.root / "proj"
        self.project.mkdir()
        self.env = os.environ.copy()
        self.env.update(
            {
                "CONTAINER_DOCKER_SHIM_CONTAINER": str(self.fake),
                "FAKE_CONTAINER_STATE": str(self.state),
            }
        )
        # Keep project name deterministic regardless of the temp dir name.
        self.env["COMPOSE_PROJECT_NAME"] = "demo"

    def write_compose(self, body: str) -> None:
        (self.project / "docker-compose.yml").write_text(textwrap.dedent(body))

    def docker(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(DOCKER), *args],
            text=True,
            capture_output=True,
            env=self.env,
            cwd=str(self.project),
            check=False,
        )

    def load_state(self) -> dict:
        return json.loads(self.state.read_text())

    def test_up_creates_labeled_containers_and_network(self) -> None:
        self.write_compose(
            """
            services:
              web:
                image: nginx:latest
                ports: ["8080:80"]
                depends_on: [db]
              db:
                image: postgres:16
            """
        )
        result = self.docker("compose", "up", "-d")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.load_state()
        names = set(state["containers"])
        self.assertEqual(names, {"demo-web-1", "demo-db-1"})
        for name in names:
            labels = state["containers"][name]["labels"]
            self.assertEqual(labels["com.docker.compose.project"], "demo")
        self.assertIn("demo_default", state["networks"])
        self.assertEqual(
            state["networks"]["demo_default"]["configuration"]["labels"][
                "com.docker.compose.project"
            ],
            "demo",
        )

    def test_up_injects_peer_hostnames(self) -> None:
        self.write_compose(
            """
            services:
              web:
                image: nginx:latest
              db:
                image: postgres:16
            """
        )
        self.assertEqual(self.docker("compose", "up", "-d").returncode, 0)
        state = self.load_state()
        # Every container should have received a peer /etc/hosts append.
        hosts_writes = [e for e in state["exec_log"] if "/etc/hosts" in " ".join(e["cmd"])]
        self.assertEqual(len(hosts_writes), 2)
        joined = " ".join(" ".join(e["cmd"]) for e in hosts_writes)
        self.assertIn("web", joined)
        self.assertIn("db", joined)

    def test_up_injects_host_docker_internal(self) -> None:
        # Multi-service /etc/hosts writes must also publish the host-gateway
        # aliases (Docker Desktop parity), pointing at the network gateway,
        # via rewrite (strip managed names, append fresh) so re-runs and
        # partial ups never duplicate or shadow entries.
        self.write_compose(
            """
            services:
              web:
                image: nginx:latest
              db:
                image: postgres:16
            """
        )
        self.assertEqual(self.docker("compose", "up", "-d").returncode, 0)
        state = self.load_state()
        hosts_writes = [e for e in state["exec_log"] if "/etc/hosts" in " ".join(e["cmd"])]
        for entry in hosts_writes:
            script = " ".join(entry["cmd"])
            self.assertIn("192.168.65.1 host.docker.internal gateway.docker.internal", script)
            self.assertIn("grep -vE", script)  # replace semantics: strip managed names first

    def test_single_service_still_gets_host_gateway(self) -> None:
        # A lone service has no peers to resolve, but must still get
        # host.docker.internal — the old peers-only path skipped it entirely.
        self.write_compose(
            """
            services:
              solo:
                image: nginx:latest
            """
        )
        self.assertEqual(self.docker("compose", "up", "-d").returncode, 0)
        state = self.load_state()
        hosts_writes = [e for e in state["exec_log"] if "/etc/hosts" in " ".join(e["cmd"])]
        self.assertEqual(len(hosts_writes), 1)
        script = " ".join(hosts_writes[0]["cmd"])
        self.assertIn("host.docker.internal", script)
        # No peer line for itself (single service → no <ip> solo entry).
        self.assertNotIn(" solo", script)

    def test_dependent_service_boots_with_dependency_hosts(self) -> None:
        # web depends_on db: db starts first (topological order), so its IP is
        # known when web launches. The shim must wrap web's entrypoint so
        # /etc/hosts is written BEFORE the app runs — a fail-fast app dials its
        # database in its first millisecond, long before post-start `exec`
        # injection can land (and Apple container has no restart policies to
        # give it a second chance).
        self.write_compose(
            """
            services:
              web:
                image: nginx:latest
                command: --flag on
                depends_on: [db]
              db:
                image: postgres:16
            """
        )
        result = self.docker("compose", "up", "-d")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.load_state()
        web = state["containers"]["demo-web-1"]
        db = state["containers"]["demo-db-1"]
        self.assertIsNone(db["entrypoint"])  # no dependencies -> not wrapped
        self.assertEqual(web["entrypoint"], "/bin/sh")
        self.assertEqual(web["cmd"][0], "-c")
        script = web["cmd"][1]
        self.assertIn("192.168.65.1 db", script)  # db started first -> first IP
        self.assertIn("host.docker.internal", script)
        self.assertIn('exec "$@"', script)
        # The real argv rides behind the script: image ENTRYPOINT (from
        # `image inspect`) + the compose command — string form shell-split,
        # never sh -c wrapped, replacing the image CMD.
        self.assertEqual(web["cmd"][2:], ["sh", "/entry", "--flag", "on"])

    def test_partial_up_sees_whole_project_and_refreshes_peers(self) -> None:
        # `up -d web` after a full up: the recreated web (new IP) must still
        # learn db's name (peers come from the whole project, not the started
        # subset), and db must be re-injected with web's NEW address — the
        # resolver takes the first match, so stale lines must be replaced,
        # not shadowed. This is the exact failure that broke a live tunnel
        # sidecar restart (fresh container knew no peers at all).
        self.write_compose(
            """
            services:
              web:
                image: nginx:latest
              db:
                image: postgres:16
            """
        )
        self.assertEqual(self.docker("compose", "up", "-d").returncode, 0)
        result = self.docker("compose", "up", "-d", "web")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.load_state()
        web_ip = state["containers"]["demo-web-1"]["status"]["networks"][0]["ipv4Address"].split("/")[0]
        partial_writes = [e for e in state["exec_log"] if "/etc/hosts" in " ".join(e["cmd"])][-2:]
        by_target = {e["container"]: " ".join(e["cmd"]) for e in partial_writes}
        self.assertIn("demo-web-1", by_target)
        self.assertIn("demo-db-1", by_target)
        self.assertIn(" db", by_target["demo-web-1"])       # fresh web knows db
        self.assertIn(f"{web_ip} web", by_target["demo-db-1"])  # db gets web's NEW ip

    def test_shell_less_image_gets_hosts_via_cp_fallback(self) -> None:
        # A distroless image (no /bin/sh — e.g. cloudflare/cloudflared) can't
        # take exec injection or the boot wrapper. The shim must fall back to
        # `container cp`: read /etc/hosts out, merge, write it back.
        self.write_compose(
            """
            services:
              edge:
                image: distroless/edge:latest
                x-shim-boot-hosts: false
                depends_on: [app]
              app:
                image: nginx:latest
            """
        )
        self.env["FAKE_NO_SHELL"] = "demo-edge-1"
        result = self.docker("compose", "up", "-d")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("could not write /etc/hosts", result.stderr)
        state = self.load_state()
        hosts = state["containers"]["demo-edge-1"]["files"]["/etc/hosts"]
        self.assertIn(" app", hosts)
        self.assertIn("host.docker.internal", hosts)
        # And the shelled peer still got plain exec injection.
        self.assertTrue(any(e["container"] == "demo-app-1" for e in state["exec_log"]))

    def test_boot_hosts_wrapper_can_be_opted_out(self) -> None:
        self.write_compose(
            """
            services:
              web:
                image: nginx:latest
                depends_on: [db]
                x-shim-boot-hosts: false
              db:
                image: postgres:16
            """
        )
        self.assertEqual(self.docker("compose", "up", "-d").returncode, 0)
        web = self.load_state()["containers"]["demo-web-1"]
        self.assertIsNone(web["entrypoint"])
        # Post-start injection still covers it.
        state = self.load_state()
        hosts_writes = [e for e in state["exec_log"] if "/etc/hosts" in " ".join(e["cmd"])]
        self.assertTrue(any(e["container"] == "demo-web-1" for e in hosts_writes))

    def test_ps_reconstructs_from_labels(self) -> None:
        self.write_compose(
            """
            services:
              web:
                image: nginx:latest
            """
        )
        self.docker("compose", "up", "-d")
        ps = self.docker("compose", "ps")
        self.assertEqual(ps.returncode, 0, ps.stderr)
        self.assertIn("demo-web-1", ps.stdout)
        self.assertIn("web", ps.stdout)

    def test_down_removes_only_owned_resources(self) -> None:
        self.write_compose(
            """
            services:
              web:
                image: nginx:latest
            volumes:
              data: {}
            """
        )
        # Pre-existing foreign container must survive a down.
        subprocess.run(
            [str(self.fake), "run", "-d", "--name", "outsider", "busybox"],
            env=self.env, check=True, capture_output=True,
        )
        self.docker("compose", "up", "-d")
        down = self.docker("compose", "down")
        self.assertEqual(down.returncode, 0, down.stderr)
        state = self.load_state()
        self.assertIn("outsider", state["containers"])
        self.assertNotIn("demo-web-1", state["containers"])
        self.assertNotIn("demo_default", state["networks"])

    def test_down_v_removes_named_volumes(self) -> None:
        self.write_compose(
            """
            services:
              db:
                image: postgres:16
                volumes:
                  - data:/var/lib/postgresql/data
            volumes:
              data: {}
            """
        )
        self.docker("compose", "up", "-d")
        state = self.load_state()
        self.assertIn("demo_data", state["volumes"])
        self.docker("compose", "down", "-v")
        state = self.load_state()
        self.assertNotIn("demo_data", state["volumes"])

    def test_up_is_idempotent(self) -> None:
        self.write_compose(
            """
            services:
              web:
                image: nginx:latest
            """
        )
        self.docker("compose", "up", "-d")
        first = set(self.load_state()["containers"])
        self.docker("compose", "up", "-d")
        second = set(self.load_state()["containers"])
        self.assertEqual(first, second)  # no duplicate accumulation

    def test_ls_lists_projects(self) -> None:
        self.write_compose(
            """
            services:
              web:
                image: nginx:latest
            """
        )
        self.docker("compose", "up", "-d")
        ls = self.docker("compose", "ls")
        self.assertEqual(ls.returncode, 0, ls.stderr)
        self.assertIn("demo", ls.stdout)

    def test_down_without_compose_file(self) -> None:
        # down must work from labels alone, even with no compose file present.
        self.write_compose(
            """
            services:
              web:
                image: nginx:latest
            """
        )
        self.docker("compose", "up", "-d")
        (self.project / "docker-compose.yml").unlink()
        down = self.docker("compose", "down")
        self.assertEqual(down.returncode, 0, down.stderr)
        self.assertNotIn("demo-web-1", self.load_state()["containers"])


if __name__ == "__main__":
    unittest.main()
