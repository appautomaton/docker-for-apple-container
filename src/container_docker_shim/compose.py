"""`docker compose` subset over Apple `container` — stateless orchestration.

State model
-----------
This module persists **nothing** on the macOS host. There is no sidecar
file, project registry, SQLite database, or cache owned by the shim. All
durable state lives in Apple `container` (the containers, the project
network, and named volumes), annotated with Docker-compatible labels:

    com.docker.compose.project           the project a resource belongs to
    com.docker.compose.service           the service a container implements
    com.docker.compose.container-number  always 1 (no replicas)
    com.docker.compose.oneoff            always "False"

Every verb reconstructs the project by querying Apple and filtering on
these labels — exactly the schema Docker Compose itself uses. `up` reads
the compose file (it needs the service definitions and dependency order);
`down`, `ps`, `logs`, and `ls` need only the labels.

Service discovery
-----------------
Apple `container` does not resolve service names by DNS without an admin
`container system dns` domain, and has no `--add-host`. The shim closes the
gap in two layers, both writing only to **each container's own ephemeral
/etc/hosts** (gone when the container is removed — the macOS host's
/etc/hosts and resolver are never touched):

1. **Boot-time, for dependencies.** Services start in `depends_on` order, so
   by the time a dependent service launches its dependencies' addresses are
   already known. The dependent's entrypoint is wrapped in a `/bin/sh`
   prelude that writes ``<ip> <dependency>`` lines before exec'ing the real
   process — so an app that dials its database in its first millisecond
   still resolves the name. (Post-start injection alone loses that race:
   the app crashes before `container exec` can land, and Apple `container`
   has no restart policies to save it.) Opt out per service with
   ``x-shim-boot-hosts: false``.

2. **Post-start, for all peers.** After everything is up, ``<ip> <service>``
   lines for the whole project are appended idempotently into every
   container via `container exec` — covering peers that are not declared
   dependencies.

Both layers also publish ``host.docker.internal`` and
``gateway.docker.internal`` → the container's gateway (which, on Apple
`container`, is the macOS host). Docker Desktop does this automatically on
macOS/Windows; reproducing it here lets images that dial the host by that
name work unchanged.
"""

from __future__ import annotations

import os
import shlex
import sys
import threading
import time
from typing import Any

from .cli import (
    DOCKER_ZERO_TIME,
    ShimError,
    _container_bin,
    _die,
    _load_container_rows,
    _normalize_cpus,
    _normalize_memory,
    _run_container_capture,
    _run_container_passthrough,
    _translate_volume,
)


# --------------------------------------------------------------------------- #
# Label schema (Docker Compose's own keys — Apple stores them verbatim).
# --------------------------------------------------------------------------- #

LABEL_PROJECT = "com.docker.compose.project"
LABEL_SERVICE = "com.docker.compose.service"
LABEL_NUMBER = "com.docker.compose.container-number"
LABEL_ONEOFF = "com.docker.compose.oneoff"

SUPPORTED_COMPOSE_FILENAMES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
)

# Compose keys that have no verified Apple `container` equivalent. They are
# parsed (so the file is valid) but not applied; `up` warns once per service
# so the user is never misled into thinking they took effect.
UNSUPPORTED_SERVICE_KEYS = {
    "restart": "restart policies are not supported by Apple container",
    "healthcheck": "healthchecks are not enforced (depends_on is treated as ordering only)",
    "privileged": "privileged mode is not supported by Apple container",
    "hostname": "custom hostname is not supported by Apple container",
    "secrets": "secrets are a Swarm feature with no Apple container equivalent",
    "configs": "configs are a Swarm feature with no Apple container equivalent",
    "extra_hosts": "extra_hosts is not yet translated",
    "devices": "device mapping is not supported by Apple container",
    "sysctls": "sysctls are not supported by Apple container",
}

# Per-service escape hatch: `x-shim-boot-hosts: false` skips the boot-time
# /etc/hosts entrypoint wrapper for that service (post-start injection still
# applies). For `depends_on` services whose image has no /bin/sh.
BOOT_HOSTS_KEY = "x-shim-boot-hosts"


# ========================================================================== #
# Minimal YAML parser (compose subset)
#
# A hand-rolled, dependency-free parser covering the YAML that compose files
# actually use: indentation-based block maps and sequences, inline flow
# collections (``[a, b]`` / ``{k: v}``), quoted and bare scalars, and ``#``
# comments. It is deliberately not a full YAML implementation — anchors,
# multi-document streams, and ``|``/``>`` block scalars are out of scope.
# ========================================================================== #


