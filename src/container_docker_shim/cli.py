from __future__ import annotations

import json
import os
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


def _normalize_list_item(item: dict[str, Any]) -> dict[str, Any]:
    item_id = _first_present(item, ("id", "ID", "containerID", "container_id"))
    name = _first_present(item, ("name", "Name", "names", "Names"))
    if isinstance(name, list):
        name = name[0] if name else None
    item_id = str(item_id or name or "")
    name = str(name or item_id)

    labels = _labels_from_item(item)

    raw_state = (
        _deep_get(item, "status", "state")
        or _deep_get(item, "Status", "State")
        or item.get("state")
        or item.get("State")
        or item.get("status")
        or item.get("Status")
    )
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

    return {
        "id": str(item_id or name),
        "apple_id": str(item_id or name),
        "name": str(name or item_id),
        "image": str(
            item.get("image")
            or item.get("Image")
            or _deep_get(item, "configuration", "image", "reference")
            or _deep_get(item, "image", "reference")
            or ""
        ),
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
        "networks": _normalize_networks(item),
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
        ident = row.get("apple_id") or row.get("id") or row.get("name")
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
        else:
            return False
    return True


def _format_row(template: str, row: dict[str, Any]) -> str:
    rendered = template
    replacements = {
        "{{.ID}}": row.get("id", ""),
        "{{.Names}}": row.get("name", ""),
        "{{.Name}}": row.get("name", ""),
        "{{.Image}}": row.get("image", ""),
        "{{.State}}": row.get("state", ""),
        "{{.Status}}": row.get("state", ""),
    }
    for needle, value in replacements.items():
        rendered = rendered.replace(needle, str(value))
    return rendered


def _extract_entrypoint(image: Any) -> Any:
    candidates = [
        ("Config", "Entrypoint"),
        ("config", "Entrypoint"),
        ("config", "entrypoint"),
        ("config", "config", "Entrypoint"),
        ("config", "config", "entrypoint"),
        ("configuration", "entrypoint"),
        ("configuration", "Entrypoint"),
        ("image", "config", "Entrypoint"),
        ("image", "config", "entrypoint"),
    ]
    for path in candidates:
        value = _deep_get(image, *path)
        if value is not None:
            return value
    for key in ("entrypoint", "Entrypoint"):
        if isinstance(image, dict) and key in image:
            return image[key]
    if isinstance(image, dict) and isinstance(image.get("variants"), list):
        for variant in image["variants"]:
            if not isinstance(variant, dict):
                continue
            value = _extract_entrypoint(variant)
            if value is not None:
                return value
    return None


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
        "Config": {
            "Image": row.get("image", ""),
            "Labels": row.get("labels", {}),
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
            "Networks": networks,
        },
    }


_INSPECT_PATH_SEGMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _compile_inspect_template(template: str) -> list[tuple[str, Any, bool]]:
    tokens: list[tuple[str, Any, bool]] = []
    cursor = 0
    while cursor < len(template):
        start = template.find("{{", cursor)
        stray_close = template.find("}}", cursor)
        if stray_close != -1 and (start == -1 or stray_close < start):
            raise ShimError("malformed inspect format: unmatched '}}'", 64)
        if start == -1:
            tokens.append(("literal", template[cursor:], False))
            break
        if start > cursor:
            tokens.append(("literal", template[cursor:start], False))

        end = template.find("}}", start + 2)
        if end == -1:
            raise ShimError("malformed inspect format: unmatched '{{'", 64)
        expression = template[start + 2:end].strip()
        if not expression or "{{" in expression:
            raise ShimError(
                f"unsupported inspect format expression: {expression!r}", 64
            )

        parts = expression.split()
        use_json = False
        if len(parts) == 1:
            path = parts[0]
        elif len(parts) == 2 and parts[0] == "json":
            use_json = True
            path = parts[1]
        else:
            raise ShimError(f"unsupported inspect format expression: {expression}", 64)

        if path == ".":
            segments: tuple[str, ...] = ()
        elif path.startswith("."):
            segments = tuple(path[1:].split("."))
            if not segments or any(
                not _INSPECT_PATH_SEGMENT.fullmatch(segment) for segment in segments
            ):
                raise ShimError(f"unsupported inspect field path: {path}", 64)
        else:
            raise ShimError(f"unsupported inspect format expression: {expression}", 64)

        tokens.append((path, segments, use_json))
        cursor = end + 2
    return tokens


