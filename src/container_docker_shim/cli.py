from __future__ import annotations

import datetime
import json
import os
import platform as host_platform
import re
import subprocess
import sys
import uuid
from typing import Any


DOCKER_ZERO_TIME = "0001-01-01T00:00:00Z"


class ShimError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def _container_bin() -> str:
    return os.environ.get("CONTAINER_DOCKER_SHIM_CONTAINER", "/usr/local/bin/container")


def _debug(message: str) -> None:
    if os.environ.get("CONTAINER_DOCKER_SHIM_DEBUG"):
        print(f"docker-for-apple-container debug: {message}", file=sys.stderr)


def _run_container_capture(args: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    cmd = [_container_bin(), *args]
    _debug("container capture: " + json.dumps(cmd))
    try:
        return subprocess.run(
            cmd,
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        raise ShimError(f"Apple container CLI not found: {_container_bin()}")


def _run_container_passthrough(args: list[str]) -> int:
    cmd = [_container_bin(), *args]
    _debug("container passthrough: " + json.dumps(cmd))
    try:
        result = subprocess.run(
            cmd,
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
            check=False,
        )
    except FileNotFoundError:
        raise ShimError(f"Apple container CLI not found: {_container_bin()}")
    return result.returncode


def _print_completed(result: subprocess.CompletedProcess[str]) -> None:
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)


def _die(message: str, code: int = 1) -> int:
    print(f"docker-for-apple-container: {message}", file=sys.stderr)
    return code


def _unsupported(command: str) -> int:
    return _die(
        f"unsupported Docker command: {command}. "
        "This shim translates the Docker commands with a clean Apple container "
        "equivalent; stateful or unmapped commands are intentionally not implemented.",
        64,
    )


def _take_value(argv: list[str], index: int, opt: str) -> tuple[str, int]:
    arg = argv[index]
    if arg.startswith("--") and "=" in arg:
        return arg.split("=", 1)[1], index + 1
    if len(arg) > 2 and arg[0] == "-" and not arg.startswith("--") and arg[:2] in ("-e", "-v", "-w", "-u", "-m", "-p", "-f", "-t", "-c", "-o"):
        return arg[2:], index + 1
    if index + 1 >= len(argv):
        raise ShimError(f"{opt} requires a value", 64)
    return argv[index + 1], index + 2


def _split_long(arg: str) -> tuple[str, str | None]:
    if arg.startswith("--") and "=" in arg:
        key, value = arg.split("=", 1)
        return key, value
    return arg, None


def _normalize_memory(value: str) -> str:
    if value and value[-1:] in ("k", "m", "g", "t", "p"):
        return value[:-1] + value[-1].upper()
    return value


def _normalize_cpus(value: str) -> str:
    try:
        parsed = float(value)
    except ValueError:
        return value
    if parsed.is_integer():
        return str(int(parsed))
    return value


def _translate_volume(value: str) -> list[str]:
    parts = value.split(":")
    if len(parts) < 2:
        raise ShimError(f"unsupported volume syntax: {value}", 64)
    source = parts[0]
    target = parts[1]
    mode = ":".join(parts[2:]) if len(parts) > 2 else ""
    mount = f"type=bind,source={source},target={target}"
    if "ro" in {item.strip() for item in mode.replace(",", ":").split(":") if item.strip()}:
        mount += ",readonly"
    return ["--mount", mount]


def _tmpfs_path(value: str) -> str:
    if ":" not in value:
        return value
    path, options = value.split(":", 1)
    _debug(f"discarding Docker tmpfs options for {path}: {options}")
    return path


def _parse_key_value(value: str) -> tuple[str, str]:
    if "=" not in value:
        return value, ""
    return value.split("=", 1)


def _is_go_template(value: str) -> bool:
    """Docker --format takes Go templates; Apple container takes an enum."""
    return "{{" in (value or "")


def _labels_from_item(item: dict[str, Any]) -> dict[str, str]:
    raw = (
        item.get("labels")
        or item.get("Labels")
        or item.get("annotations")
        or _deep_get(item, "configuration", "labels")
        or _deep_get(item, "Config", "Labels")
        or {}
    )
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    if isinstance(raw, list):
        labels: dict[str, str] = {}
        for entry in raw:
            if isinstance(entry, str):
                key, value = _parse_key_value(entry)
                labels[key] = value
        return labels
    return {}


def _item_has_labels(item: dict[str, Any]) -> bool:
    candidates = (
        ("labels",),
        ("Labels",),
        ("annotations",),
        ("configuration", "labels"),
        ("Config", "Labels"),
    )
    for path in candidates:
        raw = _deep_get(item, *path)
        if isinstance(raw, (dict, list)):
            return True
    return False


def _deep_get(item: Any, *path: str) -> Any:
    cur = item
    for part in path:
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _first_present(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None


def _state_from_item(item: dict[str, Any]) -> Any:
    candidates = (
        _deep_get(item, "status", "state"),
        _deep_get(item, "Status", "State"),
        item.get("state"),
        item.get("State"),
        item.get("status"),
        item.get("Status"),
    )
    for value in candidates:
        if value not in (None, "") and not isinstance(value, (dict, list)):
            return value
    return None


def _image_reference_from_item(item: dict[str, Any]) -> Any:
    candidates = (
        item.get("image"),
        item.get("Image"),
        _deep_get(item, "configuration", "image", "reference"),
        _deep_get(item, "image", "reference"),
    )
    for value in candidates:
        if value not in (None, "") and not isinstance(value, (dict, list)):
            return value
    return None


def _docker_state(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in ("running", "created", "paused", "restarting", "dead"):
        return raw
    if raw in ("stopped", "exited", "terminated"):
        return "exited"
    if not raw:
        return "unknown"
    return raw


def _without_cidr(value: Any) -> str:
    return str(value or "").split("/", 1)[0]


def _normalize_networks(item: dict[str, Any]) -> list[dict[str, str]]:
    raw = _deep_get(item, "status", "networks") or item.get("networks") or []
    if not isinstance(raw, list):
        return []

    networks: list[dict[str, str]] = []
    for attachment in raw:
        if not isinstance(attachment, dict):
            continue
        name = str(
            attachment.get("network")
            or attachment.get("name")
            or attachment.get("NetworkID")
            or ""
        )
        if not name:
            continue
        networks.append(
            {
                "name": name,
                "ipv4": _without_cidr(
                    attachment.get("ipv4Address") or attachment.get("address")
                ),
                "gateway": str(
                    attachment.get("ipv4Gateway")
                    or attachment.get("gateway")
                    or ""
                ),
                "ipv6": _without_cidr(attachment.get("ipv6Address")),
                "mac": str(attachment.get("macAddress") or ""),
            }
        )
    return networks


def _container_configuration(item: dict[str, Any]) -> dict[str, Any]:
    configuration = item.get("configuration") or item.get("Config") or {}
    return configuration if isinstance(configuration, dict) else {}


def _normalize_process(item: dict[str, Any]) -> dict[str, Any]:
    process = _deep_get(item, "configuration", "initProcess") or {}
    if not isinstance(process, dict):
        process = {}
    user = process.get("user") or {}
    rendered_user = ""
    if isinstance(user, str):
        rendered_user = user
    elif isinstance(user, dict):
        user_id = user.get("id") or {}
        raw = user.get("raw") or {}
        if isinstance(user_id, dict) and (
            "uid" in user_id or "gid" in user_id
        ):
            rendered_user = f"{user_id.get('uid', 0)}:{user_id.get('gid', 0)}"
        elif isinstance(raw, dict):
            rendered_user = str(raw.get("userString") or "")
    arguments = process.get("arguments") or []
    environment = process.get("environment") or []
    return {
        "path": str(process.get("executable") or ""),
        "args": [str(value) for value in arguments]
        if isinstance(arguments, list)
        else [],
        "env": [str(value) for value in environment]
        if isinstance(environment, list)
        else [],
        "working_dir": str(process.get("workingDirectory") or ""),
        "tty": bool(process.get("terminal")),
        "user": rendered_user,
    }


def _mount_type(raw: Any) -> tuple[str, str]:
    if isinstance(raw, str):
        return raw, ""
    if not isinstance(raw, dict) or not raw:
        return "bind", ""
    name = next(iter(raw))
    payload = raw.get(name) or {}
    if name == "volume":
        volume_name = payload.get("name") if isinstance(payload, dict) else ""
        return "volume", str(volume_name or "")
    if name == "tmpfs":
        return "tmpfs", ""
    return "bind", ""


def _normalize_mounts(item: dict[str, Any]) -> list[dict[str, Any]]:
    mounts = _deep_get(item, "configuration", "mounts") or item.get("mounts") or []
    if not isinstance(mounts, list):
        return []
    normalized: list[dict[str, Any]] = []
    for mount in mounts:
        if not isinstance(mount, dict):
            continue
        options = mount.get("options") or []
        if not isinstance(options, list):
            options = []
        mount_type, name = _mount_type(mount.get("type"))
        normalized.append(
            {
                "Type": mount_type,
                "Name": name,
                "Source": str(mount.get("source") or ""),
                "Destination": str(mount.get("destination") or ""),
                "Driver": "local" if mount_type == "volume" else "",
                "Mode": ",".join(str(option) for option in options),
                "RW": "ro" not in options,
                "Propagation": "",
            }
        )
    return normalized


def _normalize_published_ports(item: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    ports = _deep_get(item, "configuration", "publishedPorts") or []
    if not isinstance(ports, list):
        return {}
    normalized: dict[str, list[dict[str, str]]] = {}
    for port in ports:
        if not isinstance(port, dict):
            continue
        try:
            host_port = int(port.get("hostPort"))
            container_port = int(port.get("containerPort"))
            count = int(port.get("count") or 1)
        except (TypeError, ValueError):
            continue
        protocol = str(port.get("proto") or "tcp").lower()
        host_ip = str(port.get("hostAddress") or "0.0.0.0")
        for offset in range(max(0, count)):
            key = f"{container_port + offset}/{protocol}"
            normalized.setdefault(key, []).append(
                {
                    "HostIp": host_ip,
                    "HostPort": str(host_port + offset),
                }
            )
    return normalized


def _normalize_resources(item: dict[str, Any]) -> dict[str, int]:
    resources = _deep_get(item, "configuration", "resources") or {}
    if not isinstance(resources, dict):
        resources = {}
    try:
        cpus = int(resources.get("cpus") or 0)
    except (TypeError, ValueError):
        cpus = 0
    try:
        memory = int(resources.get("memoryInBytes") or 0)
    except (TypeError, ValueError):
        memory = 0
    return {"cpus": cpus, "memory": memory}


def _normalize_list_item(item: dict[str, Any]) -> dict[str, Any]:
    item_id = _first_present(item, ("id", "ID", "containerID", "container_id"))
    name = _first_present(item, ("name", "Name", "names", "Names"))
    if isinstance(name, list):
        name = name[0] if name else None
    item_id = str(item_id or name or "")
    name = str(name or item_id)

    labels = _labels_from_item(item)

    raw_state = _state_from_item(item)
    docker_state = _docker_state(raw_state)
    created_at = str(
        _deep_get(item, "configuration", "creationDate")
        or item.get("creationDate")
        or item.get("Created")
        or DOCKER_ZERO_TIME
    )
    started_at = str(
        _deep_get(item, "status", "startedDate")
        or item.get("startedDate")
        or item.get("StartedAt")
        or DOCKER_ZERO_TIME
    )
    explicit_finished_at = (
        _deep_get(item, "status", "finishedDate")
        or _deep_get(item, "status", "finishedAt")
        or _deep_get(item, "status", "stoppedDate")
        or _deep_get(item, "status", "terminatedDate")
        or item.get("finishedAt")
        or item.get("FinishedAt")
    )
    if docker_state == "exited":
        finished_at = str(
            explicit_finished_at
            or (started_at if started_at != DOCKER_ZERO_TIME else None)
            or (created_at if created_at != DOCKER_ZERO_TIME else None)
            or DOCKER_ZERO_TIME
        )
    else:
        finished_at = str(explicit_finished_at or DOCKER_ZERO_TIME)
    configuration = _container_configuration(item)
    process = _normalize_process(item)
    resources = _normalize_resources(item)
    mounts = _normalize_mounts(item)
    networks = _normalize_networks(item)
    ports = _normalize_published_ports(item)
    dns = configuration.get("dns") or {}
    if not isinstance(dns, dict):
        dns = {}

    return {
        "id": str(item_id or name),
        "apple_id": str(item_id or name),
        "name": str(name or item_id),
        "image": str(_image_reference_from_item(item) or ""),
        "image_id": str(
            _deep_get(item, "configuration", "image", "descriptor", "digest")
            or _deep_get(item, "image", "descriptor", "digest")
            or item.get("imageID")
            or item.get("ImageID")
            or ""
        ),
        "state": docker_state,
        "labels": labels,
        "created_at": created_at,
        "started_at": started_at,
        "finished_at": finished_at,
        "networks": networks,
        "ports": ports,
        "mounts": mounts,
        "path": process["path"],
        "args": process["args"],
        "env": process["env"],
        "working_dir": process["working_dir"],
        "tty": process["tty"],
        "user": process["user"],
        "cpus": resources["cpus"],
        "memory": resources["memory"],
        "read_only": bool(configuration.get("readOnly")),
        "shm_size": int(configuration.get("shmSize") or 0),
        "init": bool(configuration.get("useInit")),
        "cap_add": [str(value) for value in configuration.get("capAdd") or []],
        "cap_drop": [str(value) for value in configuration.get("capDrop") or []],
        "dns": [str(value) for value in dns.get("nameservers") or []],
        "dns_search": [str(value) for value in dns.get("searchDomains") or []],
        "dns_options": [str(value) for value in dns.get("options") or []],
        "platform": configuration.get("platform") or {},
    }


def _normalize_inspect_item(item: dict[str, Any], ident: str) -> dict[str, Any]:
    row = _normalize_list_item(item)
    if not row["id"]:
        row["id"] = ident
    if not row["apple_id"]:
        row["apple_id"] = ident
    if not row["name"]:
        row["name"] = ident
    return row


def _list_item_is_complete(item: dict[str, Any], row: dict[str, Any]) -> bool:
    """Whether Apple list JSON has everything current list consumers need.

    Apple container 1.1.0 returns the full configuration and status object from
    ``container list --format json``. Keep a per-record inspect fallback for a
    missing identity, image, state, or labels block so newer schemas degrade
    safely without restoring an inspect call for every listed container.
    """
    return bool(
        row.get("id")
        and _image_reference_from_item(item) is not None
        and _state_from_item(item) is not None
        and _item_has_labels(item)
    )


def _inspect_container_item(ident: str) -> dict[str, Any] | None:
    result = _run_container_capture(["inspect", ident])
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout or "[]")
    except ValueError:
        return None
    item = data[0] if isinstance(data, list) and data else data
    return item if isinstance(item, dict) else None


def _load_container_rows(all_containers: bool) -> tuple[int, list[dict[str, Any]], str]:
    args = ["list"]
    if all_containers:
        args.append("--all")
    args.extend(["--format", "json"])
    result = _run_container_capture(args)
    if result.returncode != 0:
        return result.returncode, [], result.stderr or result.stdout
    try:
        data = json.loads(result.stdout or "[]")
    except ValueError as exc:
        return 1, [], f"could not parse container list JSON: {exc}\n{result.stdout}"
    if isinstance(data, dict):
        data = data.get("containers") or data.get("items") or [data]
    if not isinstance(data, list):
        data = []

    rows: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        row = _normalize_list_item(item)
        if not _list_item_is_complete(item, row):
            ident = (
                row.get("apple_id")
                or row.get("id")
                or row.get("name")
                or _deep_get(item, "configuration", "id")
            )
            inspected = _inspect_container_item(str(ident)) if ident else None
            if inspected is not None:
                row = _normalize_inspect_item(inspected, str(ident))
        rows.append(row)
    return 0, rows, ""


def _matches_filter(row: dict[str, Any], filters: list[str]) -> bool:
    labels: dict[str, str] = row.get("labels") or {}
    for filt in filters:
        if filt.startswith("label="):
            expected = filt[len("label="):]
            if "=" in expected:
                key, value = expected.split("=", 1)
                if labels.get(key) != value:
                    return False
            elif expected not in labels:
                return False
        elif filt.startswith("status="):
            wanted = filt[len("status="):].strip().lower()
            if wanted == "stopped":
                wanted = "exited"
            if row.get("state") != wanted:
                return False
        elif filt.startswith("name="):
            wanted = filt[len("name="):]
            if wanted not in row.get("name", ""):
                return False
        elif filt.startswith("id="):
            wanted = filt[len("id="):]
            if not row.get("id", "").startswith(wanted):
                return False
        elif filt.startswith("ancestor="):
            wanted = filt[len("ancestor="):]
            image = row.get("image", "")
            image_id = row.get("image_id", "")
            repository, _tag = _split_image_reference(image) if image else ("", "")
            if wanted not in (image, repository) and not image_id.startswith(wanted):
                return False
        elif filt.startswith("network="):
            wanted = filt[len("network="):]
            if not any(
                attachment.get("name") == wanted
                or attachment.get("name", "").startswith(wanted)
                for attachment in row.get("networks") or []
            ):
                return False
        elif filt.startswith("volume="):
            wanted = filt[len("volume="):]
            if not any(
                wanted
                in (
                    mount.get("Name", ""),
                    mount.get("Source", ""),
                    mount.get("Destination", ""),
                )
                for mount in row.get("mounts") or []
            ):
                return False
        else:
            return False
    return True


def _format_port_bindings(ports: dict[str, list[dict[str, str]]]) -> str:
    rendered: list[str] = []
    for container_port, bindings in ports.items():
        if not bindings:
            rendered.append(container_port)
            continue
        for binding in bindings:
            host_ip = binding.get("HostIp", "")
            host_port = binding.get("HostPort", "")
            prefix = f"{host_ip}:" if host_ip else ""
            rendered.append(f"{prefix}{host_port}->{container_port}")
    return ", ".join(rendered)


def _docker_ps_object(row: dict[str, Any]) -> dict[str, Any]:
    command = " ".join([row.get("path", ""), *(row.get("args") or [])]).strip()
    labels = row.get("labels") or {}
    mounts = row.get("mounts") or []
    networks = row.get("networks") or []
    started = row.get("started_at") or row.get("created_at")
    return {
        "ID": row.get("id", ""),
        "Image": row.get("image", ""),
        "Command": json.dumps(command) if command else "",
        "CreatedAt": row.get("created_at", DOCKER_ZERO_TIME),
        "RunningFor": _human_age(started),
        "Ports": _format_port_bindings(row.get("ports") or {}),
        "State": row.get("state", ""),
        "Status": row.get("state", ""),
        "Names": row.get("name", ""),
        "Name": row.get("name", ""),
        "Labels": ",".join(f"{key}={value}" for key, value in sorted(labels.items())),
        "Mounts": ",".join(
            mount.get("Name") or mount.get("Source") or "" for mount in mounts
        ),
        "Networks": ",".join(network.get("name", "") for network in networks),
    }


def _split_image_reference(reference: str) -> tuple[str, str]:
    without_digest = reference.split("@", 1)[0]
    last_slash = without_digest.rfind("/")
    last_colon = without_digest.rfind(":")
    if last_colon > last_slash:
        return without_digest[:last_colon], without_digest[last_colon + 1:]
    return without_digest, "latest"


def _parse_timestamp(value: Any) -> datetime.datetime | None:
    raw = str(value or "").strip()
    if not raw or raw == DOCKER_ZERO_TIME:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def _human_age(value: Any) -> str:
    parsed = _parse_timestamp(value)
    if parsed is None:
        return ""
    now = datetime.datetime.now(datetime.timezone.utc)
    seconds = max(0, int((now - parsed).total_seconds()))
    units = (
        (365 * 24 * 60 * 60, "year"),
        (30 * 24 * 60 * 60, "month"),
        (24 * 60 * 60, "day"),
        (60 * 60, "hour"),
        (60, "minute"),
    )
    for length, name in units:
        if seconds >= length:
            count = seconds // length
            suffix = "" if count == 1 else "s"
            return f"{count} {name}{suffix} ago"
    return f"{seconds} second{'s' if seconds != 1 else ''} ago"


def _human_size(value: Any) -> str:
    try:
        size = max(0, int(value or 0))
    except (TypeError, ValueError):
        size = 0
    units = ("B", "kB", "MB", "GB", "TB", "PB")
    amount = float(size)
    unit = units[0]
    for unit in units:
        if amount < 1000 or unit == units[-1]:
            break
        amount /= 1000
    if unit == "B":
        return f"{int(amount)}B"
    rendered = f"{amount:.1f}".rstrip("0").rstrip(".")
    return f"{rendered}{unit}"


def _platform_tuple(value: str) -> tuple[str, str, str]:
    parts = value.split("/")
    if len(parts) not in (2, 3) or not all(parts):
        raise ShimError(
            f"invalid platform {value!r}; expected os/arch[/variant]", 64
        )
    return parts[0], parts[1], parts[2] if len(parts) == 3 else ""


def _host_image_platform() -> tuple[str, str, str]:
    machine = host_platform.machine().lower()
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(machine, machine)
    return "linux", arch, ""


def _select_image_variant(
    item: dict[str, Any], requested_platform: str | None
) -> dict[str, Any]:
    variants = [
        variant
        for variant in item.get("variants") or []
        if isinstance(variant, dict)
    ]
    if not variants:
        return {}
    wanted = (
        _platform_tuple(requested_platform)
        if requested_platform is not None
        else _host_image_platform()
    )
    for variant in variants:
        platform = variant.get("platform") or {}
        candidate = (
            str(platform.get("os") or ""),
            str(platform.get("architecture") or ""),
            str(platform.get("variant") or ""),
        )
        if candidate[:2] == wanted[:2] and (
            not wanted[2] or candidate[2] == wanted[2]
        ):
            return variant
    if requested_platform is not None:
        raise ShimError(f"image has no platform matching {requested_platform}", 1)
    return variants[0]


def _docker_image_object(
    item: dict[str, Any], requested_platform: str | None
) -> dict[str, Any]:
    configuration = item.get("configuration") or {}
    if not isinstance(configuration, dict):
        configuration = {}
    descriptor = configuration.get("descriptor") or {}
    if not isinstance(descriptor, dict):
        descriptor = {}
    variant = _select_image_variant(item, requested_platform)
    variant_config = variant.get("config") or {}
    if not isinstance(variant_config, dict):
        variant_config = {}
    config = variant_config.get("config") or {}
    if not isinstance(config, dict):
        config = {}
    rootfs = variant_config.get("rootfs") or {}
    if not isinstance(rootfs, dict):
        rootfs = {}
    platform = variant.get("platform") or {}
    if not isinstance(platform, dict):
        platform = {}

    reference = str(configuration.get("name") or item.get("name") or "")
    repository, _tag = _split_image_reference(reference) if reference else ("", "")
    digest = str(descriptor.get("digest") or item.get("id") or "")
    created = str(
        variant_config.get("created")
        or configuration.get("creationDate")
        or DOCKER_ZERO_TIME
    )
    size = int(variant.get("size") or descriptor.get("size") or 0)
    repo_tags = [reference] if reference and "@" not in reference else []
    repo_digests = [f"{repository}@{digest}"] if repository and digest else []

    return {
        "Id": digest,
        "RepoTags": repo_tags,
        "RepoDigests": repo_digests,
        "Created": created,
        "Size": size,
        "VirtualSize": size,
        "Architecture": str(
            platform.get("architecture")
            or variant_config.get("architecture")
            or ""
        ),
        "Os": str(platform.get("os") or variant_config.get("os") or ""),
        "Variant": str(platform.get("variant") or ""),
        "Config": {
            "Entrypoint": config.get("Entrypoint"),
            "Cmd": config.get("Cmd"),
            "Env": config.get("Env"),
            "WorkingDir": config.get("WorkingDir") or "",
            "User": config.get("User") or "",
            "Labels": config.get("Labels"),
        },
        "RootFS": {
            "Type": rootfs.get("type") or "layers",
            "Layers": rootfs.get("diff_ids") or [],
        },
    }


def _docker_image_list_row(item: dict[str, Any], *, no_trunc: bool) -> dict[str, Any]:
    configuration = item.get("configuration") or {}
    if not isinstance(configuration, dict):
        configuration = {}
    descriptor = configuration.get("descriptor") or {}
    if not isinstance(descriptor, dict):
        descriptor = {}
    reference = str(configuration.get("name") or item.get("name") or "")
    repository, tag = (
        _split_image_reference(reference) if reference else ("<none>", "<none>")
    )
    digest = str(descriptor.get("digest") or item.get("id") or "")
    image_id = digest if no_trunc else digest.removeprefix("sha256:")[:12]
    created = str(configuration.get("creationDate") or DOCKER_ZERO_TIME)
    size = int(descriptor.get("size") or 0)
    return {
        "ID": image_id,
        "Repository": repository,
        "Tag": tag,
        "Digest": digest,
        "CreatedSince": _human_age(created),
        "CreatedAt": created,
        "Size": _human_size(size),
    }


def _docker_inspect_object(row: dict[str, Any]) -> dict[str, Any]:
    state = row.get("state") or "unknown"
    running = state == "running"
    attachments = row.get("networks") or []
    networks = {
        attachment["name"]: {
            "IPAddress": attachment.get("ipv4", ""),
            "Gateway": attachment.get("gateway", ""),
            "GlobalIPv6Address": attachment.get("ipv6", ""),
            "MacAddress": attachment.get("mac", ""),
        }
        for attachment in attachments
    }
    return {
        "Id": row.get("id", ""),
        "Name": row.get("name", ""),
        "Image": row.get("image_id") or row.get("image", ""),
        "Created": row.get("created_at", DOCKER_ZERO_TIME),
        "Path": row.get("path", ""),
        "Args": row.get("args", []),
        "Platform": row.get("platform", {}),
        "Config": {
            "Image": row.get("image", ""),
            "Labels": row.get("labels", {}),
            "Env": row.get("env", []),
            "WorkingDir": row.get("working_dir", ""),
            "User": row.get("user", ""),
            "Tty": bool(row.get("tty")),
            "Cmd": None,
            "Entrypoint": None,
        },
        "HostConfig": {
            "Memory": row.get("memory", 0),
            "NanoCpus": row.get("cpus", 0) * 1_000_000_000,
            "ReadonlyRootfs": bool(row.get("read_only")),
            "ShmSize": row.get("shm_size", 0),
            "CapAdd": row.get("cap_add", []),
            "CapDrop": row.get("cap_drop", []),
            "Init": bool(row.get("init")),
            "Dns": row.get("dns", []),
            "DnsSearch": row.get("dns_search", []),
            "DnsOptions": row.get("dns_options", []),
        },
        "State": {
            "Status": state,
            "Running": running,
            "StartedAt": row.get("started_at", DOCKER_ZERO_TIME),
            "FinishedAt": (
                DOCKER_ZERO_TIME
                if running
                else row.get("finished_at", DOCKER_ZERO_TIME)
            ),
        },
        "NetworkSettings": {
            "IPAddress": attachments[0].get("ipv4", "") if attachments else "",
            "Ports": row.get("ports", {}),
            "Networks": networks,
        },
        "Mounts": row.get("mounts", []),
    }


_INSPECT_PATH_SEGMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _compile_template(
    template: str, *, subject: str
) -> list[tuple[str, Any, bool]]:
    tokens: list[tuple[str, Any, bool]] = []
    cursor = 0
    while cursor < len(template):
        start = template.find("{{", cursor)
        stray_close = template.find("}}", cursor)
        if stray_close != -1 and (start == -1 or stray_close < start):
            raise ShimError(f"malformed {subject} format: unmatched '}}'", 64)
        if start == -1:
            tokens.append(("literal", template[cursor:], False))
            break
        if start > cursor:
            tokens.append(("literal", template[cursor:start], False))

        end = template.find("}}", start + 2)
        if end == -1:
            raise ShimError(f"malformed {subject} format: unmatched '{{'", 64)
        expression = template[start + 2:end].strip()
        if not expression or "{{" in expression:
            raise ShimError(
                f"unsupported {subject} format expression: {expression!r}", 64
            )

        parts = expression.split()
        use_json = False
        if len(parts) == 1:
            path = parts[0]
        elif len(parts) == 2 and parts[0] == "json":
            use_json = True
            path = parts[1]
        else:
            raise ShimError(
                f"unsupported {subject} format expression: {expression}", 64
            )

        if path == ".":
            segments: tuple[str, ...] = ()
        elif path.startswith("."):
            segments = tuple(path[1:].split("."))
            if not segments or any(
                not _INSPECT_PATH_SEGMENT.fullmatch(segment) for segment in segments
            ):
                raise ShimError(f"unsupported {subject} field path: {path}", 64)
        else:
            raise ShimError(
                f"unsupported {subject} format expression: {expression}", 64
            )

        tokens.append((path, segments, use_json))
        cursor = end + 2
    return tokens


def _template_field_value(
    obj: dict[str, Any], path: str, segments: tuple[str, ...], *, subject: str
) -> Any:
    value: Any = obj
    for segment in segments:
        if not isinstance(value, dict) or segment not in value:
            raise ShimError(f"unsupported {subject} field: {path}", 64)
        value = value[segment]
    return value


def _render_template(
    tokens: list[tuple[str, Any, bool]],
    obj: dict[str, Any],
    *,
    subject: str,
) -> str:
    rendered: list[str] = []
    for token, payload, use_json in tokens:
        if token == "literal":
            rendered.append(str(payload))
            continue

        path = token
        value = _template_field_value(obj, path, payload, subject=subject)
        if use_json:
            rendered.append(
                json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            )
        elif isinstance(value, bool):
            rendered.append("true" if value else "false")
        elif value is None:
            rendered.append("")
        elif isinstance(value, (dict, list)):
            raise ShimError(
                f"{subject} field {path} is a composite value; "
                f"use '{{{{json {path}}}}}'",
                64,
            )
        else:
            rendered.append(str(value))
    return "".join(rendered)


def cmd_version(argv: list[str]) -> int:
    if argv:
        return _unsupported("version " + " ".join(argv))
    cli = _run_container_capture(["--version"])
    if cli.returncode != 0:
        _print_completed(cli)
        return cli.returncode
    status = _run_container_capture(["system", "status"])
    if status.returncode != 0:
        sys.stderr.write(status.stderr or status.stdout)
        if not (status.stderr or status.stdout):
            sys.stderr.write("Apple container service is unavailable. Run: container system start\n")
        return status.returncode
    version = (cli.stdout or "").strip()
    print("Client:")
    print(f" Version: {version}")
    print("Server:")
    print(" Engine: Apple container")
    return 0


def cmd_info(argv: list[str]) -> int:
    fmt = None
    i = 0
    while i < len(argv):
        arg, value = _split_long(argv[i])
        if arg == "--format":
            fmt = value
            if fmt is None:
                fmt, i = _take_value(argv, i, "--format")
                continue
        else:
            return _unsupported("info " + " ".join(argv))
        i += 1

    status = _run_container_capture(["system", "status"])
    if status.returncode != 0:
        sys.stderr.write(status.stderr or status.stdout)
        return status.returncode

    if fmt:
        if fmt == "{{.Driver}}":
            print("apple-container")
            return 0
        return _die(f"unsupported docker info format: {fmt}", 64)
    print("Storage Driver: apple-container")
    print("Container Runtime: Apple container")
    return 0


def cmd_image(argv: list[str]) -> int:
    if not argv:
        return _unsupported("image")
    sub = argv[0]
    if sub == "inspect":
        return cmd_image_inspect(argv[1:])
    if sub in ("ls", "list"):
        return cmd_image_list(argv[1:])
    if sub in ("pull", "rm", "tag", "push", "save", "load", "prune"):
        result = _run_container_capture(["image", sub, *argv[1:]])
        _print_completed(result)
        return result.returncode
    return _unsupported("image " + sub)


def cmd_image_inspect(argv: list[str]) -> int:
    fmt: str | None = None
    requested_platform: str | None = None
    images: list[str] = []
    i = 0
    while i < len(argv):
        raw = argv[i]
        arg, value = _split_long(raw)
        if raw == "-f":
            fmt, i = _take_value(argv, i, "-f")
            continue
        if raw.startswith("-f") and not raw.startswith("--"):
            fmt = raw[2:]
            if fmt.startswith("="):
                fmt = fmt[1:]
        elif arg == "--format":
            fmt = value
            if fmt is None:
                fmt, i = _take_value(argv, i, "--format")
                continue
        elif arg == "--platform":
            requested_platform = value
            if requested_platform is None:
                requested_platform, i = _take_value(argv, i, "--platform")
                continue
        elif raw.startswith("-"):
            return _die(f"unsupported image inspect option: {raw}", 64)
        else:
            images.append(raw)
        i += 1
    if not images:
        return _die("image inspect requires an image", 64)
    template = None
    if fmt is not None and fmt != "json":
        template = _compile_template(fmt, subject="image inspect")

    result = _run_container_capture(["image", "inspect", *images])
    if result.returncode != 0:
        _print_completed(result)
        return result.returncode
    try:
        data = json.loads(result.stdout or "[]")
    except ValueError as exc:
        return _die(f"could not parse image inspect JSON: {exc}")
    items = data if isinstance(data, list) else [data]
    objects = [
        _docker_image_object(item, requested_platform)
        for item in items
        if isinstance(item, dict)
    ]

    if fmt == "json":
        for obj in objects:
            print(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
        return 0
    if template is not None:
        output = [
            _render_template(template, obj, subject="image inspect")
            for obj in objects
        ]
        for line in output:
            print(line)
        return 0
    print(json.dumps(objects, indent=2))
    return 0


def cmd_image_list(argv: list[str]) -> int:
    fmt: str | None = None
    quiet = False
    no_trunc = False
    show_digests = False
    i = 0
    while i < len(argv):
        raw = argv[i]
        arg, value = _split_long(raw)
        if arg in ("-q", "--quiet"):
            quiet = True
        elif arg == "--no-trunc":
            no_trunc = True
        elif arg == "--digests":
            show_digests = True
        elif arg == "--format":
            fmt = value
            if fmt is None:
                fmt, i = _take_value(argv, i, "--format")
                continue
        elif raw.startswith("-"):
            return _die(f"unsupported image list option: {raw}", 64)
        else:
            return _die(f"image list repository filters are unsupported: {raw}", 64)
        i += 1

    template = None
    if fmt is not None and fmt != "json":
        if fmt.startswith("table"):
            return _die("image list table templates are not supported", 64)
        template = _compile_template(fmt, subject="image list")

    result = _run_container_capture(["image", "list", "--format", "json"])
    if result.returncode != 0:
        _print_completed(result)
        return result.returncode
    try:
        data = json.loads(result.stdout or "[]")
    except ValueError as exc:
        return _die(f"could not parse image list JSON: {exc}")
    if isinstance(data, dict):
        data = data.get("images") or data.get("items") or [data]
    if not isinstance(data, list):
        data = []
    rows = [
        _docker_image_list_row(item, no_trunc=no_trunc)
        for item in data
        if isinstance(item, dict)
    ]

    if quiet:
        for row in rows:
            print(row["ID"])
        return 0
    if fmt == "json":
        for row in rows:
            print(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        return 0
    if template is not None:
        output = [
            _render_template(template, row, subject="image list") for row in rows
        ]
        for line in output:
            print(line)
        return 0

    headers = ["REPOSITORY", "TAG"]
    if show_digests:
        headers.append("DIGEST")
    headers.extend(["IMAGE ID", "CREATED", "SIZE"])
    print("\t".join(headers))
    for row in rows:
        values = [row["Repository"], row["Tag"]]
        if show_digests:
            values.append(row["Digest"])
        values.extend([row["ID"], row["CreatedSince"], row["Size"]])
        print("\t".join(values))
    return 0


def cmd_ps(argv: list[str]) -> int:
    all_containers = False
    quiet = False
    filters: list[str] = []
    fmt: str | None = None

    i = 0
    while i < len(argv):
        arg, value = _split_long(argv[i])
        if arg in ("-a", "--all"):
            all_containers = True
        elif arg in ("-q", "--quiet"):
            quiet = True
        elif arg == "--no-trunc":
            pass
        elif arg in ("--filter", "-f"):
            if value is None:
                value, i = _take_value(argv, i, arg)
                filters.append(value)
                continue
            filters.append(value)
        elif arg == "--format":
            fmt = value
            if fmt is None:
                fmt, i = _take_value(argv, i, arg)
                continue
        else:
            return _die(f"unsupported ps option: {argv[i]}", 64)
        i += 1

    allowed_filters = {"id", "name", "label", "status", "ancestor", "network", "volume"}
    for filt in filters:
        key = filt.split("=", 1)[0]
        if "=" not in filt or key not in allowed_filters:
            return _die(f"unsupported docker ps filter: {filt}", 64)
    template = None
    if fmt is not None and fmt != "json":
        if fmt.startswith("table"):
            return _die("docker ps table templates are not supported", 64)
        template = _compile_template(fmt, subject="ps")

    code, rows, err = _load_container_rows(all_containers)
    if code != 0:
        sys.stderr.write(err)
        return code
    rows = [row for row in rows if _matches_filter(row, filters)]
    if quiet:
        for row in rows:
            print(row["id"])
        return 0
    objects = [_docker_ps_object(row) for row in rows]
    if fmt == "json":
        for obj in objects:
            print(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
        return 0
    if template is not None:
        output = [_render_template(template, obj, subject="ps") for obj in objects]
        for line in output:
            print(line)
        return 0
    print("CONTAINER ID\tIMAGE\tCOMMAND\tCREATED\tSTATUS\tPORTS\tNAMES")
    for obj in objects:
        print(
            "\t".join(
                str(obj[key])
                for key in (
                    "ID",
                    "Image",
                    "Command",
                    "RunningFor",
                    "Status",
                    "Ports",
                    "Names",
                )
            )
        )
    return 0


def cmd_inspect(argv: list[str]) -> int:
    fmt: str | None = None
    inspect_type: str | None = None
    ids: list[str] = []
    i = 0
    while i < len(argv):
        raw = argv[i]
        arg, value = _split_long(raw)
        if raw == "-f":
            fmt, i = _take_value(argv, i, "-f")
            continue
        if raw.startswith("-f") and not raw.startswith("--"):
            fmt = raw[2:]
            if fmt.startswith("="):
                fmt = fmt[1:]
        elif arg == "--format":
            fmt = value
            if fmt is None:
                fmt, i = _take_value(argv, i, "--format")
                continue
        elif arg == "--type":
            inspect_type = value
            if inspect_type is None:
                inspect_type, i = _take_value(argv, i, "--type")
                continue
        elif raw.startswith("-"):
            return _die(f"unsupported inspect option: {raw}", 64)
        else:
            ids.append(raw)
        i += 1

    if inspect_type not in (None, "container"):
        return _die(
            f"unsupported inspect type: {inspect_type}. Only container is supported",
            64,
        )
    if not ids:
        return _die("inspect requires at least one container", 64)
    template = (
        _compile_template(fmt, subject="inspect")
        if fmt is not None and fmt != "json"
        else None
    )

    objects: list[dict[str, Any]] = []
    for ident in ids:
        result = _run_container_capture(["inspect", ident])
        if result.returncode != 0:
            _print_completed(result)
            return result.returncode
        try:
            data = json.loads(result.stdout or "[]")
        except ValueError as exc:
            return _die(f"could not parse inspect JSON for {ident}: {exc}")
        item = data[0] if isinstance(data, list) and data else data
        if not isinstance(item, dict):
            item = {}
        row = _normalize_inspect_item(item, ident)
        objects.append(_docker_inspect_object(row))

    if fmt == "json":
        for obj in objects:
            print(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
        return 0

    if template is not None:
        output = [
            _render_template(template, obj, subject="inspect") for obj in objects
        ]
        for line in output:
            print(line)
        return 0

    print(json.dumps(objects, indent=2))
    return 0


def cmd_container(argv: list[str]) -> int:
    if not argv:
        return _die("container requires a subcommand", 64)
    if argv[0] == "inspect":
        return cmd_inspect(argv[1:])
    if argv[0] == "port":
        return cmd_port(argv[1:])
    return _unsupported("container " + " ".join(argv))


def _host_binding(binding: dict[str, str]) -> str:
    host_ip = binding.get("HostIp", "")
    host_port = binding.get("HostPort", "")
    if ":" in host_ip and not host_ip.startswith("["):
        host_ip = f"[{host_ip}]"
    return f"{host_ip}:{host_port}" if host_ip else host_port


def cmd_port(argv: list[str]) -> int:
    if not argv or len(argv) > 2 or argv[0].startswith("-"):
        return _die("port requires CONTAINER [PRIVATE_PORT[/PROTO]]", 64)
    ident = argv[0]
    private_port = argv[1] if len(argv) == 2 else None
    if private_port is not None and "/" not in private_port:
        private_port += "/tcp"
    item = _inspect_container_item(ident)
    if item is None:
        return _die(f"could not inspect container: {ident}", 1)
    row = _normalize_inspect_item(item, ident)
    ports = row.get("ports") or {}
    if private_port is not None:
        bindings = ports.get(private_port) or []
        if not bindings:
            return _die(f"container has no published port {private_port}", 1)
        for binding in bindings:
            print(_host_binding(binding))
        return 0
    for container_port, bindings in ports.items():
        for binding in bindings:
            print(f"{container_port} -> {_host_binding(binding)}")
    return 0


def _parse_run_options(
    argv: list[str],
) -> tuple[list[str], str | None, list[str], bool, str | None]:
    """Translate Docker `run`/`create` flags into Apple container flags.

    Returns (opts, image, command, detach, name). `opts` never contains `-d`;
    the caller decides whether to detach. Raises ShimError on unsupported input
    so main() renders the standard clear error.
    """
    value_options = {
        "--arch",
        "--cap-add",
        "--cap-drop",
        "--cidfile",
        "--dns",
        "--dns-option",
        "--dns-search",
        "--entrypoint",
        "--env-file",
        "--platform",
        "--publish",
        "--runtime",
        "--shm-size",
        "--ulimit",
    }
    bool_options = {"--init", "--read-only", "--rm", "--no-dns"}
    # Common Docker run flags Apple `container run` has no equivalent for. Refuse
    # them with a clear message instead of forwarding a flag the CLI rejects opaquely.
    unsupported_apple_options = {"--add-host", "--hostname"}

    detach = False
    name = None
    opts: list[str] = []
    image = None
    command: list[str] = []

    i = 0
    while i < len(argv):
        current = argv[i]
        if current == "--":
            if i + 1 >= len(argv):
                raise ShimError("run requires an image", 64)
            image = argv[i + 1]
            command = argv[i + 2:]
            break
        if not current.startswith("-"):
            image = current
            command = argv[i + 1:]
            break

        arg, inline_value = _split_long(current)

        if current in ("-d", "--detach"):
            detach = True
        elif current in ("-i", "--interactive"):
            opts.append("-i")
        elif current in ("-t", "--tty"):
            opts.append("-t")
        elif current in ("-it", "-ti"):
            opts.extend(["-i", "-t"])
        elif arg == "--name":
            value = inline_value
            if value is None:
                value, i = _take_value(argv, i, "--name")
            name = value
            opts.extend(["--name", value])
            continue
        elif arg in ("--label", "-l"):
            value = inline_value
            if value is None:
                value, i = _take_value(argv, i, arg)
            opts.extend(["--label", value])
            continue
        elif arg in ("--workdir", "--cwd", "-w"):
            value = inline_value
            if value is None:
                value, i = _take_value(argv, i, arg)
            opts.extend(["-w", value])
            continue
        elif arg in ("--env", "-e"):
            value = inline_value
            if value is None:
                value, i = _take_value(argv, i, arg)
            opts.extend(["-e", value])
            continue
        elif arg in ("--volume", "-v"):
            value = inline_value
            if value is None:
                value, i = _take_value(argv, i, arg)
            opts.extend(_translate_volume(value))
            continue
        elif arg == "--mount":
            value = inline_value
            if value is None:
                value, i = _take_value(argv, i, arg)
            opts.extend(["--mount", value])
            continue
        elif arg == "--tmpfs":
            value = inline_value
            if value is None:
                value, i = _take_value(argv, i, arg)
            opts.extend(["--tmpfs", _tmpfs_path(value)])
            continue
        elif arg in ("--memory", "-m"):
            value = inline_value
            if value is None:
                value, i = _take_value(argv, i, arg)
            opts.extend(["--memory", _normalize_memory(value)])
            continue
        elif arg == "--cpus":
            value = inline_value
            if value is None:
                value, i = _take_value(argv, i, arg)
            opts.extend(["--cpus", _normalize_cpus(value)])
            continue
        elif arg in ("--user", "-u"):
            value = inline_value
            if value is None:
                value, i = _take_value(argv, i, arg)
            opts.extend(["--user", value])
            continue
        elif arg == "--network":
            value = inline_value
            if value is None:
                value, i = _take_value(argv, i, arg)
            if value == "none":
                raise ShimError("Docker --network=none has no verified Apple container equivalent; refusing unsafe translation", 64)
            opts.extend(["--network", value])
            continue
        elif arg in ("--security-opt", "--pids-limit", "--storage-opt"):
            value = inline_value
            if value is None:
                value, i = _take_value(argv, i, arg)
            _debug(f"accepted no-op run option {arg}={value}")
            continue
        elif arg in unsupported_apple_options:
            raise ShimError(f"unsupported docker run option for Apple container: {current}", 64)
        elif arg in value_options or arg in ("-p", "-c"):
            value = inline_value
            if value is None:
                value, i = _take_value(argv, i, arg)
            if arg == "-c":
                opts.extend(["--cpus", _normalize_cpus(value)])
                continue
            opts.extend([arg, value])
            continue
        elif arg in bool_options:
            opts.append(arg)
        else:
            raise ShimError(f"unsupported docker run option: {current}", 64)
        i += 1

    if image is None:
        raise ShimError("run requires an image", 64)
    return opts, image, command, detach, name


def _print_new_container_id(result: subprocess.CompletedProcess[str], fallback: str | None) -> int:
    """Print the container ID from a run -d / create, Docker-style (one ID line)."""
    if result.returncode != 0:
        _print_completed(result)
        return result.returncode
    stdout_lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    container_id = stdout_lines[-1] if stdout_lines else (fallback or "")
    if any(ch.isspace() for ch in container_id):
        container_id = fallback or container_id
    print(container_id)
    return 0


def cmd_run(argv: list[str]) -> int:
    opts, image, command, detach, name = _parse_run_options(argv)
    if not detach:
        return _run_container_passthrough(["run", *opts, image, *command])
    if name is None:
        name = "docker-shim-" + uuid.uuid4().hex[:12]
        opts = ["--name", name, *opts]
    result = _run_container_capture(["run", "-d", *opts, image, *command])
    return _print_new_container_id(result, name)


def cmd_build(argv: list[str]) -> int:
    opts: list[str] = []
    context = "."
    saw_context = False

    value_options = {
        "--arch",
        "--build-arg",
        "--file",
        "--label",
        "--output",
        "--platform",
        "--progress",
        "--secret",
        "--target",
    }
    bool_options = {"--no-cache", "--pull", "--quiet"}

    i = 0
    while i < len(argv):
        current = argv[i]
        if current in ("-h", "--help"):
            return _run_container_passthrough(["build", "--help"])
        if current == "--":
            if i + 1 < len(argv):
                if saw_context:
                    return _die("build accepts only one context directory", 64)
                context = argv[i + 1]
                saw_context = True
                if i + 2 < len(argv):
                    return _die("build accepts only one context directory", 64)
            break
        if not current.startswith("-"):
            if saw_context:
                return _die("build accepts only one context directory", 64)
            context = current
            saw_context = True
            i += 1
            continue

        arg, inline_value = _split_long(current)
        if arg in ("--tag", "-t"):
            value = inline_value
            if value is None:
                value, i = _take_value(argv, i, arg)
            opts.extend(["--tag", value])
            continue
        if arg in ("--file", "-f"):
            value = inline_value
            if value is None:
                value, i = _take_value(argv, i, arg)
            opts.extend(["--file", value])
            continue
        if arg in ("--memory", "-m"):
            value = inline_value
            if value is None:
                value, i = _take_value(argv, i, arg)
            opts.extend(["--memory", _normalize_memory(value)])
            continue
        if arg in ("--cpus", "-c"):
            value = inline_value
            if value is None:
                value, i = _take_value(argv, i, arg)
            opts.extend(["--cpus", _normalize_cpus(value)])
            continue
        if arg in ("--output", "-o"):
            value = inline_value
            if value is None:
                value, i = _take_value(argv, i, arg)
            opts.extend(["--output", value])
            continue
        if arg in value_options:
            value = inline_value
            if value is None:
                value, i = _take_value(argv, i, arg)
            opts.extend([arg, value])
            continue
        if arg in bool_options or arg == "-q":
            opts.append("--quiet" if arg == "-q" else arg)
        elif arg in {"--rm", "--force-rm"}:
            _debug(f"accepted no-op build option {arg}")
        elif arg in {
            "--add-host",
            "--build-context",
            "--cache-from",
            "--cache-to",
            "--iidfile",
            "--load",
            "--network",
            "--push",
            "--ssh",
            "--ulimit",
        }:
            return _die(f"unsupported docker build option for Apple container: {current}", 64)
        else:
            return _die(f"unsupported docker build option: {current}", 64)
        i += 1

    return _run_container_passthrough(["build", *opts, context])


def cmd_start(argv: list[str]) -> int:
    if not argv:
        return _die("start requires at least one container", 64)
    rc = 0
    for ident in argv:
        result = _run_container_capture(["start", ident])
        if result.returncode != 0:
            _print_completed(result)
            rc = result.returncode
            continue
        sys.stdout.write(result.stdout or f"{ident}\n")
    return rc


def cmd_stop(argv: list[str]) -> int:
    timeout = None
    ids: list[str] = []
    i = 0
    while i < len(argv):
        arg, value = _split_long(argv[i])
        if arg in ("-t", "--time", "--timeout"):
            if value is None:
                value, i = _take_value(argv, i, arg)
            timeout = value
            continue
        elif argv[i].startswith("-"):
            return _die(f"unsupported stop option: {argv[i]}", 64)
        else:
            ids.append(argv[i])
        i += 1
    if not ids:
        return _die("stop requires at least one container", 64)

    rc = 0
    for ident in ids:
        args = ["stop"]
        if timeout is not None:
            args.extend(["--time", timeout])
        args.append(ident)
        result = _run_container_capture(args)
        if result.returncode != 0:
            _print_completed(result)
            rc = result.returncode
            continue
        sys.stdout.write(result.stdout or f"{ident}\n")
    return rc


def cmd_rm(argv: list[str]) -> int:
    force = False
    ids: list[str] = []
    for arg in argv:
        if arg in ("-f", "--force"):
            force = True
        elif arg.startswith("-"):
            return _die(f"unsupported rm option: {arg}", 64)
        else:
            ids.append(arg)
    if not ids:
        return _die("rm requires at least one container", 64)

    rc = 0
    for ident in ids:
        args = ["rm"]
        if force:
            args.append("--force")
        args.append(ident)
        result = _run_container_capture(args)
        if result.returncode != 0:
            _print_completed(result)
            rc = result.returncode
            continue
        sys.stdout.write(result.stdout or f"{ident}\n")
    return rc


def cmd_exec(argv: list[str]) -> int:
    interactive = False
    opts: list[str] = []
    ident = None
    command: list[str] = []

    i = 0
    while i < len(argv):
        current = argv[i]
        if not current.startswith("-"):
            ident = current
            command = argv[i + 1:]
            break
        arg, value = _split_long(current)
        if current in ("-i", "--interactive"):
            interactive = True
            opts.append("-i")
        elif current in ("-t", "--tty"):
            opts.append("-t")
        elif current in ("-it", "-ti"):
            interactive = True
            opts.extend(["-i", "-t"])
        elif arg in ("--env", "-e"):
            if value is None:
                value, i = _take_value(argv, i, arg)
            opts.extend(["-e", value])
            continue
        elif arg in ("--workdir", "-w"):
            if value is None:
                value, i = _take_value(argv, i, arg)
            opts.extend(["-w", value])
            continue
        else:
            return _die(f"unsupported exec option: {current}", 64)
        i += 1

    if ident is None:
        return _die("exec requires a container", 64)
    if not command:
        return _die("exec requires a command", 64)

    cmd = [_container_bin(), "exec", *opts, ident, *command]
    _debug("container exec: " + json.dumps(cmd))
    try:
        result = subprocess.run(
            cmd,
            stdin=sys.stdin if interactive else subprocess.DEVNULL,
            stdout=sys.stdout,
            stderr=sys.stderr,
            check=False,
        )
    except FileNotFoundError:
        return _die(f"Apple container CLI not found: {_container_bin()}")
    return result.returncode


def cmd_create(argv: list[str]) -> int:
    # Some callers probe storage-opt support. Docker returns 125 when the driver lacks it.
    if "--storage-opt" in argv or any(arg.startswith("--storage-opt=") for arg in argv):
        return _die("Docker --storage-opt probe is unsupported by Apple container", 125)
    opts, image, command, detach, name = _parse_run_options(argv)
    if detach:
        return _die("create does not take -d/--detach", 64)
    result = _run_container_capture(["create", *opts, image, *command])
    return _print_new_container_id(result, name)


def cmd_logs(argv: list[str]) -> int:
    opts: list[str] = []
    ident = None
    i = 0
    while i < len(argv):
        arg, value = _split_long(argv[i])
        if arg in ("-f", "--follow"):
            opts.append("--follow")
        elif arg in ("-n", "--tail"):
            if value is None:
                value, i = _take_value(argv, i, arg)
            # Docker's default `--tail all` maps to Apple's "omit -n" (print all).
            if value != "all":
                opts.extend(["-n", value])
            continue
        elif arg == "--boot":
            opts.append("--boot")
        elif argv[i].startswith("-"):
            return _die(f"unsupported docker logs option for Apple container: {argv[i]}", 64)
        else:
            ident = argv[i]
        i += 1
    if ident is None:
        return _die("logs requires a container", 64)
    return _run_container_passthrough(["logs", *opts, ident])


def cmd_stats(argv: list[str]) -> int:
    opts: list[str] = []
    targets: list[str] = []
    i = 0
    while i < len(argv):
        arg, value = _split_long(argv[i])
        if arg == "--no-stream":
            opts.append("--no-stream")
        elif arg == "--format":
            if value is None:
                value, i = _take_value(argv, i, arg)
            if _is_go_template(value):
                return _die(
                    "docker stats Go-template --format is unsupported by Apple "
                    "container; --format accepts json|table|yaml|toml",
                    64,
                )
            opts.extend(["--format", value])
            continue
        elif argv[i].startswith("-"):
            return _die(f"unsupported docker stats option for Apple container: {argv[i]}", 64)
        else:
            targets.append(argv[i])
        i += 1
    return _run_container_passthrough(["stats", *opts, *targets])


def cmd_cp(argv: list[str]) -> int:
    paths: list[str] = []
    for arg in argv:
        if arg in ("-a", "--archive", "-L", "--follow-link"):
            return _die(f"unsupported docker cp option for Apple container: {arg}", 64)
        if arg.startswith("-") and arg != "-":
            return _die(f"unsupported docker cp option: {arg}", 64)
        paths.append(arg)
    if len(paths) != 2:
        return _die("cp requires SRC and DEST paths", 64)
    return _run_container_passthrough(["copy", *paths])


def cmd_restart(argv: list[str]) -> int:
    timeout = None
    ids: list[str] = []
    i = 0
    while i < len(argv):
        arg, value = _split_long(argv[i])
        if arg in ("-t", "--time", "--timeout"):
            if value is None:
                value, i = _take_value(argv, i, arg)
            timeout = value
            continue
        elif argv[i].startswith("-"):
            return _die(f"unsupported restart option: {argv[i]}", 64)
        else:
            ids.append(argv[i])
        i += 1
    if not ids:
        return _die("restart requires at least one container", 64)

    # Apple container has no `restart`; compose it from stop + start. Stateless.
    rc = 0
    for ident in ids:
        stop_args = ["stop"]
        if timeout is not None:
            stop_args.extend(["--time", timeout])
        stop_args.append(ident)
        stopped = _run_container_capture(stop_args)
        if stopped.returncode != 0:
            _print_completed(stopped)
            rc = stopped.returncode
            continue
        started = _run_container_capture(["start", ident])
        if started.returncode != 0:
            _print_completed(started)
            rc = started.returncode
            continue
        sys.stdout.write(started.stdout or f"{ident}\n")
    return rc


def cmd_export(argv: list[str]) -> int:
    output = None
    ident = None
    i = 0
    while i < len(argv):
        arg, value = _split_long(argv[i])
        if arg in ("-o", "--output"):
            if value is None:
                value, i = _take_value(argv, i, arg)
            output = value
            continue
        elif argv[i].startswith("-"):
            return _die(f"unsupported docker export option for Apple container: {argv[i]}", 64)
        else:
            ident = argv[i]
        i += 1
    if ident is None:
        return _die("export requires a container", 64)
    args = ["export"]
    if output is not None:
        args.extend(["-o", output])
    args.append(ident)
    return _run_container_passthrough(args)


def cmd_login(argv: list[str]) -> int:
    opts: list[str] = []
    server = None
    i = 0
    while i < len(argv):
        arg, value = _split_long(argv[i])
        if arg in ("-u", "--username"):
            if value is None:
                value, i = _take_value(argv, i, arg)
            opts.extend(["--username", value])
            continue
        elif arg == "--password-stdin":
            opts.append("--password-stdin")
        elif arg in ("-p", "--password"):
            return _die("docker login -p/--password is unsupported; use --password-stdin", 64)
        elif argv[i].startswith("-"):
            return _die(f"unsupported docker login option for Apple container: {argv[i]}", 64)
        else:
            server = argv[i]
        i += 1
    if server is None:
        return _die("login requires a registry server (Apple container has no default registry)", 64)
    # Apple persists the credential; the shim keeps nothing.
    return _run_container_passthrough(["registry", "login", *opts, server])


def cmd_logout(argv: list[str]) -> int:
    servers = [a for a in argv if not a.startswith("-")]
    flags = [a for a in argv if a.startswith("-")]
    if flags:
        return _die(f"unsupported docker logout option for Apple container: {flags[0]}", 64)
    if len(servers) != 1:
        return _die("logout requires a registry server", 64)
    return _run_container_passthrough(["registry", "logout", servers[0]])


def cmd_family(family: str, argv: list[str]) -> int:
    """Thin passthrough for Apple subcommand families (network, volume)."""
    allowed = {"create", "ls", "list", "rm", "delete", "inspect", "prune"}
    if not argv:
        return _unsupported(family)
    sub, rest = argv[0], argv[1:]
    if sub not in allowed:
        return _die(f"unsupported docker {family} subcommand for Apple container: {sub}", 64)
    if sub in ("ls", "list"):
        for idx, token in enumerate(rest):
            key, val = _split_long(token)
            if key == "--format":
                fmt = val if val is not None else (rest[idx + 1] if idx + 1 < len(rest) else "")
                if _is_go_template(fmt):
                    return _die(
                        f"docker {family} ls Go-template --format is unsupported by Apple "
                        "container; --format accepts json|table|yaml|toml",
                        64,
                    )
    result = _run_container_capture([family, sub, *rest])
    _print_completed(result)
    return result.returncode


def cmd_system(argv: list[str]) -> int:
    if not argv:
        return _unsupported("system")
    sub, rest = argv[0], argv[1:]
    if sub == "info":
        return cmd_info(rest)
    if sub == "prune":
        return cmd_system_prune(rest)
    return _die(f"unsupported docker system subcommand for Apple container: {sub}", 64)


def cmd_system_prune(argv: list[str]) -> int:
    prune_volumes = False
    for arg in argv:
        key, _ = _split_long(arg)
        if key == "--volumes":
            prune_volumes = True
        elif key in ("-f", "--force", "-a", "--all"):
            continue  # Apple prune never prompts and has no dangling/all toggle
        elif arg.startswith("-"):
            return _die(f"unsupported docker system prune option: {arg}", 64)
        else:
            return _die(f"system prune takes no positional arguments: {arg}", 64)

    # Map to Apple's per-resource prune subcommands. Non-interactive by nature.
    steps = [["prune"], ["image", "prune"], ["network", "prune"]]
    if prune_volumes:
        steps.append(["volume", "prune"])
    rc = 0
    for step in steps:
        result = _run_container_capture(step)
        _print_completed(result)
        if result.returncode != 0:
            rc = result.returncode
    return rc


def cmd_proxy(container_args: list[str]) -> int:
    result = _run_container_capture(container_args)
    _print_completed(result)
    return result.returncode


def print_help() -> None:
    print("docker-for-apple-container: Docker CLI subset over Apple container")
    print()
    print("Translated:  version, info, build, run, create, ps, inspect,")
    print("             images, image inspect, port, start, exec, stop, restart, rm,")
    print("             logs, cp, stats, export, login, logout, system prune")
    print("Alias:       container inspect")
    print("Inspect fmt: field paths, literal text, and json; not full Go templates")
    print("Compose:     compose up/down/ps/logs/build/config/ls (stateless;")
    print("             project state lives in Apple container labels)")
    print("Passthrough: pull, push, tag, save, load, rmi, image <sub>,")
    print("             network <sub>, volume <sub>, kill")
    print("Unsupported Docker commands and flags fail with a clear, explicit error.")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if not argv:
            print_help()
            return 0
        if argv[0] in ("-h", "--help"):
            print_help()
            return 0
        if argv[0] in ("-v", "--version"):
            cli = _run_container_capture(["--version"])
            _print_completed(cli)
            return cli.returncode

        command = argv[0]
        rest = argv[1:]
        if command == "version":
            return cmd_version(rest)
        if command == "info":
            return cmd_info(rest)
        if command == "image":
            return cmd_image(rest)
        if command == "images":
            return cmd_image(["ls", *rest])
        if command == "pull":
            return cmd_image(["pull", *rest])
        if command == "build":
            return cmd_build(rest)
        if command == "ps":
            return cmd_ps(rest)
        if command == "inspect":
            return cmd_inspect(rest)
        if command == "container":
            return cmd_container(rest)
        if command == "port":
            return cmd_port(rest)
        if command == "run":
            return cmd_run(rest)
        if command == "start":
            return cmd_start(rest)
        if command == "stop":
            return cmd_stop(rest)
        if command in ("rm", "delete"):
            return cmd_rm(rest)
        if command == "exec":
            return cmd_exec(rest)
        if command == "create":
            return cmd_create(rest)
        if command == "logs":
            return cmd_logs(rest)
        if command in ("cp", "copy"):
            return cmd_cp(rest)
        if command == "stats":
            return cmd_stats(rest)
        if command == "restart":
            return cmd_restart(rest)
        if command == "rmi":
            return cmd_image(["rm", *rest])
        if command in ("tag", "push", "save", "load"):
            return cmd_image([command, *rest])
        if command == "export":
            return cmd_export(rest)
        if command == "login":
            return cmd_login(rest)
        if command == "logout":
            return cmd_logout(rest)
        if command in ("network", "volume"):
            return cmd_family(command, rest)
        if command == "system":
            return cmd_system(rest)
        if command == "kill":
            return cmd_proxy([command, *rest])
        if command == "compose":
            from . import compose

            return compose.main(rest)
        return _unsupported(command)
    except ShimError as exc:
        return _die(str(exc), exc.code)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