def _strip_comment(line: str) -> str:
    """Drop a trailing ``#`` comment, respecting quotes."""
    quote: str | None = None
    for idx, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#" and (idx == 0 or line[idx - 1] in (" ", "\t")):
            return line[:idx]
    return line


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Return ``(indent, content)`` for every non-blank, non-comment line."""
    out: list[tuple[int, str]] = []
    for raw in text.splitlines():
        stripped = _strip_comment(raw.replace("\t", "    ")).rstrip()
        if not stripped.strip():
            continue
        if stripped.strip() == "---":  # tolerate a single document marker
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        out.append((indent, stripped.strip()))
    return out


def _split_key(content: str) -> tuple[str, str] | None:
    """Split ``key: value`` at the first unquoted colon. None if not a map line."""
    quote: str | None = None
    for idx, ch in enumerate(content):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == ":" and (idx + 1 == len(content) or content[idx + 1] == " "):
            return content[:idx].strip(), content[idx + 1 :].strip()
    return None


def _scalar(token: str) -> Any:
    """Type a bare scalar token the way compose expects."""
    token = token.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1]
    low = token.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~", ""):
        return None
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        pass
    return token


def _split_flow(body: str) -> list[str]:
    """Split a flow collection body on top-level commas, respecting nesting."""
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    buf: list[str] = []
    for ch in body:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch in ("[", "{"):
            depth += 1
            buf.append(ch)
        elif ch in ("]", "}"):
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _parse_value(token: str) -> Any:
    """Parse an inline value: flow sequence, flow mapping, or scalar."""
    token = token.strip()
    if token.startswith("[") and token.endswith("]"):
        return [_parse_value(part) for part in _split_flow(token[1:-1])]
    if token.startswith("{") and token.endswith("}"):
        result: dict[str, Any] = {}
        for part in _split_flow(token[1:-1]):
            kv = _split_key(part)
            if kv is None:
                continue
            result[str(_scalar(kv[0]))] = _parse_value(kv[1]) if kv[1] else None
        return result
    return _scalar(token)


def _parse_block(lines: list[tuple[int, str]], i: int, indent: int) -> tuple[Any, int]:
    first = lines[i][1]
    if first == "-" or first.startswith("- ") or first.startswith("-\t"):
        return _parse_seq(lines, i, indent)
    return _parse_map(lines, i, indent)


def _parse_map(lines: list[tuple[int, str]], i: int, indent: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while i < len(lines):
        ind, content = lines[i]
        if ind < indent:
            break
        if ind > indent:
            raise ShimError(f"compose: unexpected indentation near {content!r}", 64)
        kv = _split_key(content)
        if kv is None:
            raise ShimError(f"compose: expected 'key: value' near {content!r}", 64)
        key, rest = kv
        key = str(_scalar(key))
        i += 1
        if rest:
            result[key] = _parse_value(rest)
            continue
        # Nested block: a deeper map/seq, or a sequence at the same indent.
        if i < len(lines):
            nind, ncontent = lines[i]
            is_seq = ncontent == "-" or ncontent.startswith("- ")
            if nind > indent:
                result[key], i = _parse_block(lines, i, nind)
                continue
            if nind == indent and is_seq:
                result[key], i = _parse_seq(lines, i, indent)
                continue
        result[key] = None
    return result, i


def _parse_seq(lines: list[tuple[int, str]], i: int, indent: int) -> tuple[list[Any], int]:
    result: list[Any] = []
    while i < len(lines):
        ind, content = lines[i]
        if ind != indent or not (content == "-" or content.startswith("- ")):
            break
        item = content[1:].strip()
        i += 1
        if not item:
            if i < len(lines) and lines[i][0] > indent:
                value, i = _parse_block(lines, i, lines[i][0])
            else:
                value = None
            result.append(value)
            continue
        if _split_key(item) is not None and not item.startswith(("[", "{")):
            # A mapping that begins inline after the dash: gather its inline
            # entry plus any deeper continuation lines into one synthetic map.
            sub: list[tuple[int, str]] = [(indent + 2, item)]
            while i < len(lines) and lines[i][0] > indent:
                sub.append(lines[i])
                i += 1
            value, _ = _parse_map(sub, 0, indent + 2)
            result.append(value)
            continue
        result.append(_parse_value(item))
    return result, i


def parse_yaml(text: str) -> Any:
    """Parse a compose-subset YAML document into Python data."""
    lines = _logical_lines(text)
    if not lines:
        return {}
    value, end = _parse_block(lines, 0, lines[0][0])
    if end != len(lines):
        raise ShimError(f"compose: could not parse near {lines[end][1]!r}", 64)
    return value


# ========================================================================== #
# Environment interpolation (${VAR}, ${VAR:-default}, ${VAR:?msg}, $$)
# ========================================================================== #


def _load_env_file(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export ") :].strip()
                key, sep, value = line.partition("=")
                if not sep:
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                env[key.strip()] = value
    except FileNotFoundError:
        pass
    return env


def _interpolate_str(value: str, env: dict[str, str]) -> str:
    out: list[str] = []
    i = 0
    n = len(value)
    while i < n:
        ch = value[i]
        if ch != "$":
            out.append(ch)
            i += 1
            continue
        if i + 1 < n and value[i + 1] == "$":  # $$ -> literal $
            out.append("$")
            i += 2
            continue
        if i + 1 < n and value[i + 1] == "{":
            close = value.find("}", i + 2)
            if close == -1:
                out.append(ch)
                i += 1
                continue
            expr = value[i + 2 : close]
            out.append(_resolve_expr(expr, env))
            i = close + 1
            continue
        # Bare $VAR form.
        j = i + 1
        while j < n and (value[j].isalnum() or value[j] == "_"):
            j += 1
        name = value[i + 1 : j]
        if name:
            out.append(env.get(name, ""))
            i = j
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _resolve_expr(expr: str, env: dict[str, str]) -> str:
    for sep, required in ((":?", True), (":-", False), ("?", True), ("-", False)):
        if sep in expr:
            name, _, tail = expr.partition(sep)
            present = env.get(name)
            if present not in (None, ""):
                return present
            if required:
                raise ShimError(f"compose: required variable {name!r} is unset: {tail}", 1)
            return tail
    return env.get(expr, "")


def _interpolate(obj: Any, env: dict[str, str]) -> Any:
    if isinstance(obj, str):
        return _interpolate_str(obj, env)
    if isinstance(obj, list):
        return [_interpolate(item, env) for item in obj]
    if isinstance(obj, dict):
        return {key: _interpolate(val, env) for key, val in obj.items()}
    return obj


# ========================================================================== #
# Project model
# ========================================================================== #


def _sanitize_project_name(raw: str) -> str:
    """Reduce a name to Docker's project charset: [a-z0-9_-]."""
    lowered = raw.lower()
    cleaned = "".join(ch if (ch.isalnum() or ch in "_-") else "_" for ch in lowered)
    cleaned = cleaned.lstrip("_-")
    return cleaned or "default"


def _as_str(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return ""
    return str(value)


class Service:
    """A normalized compose service."""

    def __init__(self, name: str, spec: dict[str, Any]):
        self.name = name
        self.spec = spec
        self.depends_on = _parse_depends_on(spec.get("depends_on"))

    @property
    def container_name(self) -> str | None:
        value = self.spec.get("container_name")
        return _as_str(value) if value else None


def _parse_depends_on(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, dict):  # map form: {db: {condition: ...}}
        return [str(key) for key in value]
    return []


class Project:
    """A parsed, interpolated compose project."""

    def __init__(self, name: str, doc: dict[str, Any], directory: str):
        self.name = name
        self.directory = directory
        self.doc = doc
        raw_services = doc.get("services") or {}
        if not isinstance(raw_services, dict):
            raise ShimError("compose: 'services' must be a mapping", 64)
        self.services: dict[str, Service] = {}
        for svc_name, spec in raw_services.items():
            if spec is None:
                spec = {}
            if not isinstance(spec, dict):
                raise ShimError(f"compose: service {svc_name!r} must be a mapping", 64)
            self.services[str(svc_name)] = Service(str(svc_name), spec)
        self.networks = doc.get("networks") or {}
        self.volumes = doc.get("volumes") or {}

    def topo_sorted(self) -> list[Service]:
        """Services in dependency order (dependencies first), cycle-detected."""
        ordered: list[Service] = []
        done: set[str] = set()
        visiting: set[str] = set()

        def visit(name: str) -> None:
            if name in done:
                return
            if name in visiting:
                raise ShimError(f"compose: cyclic depends_on involving {name!r}", 64)
            service = self.services.get(name)
            if service is None:
                return  # depends_on a service not defined here — ignore, like Docker
            visiting.add(name)
            for dep in service.depends_on:
                visit(dep)
            visiting.discard(name)
            done.add(name)
            ordered.append(service)

        for svc_name in self.services:
            visit(svc_name)
        return ordered


# ========================================================================== #
# Loading & project-name resolution
# ========================================================================== #


def _find_compose_file(explicit: str | None, directory: str) -> str:
    if explicit:
        path = explicit if os.path.isabs(explicit) else os.path.join(directory, explicit)
        if not os.path.isfile(path):
            raise ShimError(f"compose: file not found: {explicit}", 1)
        return path
    for name in SUPPORTED_COMPOSE_FILENAMES:
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate):
            return candidate
    raise ShimError(
        "compose: no compose file found "
        "(looked for compose.yaml, compose.yml, docker-compose.yaml, docker-compose.yml)",
        1,
    )


