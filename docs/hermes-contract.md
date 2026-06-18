# Hermes Docker Contract

Hermes resolves the Docker executable from `HERMES_DOCKER_BINARY` first. The
installed backend then expects Docker-compatible command shapes and selected
Go-template outputs.

Source inspected:

```text
/Users/ac/.hermes/hermes-agent/tools/environments/docker.py
```

## Required Commands

```text
docker version
docker ps -a --filter label=hermes-agent=1 --filter status=exited --format "{{.ID}}"
docker inspect --format "{{.State.FinishedAt}}" CONTAINER
docker rm -f CONTAINER
docker image inspect IMAGE --format "{{json .Config.Entrypoint}}"
docker ps -a --filter label=hermes-agent=1 --filter label=hermes-task-id=TASK --filter label=hermes-profile=PROFILE --format "{{.ID}}\t{{.State}}"
docker start CONTAINER
docker run -d [--init] --name NAME --label ... -w CWD [flags] IMAGE sleep infinity
docker exec [-i] [-e KEY=VALUE ...] CONTAINER bash [-l] -c SCRIPT
docker stop -t 10 CONTAINER
```

## Apple Container Gaps Covered Here

- `container list` has no Docker `ps --filter` or Go-template output, so the
  shim filters and formats client-side.
- `container inspect` has no Docker `inspect --format`, so the shim emits the
  specific Docker fields Hermes asks for.
- `container image inspect` has no Docker `--format`, so the shim extracts
  `Config.Entrypoint` or returns JSON `null`.
- Apple `container` does not document equivalents for Docker
  `--security-opt no-new-privileges` or `--pids-limit`; the shim accepts them
  as no-ops.
- Docker tmpfs option suffixes such as `:rw,noexec,nosuid,size=256m` are
  reduced to the tmpfs path because Apple `container` documents only the path.

## Statelessness

The shim must not persist Docker-shaped metadata. Apple `container list` and
`container inspect` are the only source of truth for container existence,
labels, image, state, and mounts. This prevents stale shim-owned records from
making Hermes reuse a container that Apple `container` no longer has.