def _inspect_field_value(
    obj: dict[str, Any], path: str, segments: tuple[str, ...]
) -> Any:
    value: Any = obj
    for segment in segments:
        if not isinstance(value, dict) or segment not in value:
            raise ShimError(f"unsupported inspect field: {path}", 64)
        value = value[segment]
    return value


def _render_inspect_template(
    tokens: list[tuple[str, Any, bool]], obj: dict[str, Any]
) -> str:
    rendered: list[str] = []
    for token, payload, use_json in tokens:
        if token == "literal":
            rendered.append(str(payload))
            continue

        path = token
        value = _inspect_field_value(obj, path, payload)
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
                f"inspect field {path} is a composite value; use '{{{{json {path}}}}}'",
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
        rest_img = argv[1:]
        for idx, token in enumerate(rest_img):
            key, val = _split_long(token)
            if key == "--format":
                fmt = val if val is not None else (rest_img[idx + 1] if idx + 1 < len(rest_img) else "")
                if _is_go_template(fmt):
                    return _die(
                        "docker images Go-template --format is unsupported by Apple "
                        "container; --format accepts json|table|yaml|toml",
                        64,
                    )
        result = _run_container_capture(["image", "list", *rest_img])
        _print_completed(result)
        return result.returncode
    if sub in ("pull", "rm", "tag", "push", "save", "load", "prune"):
        result = _run_container_capture(["image", sub, *argv[1:]])
        _print_completed(result)
        return result.returncode
    return _unsupported("image " + sub)


def cmd_image_inspect(argv: list[str]) -> int:
    fmt = None
    images: list[str] = []
    i = 0
    while i < len(argv):
        arg, value = _split_long(argv[i])
        if arg == "--format":
            fmt = value
            if fmt is None:
                fmt, i = _take_value(argv, i, "--format")
                continue
        elif argv[i].startswith("-"):
            return _die(f"unsupported image inspect option: {argv[i]}", 64)
        else:
            images.append(argv[i])
        i += 1
    if not images:
        return _die("image inspect requires an image", 64)

    result = _run_container_capture(["image", "inspect", *images])
    if result.returncode != 0:
        _print_completed(result)
        return result.returncode
    try:
        data = json.loads(result.stdout or "[]")
    except ValueError as exc:
        return _die(f"could not parse image inspect JSON: {exc}")
    items = data if isinstance(data, list) else [data]

    if fmt:
        if fmt == "{{json .Config.Entrypoint}}":
            entrypoint = _extract_entrypoint(items[0] if items else {})
            print(json.dumps(entrypoint))
            return 0
        return _die(f"unsupported image inspect format: {fmt}", 64)
    print(json.dumps(items, indent=2))
    return 0


def cmd_ps(argv: list[str]) -> int:
    all_containers = False
    quiet = False
    filters: list[str] = []
    fmt = None

    i = 0
    while i < len(argv):
        arg, value = _split_long(argv[i])
        if arg in ("-a", "--all"):
            all_containers = True
        elif arg in ("-q", "--quiet"):
            quiet = True
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

    code, rows, err = _load_container_rows(all_containers)
    if code != 0:
        sys.stderr.write(err)
        return code
    rows = [row for row in rows if _matches_filter(row, filters)]
    if quiet:
        for row in rows:
            print(row["id"])
        return 0
    if fmt:
        for row in rows:
            print(_format_row(fmt, row))
        return 0
    print("CONTAINER ID\tIMAGE\tSTATE\tNAMES")
    for row in rows:
        print(f"{row['id']}\t{row['image']}\t{row['state']}\t{row['name']}")
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
    template = _compile_inspect_template(fmt) if fmt is not None else None

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

    if template is not None:
        output = [_render_inspect_template(template, obj) for obj in objects]
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
    return _unsupported("container " + " ".join(argv))


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
    print("             image inspect, start, exec, stop, restart, rm, logs, cp,")
    print("             stats, export, login, logout, system prune")
    print("Alias:       container inspect")
    print("Inspect fmt: field paths, literal text, and json; not full Go templates")
    print("Compose:     compose up/down/ps/logs/build/config/ls (stateless;")
    print("             project state lives in Apple container labels)")
    print("Passthrough: images, pull, push, tag, save, load, rmi, image <sub>,")
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