def _load_project(
    *,
    file: str | None,
    project_name: str | None,
    project_dir: str | None,
    env_file: str | None,
) -> Project:
    directory = os.path.abspath(project_dir or os.getcwd())
    compose_path = _find_compose_file(file, directory)
    project_dir_resolved = os.path.abspath(project_dir or os.path.dirname(compose_path))

    with open(compose_path, encoding="utf-8") as handle:
        raw_doc = parse_yaml(handle.read())
    if not isinstance(raw_doc, dict):
        raise ShimError("compose: top-level document must be a mapping", 64)

    env = _load_env_file(env_file or os.path.join(project_dir_resolved, ".env"))
    env.update(os.environ)  # shell environment overrides .env, like Docker
    doc = _interpolate(raw_doc, env)

    name = _resolve_project_name(project_name, doc, project_dir_resolved)
    return Project(name, doc, project_dir_resolved)


def _resolve_project_name(explicit: str | None, doc: dict[str, Any], directory: str) -> str:
    if explicit:
        return _sanitize_project_name(explicit)
    env_name = os.environ.get("COMPOSE_PROJECT_NAME")
    if env_name:
        return _sanitize_project_name(env_name)
    if doc.get("name"):
        return _sanitize_project_name(_as_str(doc["name"]))
    return _sanitize_project_name(os.path.basename(directory.rstrip("/")) or "default")


# ========================================================================== #
# Service -> `container run` translation
# ========================================================================== #


def _network_resource_name(project: str, network: str) -> str:
    return f"{project}_{network}"


def _volume_resource_name(project: str, volume: str) -> str:
    return f"{project}_{volume}"


def _compose_port_to_publish(port: Any) -> str:
    """Translate a compose port entry into Apple `-p [ip:]host:container[/proto]`.

    A bare container port (``"6379"``) is published to the identical host
    port, deterministically — Apple has no random-host-port mode.
    """
    spec = _as_str(port)
    proto = ""
    if "/" in spec:
        spec, _, proto = spec.partition("/")
        proto = f"/{proto}"
    parts = spec.split(":")
    if len(parts) == 1:
        return f"0.0.0.0:{parts[0]}:{parts[0]}{proto}"
    if len(parts) == 2:
        host, container = parts
        # IP-less host:container — bind on all interfaces, like Docker.
        return f"0.0.0.0:{host}:{container}{proto}"
    return f"{spec}{proto}"


def _translate_environment(spec_env: Any) -> list[str]:
    args: list[str] = []
    if isinstance(spec_env, dict):
        for key, value in spec_env.items():
            args.extend(["-e", f"{key}={_as_str(value)}"])
    elif isinstance(spec_env, list):
        for entry in spec_env:
            args.extend(["-e", _as_str(entry)])
    return args


def _classify_and_translate_volume(
    entry: Any, project: Project, ensured_volumes: set[str]
) -> list[str]:
    """Translate one compose volume entry into `container run` args.

    Host-path sources (``./data``, ``/abs``) become bind mounts. Bare names
    are project-scoped Apple **named volumes** (created on demand), mounted
    with the native ``-v name:/target`` form.
    """
    if isinstance(entry, dict):
        source = _as_str(entry.get("source"))
        target = _as_str(entry.get("target"))
        if not target:
            raise ShimError(f"compose: volume entry missing target: {entry!r}", 64)
        if entry.get("type") == "bind" or _is_host_path(source):
            host = _resolve_host_path(source, project.directory)
            mode = "ro" if entry.get("read_only") is True else ""
            return _translate_volume(_join_volume(host, target, mode))
        return _named_volume_args(source, target, "", project, ensured_volumes)

    text = _as_str(entry)
    parts = text.split(":")
    if len(parts) < 2:
        raise ShimError(f"compose: invalid volume entry: {text!r}", 64)
    source, target = parts[0], parts[1]
    mode = parts[2] if len(parts) > 2 else ""
    if _is_host_path(source):
        host = _resolve_host_path(source, project.directory)
        return _translate_volume(_join_volume(host, target, mode))
    return _named_volume_args(source, target, mode, project, ensured_volumes)


def _is_host_path(source: str) -> bool:
    """A compose volume source is a host path if it looks like one, not a name."""
    return source.startswith((".", "/", "~")) or "/" in source


def _resolve_host_path(source: str, project_dir: str) -> str:
    expanded = os.path.expanduser(source)
    if os.path.isabs(expanded):
        return expanded
    return os.path.normpath(os.path.join(project_dir, expanded))


def _join_volume(source: str, target: str, mode: str) -> str:
    return f"{source}:{target}:{mode}" if mode else f"{source}:{target}"


def _named_volume_args(
    source: str, target: str, mode: str, project: Project, ensured_volumes: set[str]
) -> list[str]:
    resource = _volume_resource_name(project.name, source)
    if resource not in ensured_volumes:
        _ensure_named_volume(resource, project.name)
        ensured_volumes.add(resource)
    spec = f"{resource}:{target}"
    if mode:
        spec += f":{mode}"
    return ["-v", spec]


def _service_networks(service: Service, project: Project) -> list[str]:
    declared = service.spec.get("networks")
    names: list[str]
    if isinstance(declared, dict):
        names = [str(key) for key in declared]
    elif isinstance(declared, list):
        names = [str(item) for item in declared]
    else:
        names = ["default"]
    return [_network_resource_name(project.name, name) for name in names]


def _build_run_args(
    service: Service,
    project: Project,
    *,
    image: str,
    ensured_volumes: set[str],
    warned: set[str],
    boot_hosts: list[str] | None = None,
) -> tuple[list[str], bool]:
    """Translate a service into `container run` args.

    Returns ``(args, wrapped)`` — ``wrapped`` is True when ``boot_hosts``
    lines were baked into a /bin/sh entrypoint prelude (see
    ``_boot_hosts_script``), so the caller can fall back to an unwrapped
    launch if the wrapped one fails to start.
    """
    spec = service.spec
    args: list[str] = ["-d"]

    container_name = service.container_name or f"{project.name}-{service.name}-1"
    args.extend(["--name", container_name])

    # Compose label schema — the entire stateless bookkeeping lives here.
    args.extend(["--label", f"{LABEL_PROJECT}={project.name}"])
    args.extend(["--label", f"{LABEL_SERVICE}={service.name}"])
    args.extend(["--label", f"{LABEL_NUMBER}=1"])
    args.extend(["--label", f"{LABEL_ONEOFF}=False"])
    user_labels = spec.get("labels")
    if isinstance(user_labels, dict):
        for key, value in user_labels.items():
            args.extend(["--label", f"{key}={_as_str(value)}"])
    elif isinstance(user_labels, list):
        for entry in user_labels:
            args.extend(["--label", _as_str(entry)])

    for net in _service_networks(service, project):
        args.extend(["--network", net])

    if spec.get("platform"):
        args.extend(["--platform", _as_str(spec["platform"])])
    if spec.get("user"):
        args.extend(["--user", _as_str(spec["user"])])
    if spec.get("working_dir"):
        args.extend(["-w", _as_str(spec["working_dir"])])
    if spec.get("read_only") is True:
        args.append("--read-only")
    if spec.get("init") is True:
        args.append("--init")
    if spec.get("tty") is True:
        args.append("-t")
    if spec.get("stdin_open") is True:
        args.append("-i")
    if spec.get("shm_size"):
        args.extend(["--shm-size", _as_str(spec["shm_size"])])

    for cap in _as_list(spec.get("cap_add")):
        args.extend(["--cap-add", _as_str(cap)])
    for cap in _as_list(spec.get("cap_drop")):
        args.extend(["--cap-drop", _as_str(cap)])
    for limit in _as_list(spec.get("ulimits")):
        args.extend(["--ulimit", _as_str(limit)])
    for entry in _as_list(spec.get("tmpfs")):
        args.extend(["--tmpfs", _as_str(entry)])

    cpus, memory = _resource_limits(spec)
    if cpus:
        args.extend(["--cpus", _normalize_cpus(cpus)])
    if memory:
        args.extend(["--memory", _normalize_memory(memory)])

    args.extend(_translate_environment(spec.get("environment")))
    for env_file in _as_list(spec.get("env_file")):
        path = _as_str(env_file)
        resolved = path if os.path.isabs(path) else os.path.join(project.directory, path)
        for key, value in _load_env_file(resolved).items():
            args.extend(["-e", f"{key}={value}"])

    for port in _as_list(spec.get("ports")):
        args.extend(["-p", _compose_port_to_publish(port)])

    for volume in _as_list(spec.get("volumes")):
        args.extend(_classify_and_translate_volume(volume, project, ensured_volumes))

    _warn_unsupported(service, warned)

    # Compose entrypoint follows the same string-splitting rules as command.
    entrypoint = _command_list(spec["entrypoint"]) if spec.get("entrypoint") is not None else None
    command = _command_list(spec.get("command"))
    positional: list[str] = []
    wrapped = False
    if boot_hosts:
        argv = _boot_argv(service, image, entrypoint, command)
        if argv:
            args.extend(["--entrypoint", "/bin/sh"])
            positional = ["-c", _boot_hosts_script(boot_hosts), "sh", *argv]
            wrapped = True
        else:
            _warn(
                f"service {service.name!r}: could not resolve the image entrypoint — "
                "dependency names will be injected post-start only"
            )
    if not wrapped:
        if entrypoint:
            args.extend(["--entrypoint", entrypoint[0]])
            positional.extend(entrypoint[1:])
        if command:
            positional.extend(command)

    args.append(image)
    args.extend(positional)
    return args, wrapped


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _command_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [_as_str(item) for item in value]
    # Compose splits string commands with shell *lexical* rules — it does NOT
    # wrap them in `sh -c` (that's Dockerfile shell form, a different layer).
    # `command: --log-dir /data` must reach the image ENTRYPOINT as two args.
    return shlex.split(_as_str(value))


def _resource_limits(spec: dict[str, Any]) -> tuple[str, str]:
    cpus = ""
    memory = ""
    deploy = spec.get("deploy")
    if isinstance(deploy, dict):
        limits = (deploy.get("resources") or {}).get("limits") if isinstance(deploy.get("resources"), dict) else None
        if isinstance(limits, dict):
            if limits.get("cpus"):
                cpus = _as_str(limits["cpus"])
            if limits.get("memory"):
                memory = _as_str(limits["memory"])
    if spec.get("cpus"):
        cpus = _as_str(spec["cpus"])
    if spec.get("mem_limit"):
        memory = _as_str(spec["mem_limit"])
    return cpus, memory


def _warn_unsupported(service: Service, warned: set[str]) -> None:
    for key, reason in UNSUPPORTED_SERVICE_KEYS.items():
        if key in service.spec and key not in warned:
            _warn(f"service {service.name!r}: '{key}' ignored — {reason}")
            warned.add(key)


# ========================================================================== #
# Apple-store queries (the only place project state is read from)
# ========================================================================== #


def _project_containers(project: str) -> list[dict[str, Any]]:
    code, rows, err = _load_container_rows(all_containers=True)
    if code != 0:
        raise ShimError(err.strip() or "compose: could not list containers", code)
    members = [row for row in rows if (row.get("labels") or {}).get(LABEL_PROJECT) == project]
    members.sort(key=lambda r: (r.get("labels") or {}).get(LABEL_SERVICE, ""))
    return members


def _all_projects() -> dict[str, list[dict[str, Any]]]:
    code, rows, err = _load_container_rows(all_containers=True)
    if code != 0:
        raise ShimError(err.strip() or "compose: could not list containers", code)
    projects: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        name = (row.get("labels") or {}).get(LABEL_PROJECT)
        if name:
            projects.setdefault(name, []).append(row)
    return projects


def _load_labeled_resources(family: str, project: str) -> list[str]:
    """Return names of `network`/`volume` resources owned by ``project``."""
    result = _run_container_capture([family, "list", "--format", "json"])
    if result.returncode != 0:
        return []
    import json

    try:
        data = json.loads(result.stdout or "[]")
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    owned: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        config = item.get("configuration") if isinstance(item.get("configuration"), dict) else item
        labels = config.get("labels") if isinstance(config, dict) else None
        name = (config.get("name") if isinstance(config, dict) else None) or item.get("id")
        if isinstance(labels, dict) and labels.get(LABEL_PROJECT) == project and name:
            owned.append(str(name))
    return owned


def _container_net(name: str) -> tuple[str | None, str | None]:
    """Return ``(ipv4, gateway)`` for a container's first network, or ``(None, None)``.

    Both come from the same ``status.networks[]`` block of ``container inspect``
    so a single call yields the container's own address *and* the gateway —
    which, on Apple `container`, is the macOS host (Docker Desktop's
    ``host.docker.internal``). Addresses are stripped of their CIDR suffix.
    """
    result = _run_container_capture(["inspect", name])
    if result.returncode != 0:
        return None, None
    import json

    try:
        data = json.loads(result.stdout or "[]")
    except ValueError:
        return None, None
    item = data[0] if isinstance(data, list) and data else data
    if not isinstance(item, dict):
        return None, None
    networks = (item.get("status") or {}).get("networks") or []
    for net in networks:
        if not isinstance(net, dict):
            continue
        addr = net.get("ipv4Address")
        gateway = net.get("ipv4Gateway")
        if addr:
            ip = str(addr).split("/")[0]
            gw = str(gateway).split("/")[0] if gateway else None
            return ip, gw
    return None, None


# ========================================================================== #
# Resource provisioning
# ========================================================================== #


def _ensure_network(project: str, network: str) -> None:
    resource = _network_resource_name(project, network)
    existing = _run_container_capture(["network", "inspect", resource])
    if existing.returncode == 0:
        return
    spec = (_project_network_spec(project, network))
    if spec.get("external"):
        _warn(f"network {network!r} declared external — not created")
        return
    args = ["network", "create", "--label", f"{LABEL_PROJECT}={project}"]
    if spec.get("internal") is True:
        args.append("--internal")
    args.append(resource)
    result = _run_container_capture(args)
    if result.returncode != 0:
        raise ShimError(
            f"compose: failed to create network {resource}: "
            f"{(result.stderr or result.stdout).strip()}",
            result.returncode,
        )


_NETWORK_SPECS: dict[tuple[str, str], dict[str, Any]] = {}


def _project_network_spec(project: str, network: str) -> dict[str, Any]:
    return _NETWORK_SPECS.get((project, network), {})


def _ensure_named_volume(resource: str, project: str) -> None:
    existing = _run_container_capture(["volume", "inspect", resource])
    if existing.returncode == 0:
        return
    result = _run_container_capture(
        ["volume", "create", "--label", f"{LABEL_PROJECT}={project}", resource]
    )
    if result.returncode != 0:
        raise ShimError(
            f"compose: failed to create volume {resource}: "
            f"{(result.stderr or result.stdout).strip()}",
            result.returncode,
        )


# ========================================================================== #
# Service discovery via each container's own /etc/hosts
# ========================================================================== #


# Docker Desktop auto-publishes these names (both → the host) into every
# container's /etc/hosts on macOS/Windows. Apple `container` does not, and has
# no `--add-host` flag, so we reproduce it via the same per-container injection.
_HOST_ALIASES = "host.docker.internal gateway.docker.internal"


def _boot_hosts_lines(
    service: Service,
    project: Project,
    ip_by_service: dict[str, str],
    gateway_ip: str | None,
) -> list[str]:
    """/etc/hosts lines a dependent service needs before its entrypoint runs.

    Dependencies started earlier in this `up` are in ``ip_by_service``; a
    dependency excluded by a partial `up <service>` may still be running from
    a previous `up`, so fall back to inspecting it live. Services without
    ``depends_on`` return [] — for them post-start injection is the whole
    story, as before.
    """
    if not service.depends_on or service.spec.get(BOOT_HOSTS_KEY) is False:
        return []
    lines: list[str] = []
    gateway = gateway_ip
    for dep in dict.fromkeys(service.depends_on):
        ip = ip_by_service.get(dep)
        if ip is None:
            dep_service = project.services.get(dep)
            if dep_service is None:
                continue  # depends_on a service not defined here — ignore, like Docker
            dep_name = dep_service.container_name or f"{project.name}-{dep}-1"
            ip, dep_gateway = _container_net(dep_name)
            gateway = gateway or dep_gateway
        if ip:
            lines.append(f"{ip} {dep}")
        else:
            _warn(
                f"service {service.name!r}: dependency {dep!r} has no address yet — "
                f"{dep!r} will only resolve after post-start injection"
            )
    if lines and gateway:
        lines.append(f"{gateway} {_HOST_ALIASES}")
    return lines


def _boot_hosts_script(lines: list[str]) -> str:
    """A /bin/sh prelude: write ``lines`` into /etc/hosts, then exec the real argv.

    The real argv rides in as ``"$@"`` so it never needs quoting into the
    script. Append failures (read-only fs, non-root USER, no grep) must not
    stop the container from booting — hence the blanket ``|| true``.
    """
    entries = " ".join(shlex.quote(line) for line in lines)
    return (
        "{ for l in " + entries + "; do "
        'grep -qxF "$l" /etc/hosts || printf \'%s\\n\' "$l" >> /etc/hosts; '
        "done; } 2>/dev/null || true; "
        'exec "$@"'
    )


def _boot_argv(
    service: Service, image: str, entrypoint: list[str] | None, command: list[str]
) -> list[str] | None:
    """The exact argv the container would run unwrapped, or None if unknowable.

    Mirrors Docker precedence: a compose ``entrypoint:`` override also
    discards the image CMD; otherwise image ENTRYPOINT + (compose command,
    or the image CMD when no command is given).
    """
    if entrypoint is not None:
        return (entrypoint + command) or None
    platform_hint = _as_str(service.spec["platform"]) if service.spec.get("platform") else None
    defaults = _image_default_argv(image, platform_hint)
    if defaults is None:
        return None
    image_entrypoint, image_cmd = defaults
    return (image_entrypoint + (command or image_cmd)) or None


def _image_default_argv(image: str, platform_hint: str | None) -> tuple[list[str], list[str]] | None:
    """(ENTRYPOINT, CMD) from the image config, or None if not inspectable.

    `container image inspect` returns one entry per platform variant; pick
    the one matching the requested platform (or the host architecture).
    """
    result = _run_container_capture(["image", "inspect", image])
    if result.returncode != 0:
        return None
    import json

    try:
        data = json.loads(result.stdout or "[]")
    except ValueError:
        return None
    item = data[0] if isinstance(data, list) and data else data
    if not isinstance(item, dict):
        return None
    variants = [v for v in item.get("variants") or [] if isinstance(v, dict)]
    if not variants:
        return None
    if platform_hint and "/" in platform_hint:
        want_arch: str | None = platform_hint.split("/")[1]
    else:
        import platform as host_platform

        machine = host_platform.machine().lower()
        want_arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(machine, machine)
    chosen = variants[0]
    for variant in variants:
        if (variant.get("platform") or {}).get("architecture") == want_arch:
            chosen = variant
            break
    config = (chosen.get("config") or {}).get("config") or {}
    image_entrypoint = config.get("Entrypoint") or []
    image_cmd = config.get("Cmd") or []
    if not isinstance(image_entrypoint, list) or not isinstance(image_cmd, list):
        return None
    return [_as_str(x) for x in image_entrypoint], [_as_str(x) for x in image_cmd]


def _inject_etc_hosts(
    project: Project,
    services: list[Service],
    net_info: dict[str, tuple[str | None, str | None]] | None = None,
) -> None:
    """Populate each container's own /etc/hosts with peer + host-gateway names.

    Two things are injected per container, via `container exec`:

    1. **Peer service names** (``<ip> <service>``) so services resolve each
       other by name — only meaningful when the project has ≥2 services.
    2. **``host.docker.internal`` / ``gateway.docker.internal``** → the
       container's gateway, which on Apple `container` *is* the macOS host.
       This mirrors Docker Desktop, so images that dial the host by that name
       (a very common Docker-on-Mac assumption) work unchanged. Injected even
       for single-service projects.

    All writes land in **each container's ephemeral /etc/hosts**, discarded on
    removal — the macOS host's /etc/hosts is never read or written. Each line is
    appended only if not already present (idempotent across re-runs, and with
    the boot-time wrapper's lines). If the `exec` fails (no shell in the image,
    or the container already exited), that one container is skipped with a
    warning rather than failing the whole `up`.

    ``net_info`` carries addresses already inspected by the caller (keyed by
    container name); anything missing is inspected here.
    """
    info: dict[str, tuple[str | None, str | None]] = dict(net_info or {})
    peers: list[tuple[str, str]] = []
    for service in services:
        name = service.container_name or f"{project.name}-{service.name}-1"
        if name not in info:
            info[name] = _container_net(name)
        ip = info[name][0]
        if ip:
            peers.append((service.name, ip))

    peer_lines = [f"{ip} {svc}" for svc, ip in peers] if len(peers) >= 2 else []

    for service in services:
        name = service.container_name or f"{project.name}-{service.name}-1"
        _ip, gateway = info[name]
        lines = list(peer_lines)
        if gateway:
            lines.append(f"{gateway} {_HOST_ALIASES}")
        if not lines:
            continue
        # Append each line only if an identical one isn't already there, so a
        # re-`up` (or any re-injection) never duplicates entries.
        script = "\n".join(
            f"grep -qxF {q} /etc/hosts || printf '%s\\n' {q} >> /etc/hosts"
            for q in (shlex.quote(line) for line in lines)
        )
        result = _run_container_capture(["exec", name, "sh", "-c", script])
        if result.returncode != 0:
            _warn(
                f"could not write /etc/hosts in {name} (no shell in image, or "
                "container exited before injection) — service/host-gateway "
                "names may be unavailable there"
            )


# ========================================================================== #
# Verbs
# ========================================================================== #


def _remove_containers(rows: list[dict[str, Any]]) -> int:
    rc = 0
    for row in rows:
        ident = row.get("id") or row.get("name")
        if not ident:
            continue
        stop = _run_container_capture(["stop", ident])
        if stop.returncode != 0 and row.get("state") == "running":
            sys.stderr.write(stop.stderr or stop.stdout)
            rc = stop.returncode
        remove = _run_container_capture(["rm", ident])
        if remove.returncode != 0:
            sys.stderr.write(remove.stderr or remove.stdout)
            rc = remove.returncode
        else:
            print(f"Removed {row.get('name') or ident}")
    return rc


def cmd_up(project: Project, *, detach: bool, build: bool, only: list[str], no_build: bool) -> int:
    services = project.topo_sorted()
    if only:
        wanted = set(only)
        services = [svc for svc in services if svc.name in wanted]
        if not services:
            return _die(f"compose: no such service(s): {', '.join(only)}", 1)

    # Provision declared networks (record specs for ensure-on-attach), defaulting
    # to the implicit project network every service joins.
    for net_name, net_spec in _network_dict(project).items():
        _NETWORK_SPECS[(project.name, net_name)] = net_spec if isinstance(net_spec, dict) else {}
    _ensure_network(project.name, "default")
    referenced: set[str] = {"default"}
    for service in services:
        declared = service.spec.get("networks")
        if isinstance(declared, dict):
            referenced.update(str(k) for k in declared)
        elif isinstance(declared, list):
            referenced.update(str(k) for k in declared)
    for net in referenced:
        _ensure_network(project.name, net)

    # Idempotent: clear this project's previous containers before recreating.
    existing = _project_containers(project.name)
    if only:
        existing = [r for r in existing if (r.get("labels") or {}).get(LABEL_SERVICE) in set(only)]
    if existing:
        _remove_containers(existing)

    ensured_volumes: set[str] = set()
    warned: set[str] = set()
    started: list[Service] = []
    net_info: dict[str, tuple[str | None, str | None]] = {}
    ip_by_service: dict[str, str] = {}
    gateway_ip: str | None = None
    depended_on = {dep for svc in services for dep in svc.depends_on}
    for service in services:
        image = _resolve_image(service, project, build=build, no_build=no_build)
        boot_hosts = _boot_hosts_lines(service, project, ip_by_service, gateway_ip)
        run_args, wrapped = _build_run_args(
            service,
            project,
            image=image,
            ensured_volumes=ensured_volumes,
            warned=warned,
            boot_hosts=boot_hosts,
        )
        name = service.container_name or f"{project.name}-{service.name}-1"
        print(f"Creating {name} ...")
        result = _run_container_capture(["run", *run_args])
        if result.returncode != 0 and wrapped:
            # Most likely no /bin/sh in the image — relaunch unwrapped and
            # leave name resolution to post-start injection, as before.
            _warn(
                f"service {service.name!r}: boot-time hosts wrapper failed to "
                "start — retrying without it"
            )
            _run_container_capture(["rm", name])
            run_args, _ = _build_run_args(
                service,
                project,
                image=image,
                ensured_volumes=ensured_volumes,
                warned=warned,
            )
            result = _run_container_capture(["run", *run_args])
        if result.returncode != 0:
            sys.stderr.write(result.stderr or result.stdout)
            return result.returncode
        started.append(service)
        # Learn this container's address now so later dependents can bake it
        # into their boot-time /etc/hosts (retry briefly — the address can lag
        # `run` returning by a beat, and a dependent needs it).
        ip, gateway = _container_net(name)
        if ip is None and service.name in depended_on:
            for _ in range(3):
                time.sleep(0.3)
                ip, gateway = _container_net(name)
                if ip:
                    break
        net_info[name] = (ip, gateway)
        if ip:
            ip_by_service[service.name] = ip
        gateway_ip = gateway_ip or gateway

    _inject_etc_hosts(project, started, net_info)

    if detach:
        return 0
    # Foreground: stream the logs of every started service until interrupted.
    return _stream_logs(project, started, follow=True)


def cmd_down(project_name: str, *, remove_volumes: bool, only: list[str]) -> int:
    rows = _project_containers(project_name)
    if only:
        wanted = set(only)
        rows = [r for r in rows if (r.get("labels") or {}).get(LABEL_SERVICE) in wanted]
    if not rows and not only:
        _warn(f"no containers found for project {project_name!r}")
    rc = _remove_containers(rows)

    if only:
        return rc  # partial down never tears down shared infrastructure

    for network in _load_labeled_resources("network", project_name):
        result = _run_container_capture(["network", "rm", network])
        if result.returncode == 0:
            print(f"Removed network {network}")
        else:
            sys.stderr.write(result.stderr or result.stdout)
            rc = rc or result.returncode

    if remove_volumes:
        for volume in _load_labeled_resources("volume", project_name):
            result = _run_container_capture(["volume", "rm", volume])
            if result.returncode == 0:
                print(f"Removed volume {volume}")
            else:
                sys.stderr.write(result.stderr or result.stdout)
                rc = rc or result.returncode
    return rc


def cmd_ps(project_name: str, *, quiet: bool, show_all: bool) -> int:
    rows = _project_containers(project_name)
    if not show_all:
        rows = [r for r in rows if r.get("state") == "running"]
    if quiet:
        for row in rows:
            print(row.get("id", ""))
        return 0
    print("NAME\tIMAGE\tSTATE\tSERVICE")
    for row in rows:
        service = (row.get("labels") or {}).get(LABEL_SERVICE, "")
        print(f"{row.get('name', '')}\t{row.get('image', '')}\t{row.get('state', '')}\t{service}")
    return 0


def cmd_logs(project: Project | None, project_name: str, *, follow: bool, only: list[str]) -> int:
    rows = _project_containers(project_name)
    if only:
        wanted = set(only)
        rows = [r for r in rows if (r.get("labels") or {}).get(LABEL_SERVICE) in wanted]
    if not rows:
        return _die(f"compose: no running containers for project {project_name!r}", 1)
    services = [
        _Stub((row.get("labels") or {}).get(LABEL_SERVICE, ""), row.get("name", ""))
        for row in rows
    ]
    return _stream_logs(None, services, follow=follow)


def cmd_ls() -> int:
    projects = _all_projects()
    print("NAME\tSTATUS")
    for name in sorted(projects):
        rows = projects[name]
        running = sum(1 for row in rows if row.get("state") == "running")
        print(f"{name}\trunning({running})")
    return 0


def cmd_config(project: Project) -> int:
    import json

    services = {svc.name: svc.spec for svc in project.topo_sorted()}
    rendered = {
        "name": project.name,
        "services": services,
        "networks": project.networks,
        "volumes": project.volumes,
    }
    print(json.dumps(rendered, indent=2, default=_as_str))
    return 0


def cmd_build(project: Project, *, only: list[str]) -> int:
    services = project.topo_sorted()
    if only:
        wanted = set(only)
        services = [svc for svc in services if svc.name in wanted]
    built = 0
    for service in services:
        if "build" not in service.spec:
            continue
        image = _resolve_image(service, project, build=True, no_build=False)
        print(f"Built {image} for service {service.name}")
        built += 1
    if built == 0:
        _warn("no services with a 'build:' section")
    return 0


# ========================================================================== #
# Image resolution & build
# ========================================================================== #


def _resolve_image(service: Service, project: Project, *, build: bool, no_build: bool) -> str:
    spec = service.spec
    if "build" in spec and not no_build:
        tag = _as_str(spec.get("image")) or f"{project.name}-{service.name}"
        if build or not _image_exists(tag):
            _run_build(service, project, tag)
        return tag
    image = _as_str(spec.get("image"))
    if not image:
        raise ShimError(f"compose: service {service.name!r} has neither 'image' nor 'build'", 64)
    return image


def _image_exists(tag: str) -> bool:
    result = _run_container_capture(["image", "inspect", tag])
    return result.returncode == 0


def _run_build(service: Service, project: Project, tag: str) -> None:
    build = service.spec["build"]
    if isinstance(build, str):
        context = build
        dockerfile = None
        build_args: dict[str, Any] = {}
    elif isinstance(build, dict):
        context = _as_str(build.get("context") or ".")
        dockerfile = build.get("dockerfile")
        raw_args = build.get("args") or {}
        build_args = raw_args if isinstance(raw_args, dict) else {}
    else:
        raise ShimError(f"compose: service {service.name!r} has an invalid 'build'", 64)

    context_path = context if os.path.isabs(context) else os.path.join(project.directory, context)
    args = ["build", "--tag", tag]
    if dockerfile:
        df = _as_str(dockerfile)
        df_path = df if os.path.isabs(df) else os.path.join(context_path, df)
        args.extend(["--file", df_path])
    for key, value in build_args.items():
        args.extend(["--build-arg", f"{key}={_as_str(value)}"])
    args.append(context_path)

    print(f"Building {tag} for service {service.name} ...")
    result = _run_container_passthrough(args)
    if result != 0:
        raise ShimError(f"compose: build failed for service {service.name!r}", result)


def _network_dict(project: Project) -> dict[str, Any]:
    networks = project.networks
    if isinstance(networks, dict):
        return networks
    return {}


# ========================================================================== #
# Log streaming
# ========================================================================== #


class _Stub:
    """Minimal service-like object for log streaming of existing containers."""

    def __init__(self, name: str, container_name: str):
        self.name = name
        self._container_name = container_name

    @property
    def container_name(self) -> str:
        return self._container_name


def _stream_logs(project: Project | None, services: list[Any], *, follow: bool) -> int:
    targets: list[tuple[str, str]] = []
    for service in services:
        if isinstance(service, _Stub):
            name = service.container_name
        else:
            name = service.container_name or f"{project.name}-{service.name}-1"  # type: ignore[union-attr]
        targets.append((service.name, name))

    if not targets:
        return 0
    if len(targets) == 1 or not follow:
        rc = 0
        for label, name in targets:
            args = ["logs"]
            if follow:
                args.append("--follow")
            args.append(name)
            result = _run_container_passthrough(args)
            rc = rc or result
        return rc

    # Multiple services + follow: one thread per container, prefixed output.
    threads: list[threading.Thread] = []
    for label, name in targets:
        thread = threading.Thread(target=_follow_prefixed, args=(label, name), daemon=True)
        thread.start()
        threads.append(thread)
    try:
        for thread in threads:
            thread.join()
    except KeyboardInterrupt:
        return 130
    return 0


def _follow_prefixed(label: str, name: str) -> None:
    import subprocess

    proc = subprocess.Popen(
        [_container_bin(), "logs", "--follow", name],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(f"{label}  | {line}")
        sys.stdout.flush()


# ========================================================================== #
# Entry point
# ========================================================================== #


def _warn(message: str) -> None:
    print(f"docker-for-apple-container: compose: {message}", file=sys.stderr)


_USAGE = (
    "docker compose [-f FILE] [-p NAME] <up|down|ps|logs|build|config|ls> [options]"
)


def main(argv: list[str]) -> int:
    """Parse `docker compose` global flags, then dispatch the subcommand."""
    file: str | None = None
    project_name: str | None = None
    project_dir: str | None = None
    env_file: str | None = None

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-f", "--file"):
            file, i = _take(argv, i, arg)
            continue
        if arg in ("-p", "--project-name"):
            project_name, i = _take(argv, i, arg)
            continue
        if arg == "--project-directory":
            project_dir, i = _take(argv, i, arg)
            continue
        if arg == "--env-file":
            env_file, i = _take(argv, i, arg)
            continue
        if arg.startswith("--file="):
            file = arg.split("=", 1)[1]
            i += 1
            continue
        if arg.startswith("--project-name="):
            project_name = arg.split("=", 1)[1]
            i += 1
            continue
        if arg in ("-h", "--help"):
            print(_USAGE)
            return 0
        break

    rest = argv[i:]
    if not rest:
        print(_USAGE)
        return 0
    sub, sub_args = rest[0], rest[1:]

    try:
        return _dispatch(
            sub,
            sub_args,
            file=file,
            project_name=project_name,
            project_dir=project_dir,
            env_file=env_file,
        )
    except ShimError as exc:
        return _die(str(exc), exc.code)


def _dispatch(
    sub: str,
    sub_args: list[str],
    *,
    file: str | None,
    project_name: str | None,
    project_dir: str | None,
    env_file: str | None,
) -> int:
    def project() -> Project:
        return _load_project(
            file=file, project_name=project_name, project_dir=project_dir, env_file=env_file
        )

    def resolved_name() -> str:
        # down/ps/logs need only the project name; avoid requiring a compose file.
        if project_name:
            return _sanitize_project_name(project_name)
        env_name = os.environ.get("COMPOSE_PROJECT_NAME")
        if env_name:
            return _sanitize_project_name(env_name)
        try:
            return project().name
        except ShimError:
            directory = os.path.abspath(project_dir or os.getcwd())
            return _sanitize_project_name(os.path.basename(directory.rstrip("/")) or "default")

    if sub == "up":
        opts = _parse_flags(sub_args, bools={"-d", "--detach", "--build", "--no-build"})
        return cmd_up(
            project(),
            detach=opts.flag("-d") or opts.flag("--detach"),
            build=opts.flag("--build"),
            no_build=opts.flag("--no-build"),
            only=opts.positionals,
        )
    if sub == "down":
        opts = _parse_flags(sub_args, bools={"-v", "--volumes"})
        return cmd_down(
            resolved_name(),
            remove_volumes=opts.flag("-v") or opts.flag("--volumes"),
            only=opts.positionals,
        )
    if sub == "ps":
        opts = _parse_flags(sub_args, bools={"-q", "--quiet", "-a", "--all"})
        return cmd_ps(
            resolved_name(),
            quiet=opts.flag("-q") or opts.flag("--quiet"),
            show_all=opts.flag("-a") or opts.flag("--all"),
        )
    if sub == "logs":
        opts = _parse_flags(sub_args, bools={"-f", "--follow"})
        return cmd_logs(
            None,
            resolved_name(),
            follow=opts.flag("-f") or opts.flag("--follow"),
            only=opts.positionals,
        )
    if sub == "build":
        opts = _parse_flags(sub_args, bools=set())
        return cmd_build(project(), only=opts.positionals)
    if sub == "config":
        return cmd_config(project())
    if sub == "ls":
        return cmd_ls()
    return _die(f"compose: unsupported subcommand: {sub}", 64)


# --------------------------------------------------------------------------- #
# Tiny flag parser for subcommands (boolean flags + positional service names).
# --------------------------------------------------------------------------- #


class _Flags:
    def __init__(self, present: set[str], positionals: list[str]):
        self._present = present
        self.positionals = positionals

    def flag(self, name: str) -> bool:
        return name in self._present


def _parse_flags(argv: list[str], *, bools: set[str]) -> _Flags:
    present: set[str] = set()
    positionals: list[str] = []
    for arg in argv:
        if arg in bools:
            present.add(arg)
        elif arg.startswith("-"):
            raise ShimError(f"compose: unsupported option: {arg}", 64)
        else:
            positionals.append(arg)
    return _Flags(present, positionals)


def _take(argv: list[str], index: int, opt: str) -> tuple[str, int]:
    if index + 1 >= len(argv):
        raise ShimError(f"compose: {opt} requires a value", 64)
    return argv[index + 1], index + 2
