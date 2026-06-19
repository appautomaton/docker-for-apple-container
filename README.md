# container-docker-shim

`container-docker-shim` is a small first-party `docker` command wrapper for
Apple's `container` CLI. It exists to satisfy Hermes' Docker backend without
installing Docker Desktop, Podman, or a third-party adapter.

It is a **stateless translator**, not a Docker replacement: it maps the Docker
commands with a clean Apple `container` equivalent and fails loudly on the rest.
Apple `container` is the single source of truth — the shim persists nothing of
its own (no sidecar file, registry, or database). Even `docker compose` is
stateless: project membership is stored as labels **in Apple's own object
store**, exactly as Docker Compose does, so every verb reconstructs the project
by querying Apple rather than reading shim-owned state.

## Install

The executable is `bin/docker`.

For Hermes only:

```bash
export HERMES_DOCKER_BINARY=/Users/ac/dev/containers/container-docker-shim/bin/docker
```

For shell-wide use:

```bash
ln -sf /Users/ac/dev/containers/container-docker-shim/bin/docker ~/.local/bin/docker
```

## Requirements

- macOS with Apple `container` 1.0.0
- `container system status` should report the apiserver as running

Start it with:

```bash
container system start
```

## Supported Docker Subset

Three tiers. Anything outside them fails with an explicit exit-64 error instead
of pretending to work.

### Fully translated (the Hermes contract)

- `docker version`
- `docker info --format "{{.Driver}}"`
- `docker build -f DOCKERFILE -t TAG CONTEXT`
- `docker image inspect IMAGE --format "{{json .Config.Entrypoint}}"`
- `docker run -d ... IMAGE CMD...`
- `docker create ... IMAGE CMD...` — same flag translation as `run`, prints the
  new container ID
- `docker ps -a --filter ... --format ...`
- `docker inspect --format "{{.State.FinishedAt}}" CONTAINER`
- `docker start CONTAINER`
- `docker exec [-i] [-e KEY=VALUE] CONTAINER CMD...`
- `docker stop -t N CONTAINER`
- `docker rm [-f] CONTAINER`

### Translated extras

- `docker logs [-f] [--tail N] CONTAINER` — `--tail N` maps to Apple `-n N`
  (and `--tail all` to "print all"); `--since`/`--timestamps` have no Apple
  equivalent and are refused.
- `docker stats [--no-stream] CONTAINER` — Go-template `--format` is refused;
  Apple `--format` accepts only `json|table|yaml|toml`.
- `docker cp SRC DEST` — the positional `container:path` form maps 1:1 onto
  Apple `container copy`; Docker `-a`/`-L` flags are refused.
- `docker restart [-t N] CONTAINER...` — composed from `stop` + `start` (Apple
  has no `restart`); no state is kept between the two calls.
- `docker export [-o FILE] CONTAINER` — maps onto `container export -o`.
  Note: Apple `container export` requires the container to be **stopped**
  (Docker also exports running ones); the shim surfaces Apple's "container
  is not stopped" error rather than silently stopping it for you.
- `docker login [-u USER] [--password-stdin] SERVER` / `docker logout SERVER` —
  delegate to `container registry login/logout`. **Apple stores the
  credential; the shim keeps nothing.** Docker `-p/--password` is refused in
  favor of `--password-stdin`.
- `docker system info` → `docker info`; `docker system prune [--volumes]` runs
  Apple's `prune` + `image prune` + `network prune` (+ `volume prune`). It is
  **non-interactive** — there is no confirmation prompt and `-f`/`-a` are no-ops.

### Thin passthrough (basic forms only)

`docker images`, `docker pull`, `docker push`, `docker tag`, `docker save`,
`docker load`, `docker rmi` (top-level aliases for `docker image <sub>`),
`docker image <sub>` (`pull`/`rm`/`tag`/`push`/`save`/`load`/`prune`/`ls`),
`docker network <sub>` and `docker volume <sub>`
(`create`/`ls`/`rm`/`inspect`/`prune`), and `docker kill [-s SIG]` forward to the
matching Apple `container` command.
Subcommand names and common flags line up, but Docker-only flags are not
translated — Go-template `--format` on `ls`-style commands is refused rather
than mis-forwarded, and subcommands Apple lacks (e.g. `network connect`) fail
loudly.

### Compose (stateless orchestration)

`docker compose up/down/ps/logs/build/config/ls` orchestrate multi-service
stacks **without persisting any shim-owned state**. Apple `container` has no
native compose, so the shim parses the compose file and issues a sequence of
`container` commands — but it keeps no project file. Instead every resource is
tagged with Docker's own label schema (`com.docker.compose.project`,
`com.docker.compose.service`, …) on the containers, the project network, and any
named volumes. `down`/`ps`/`logs`/`ls` reconstruct the project purely by
querying Apple and filtering on those labels; only `up`/`build`/`config` need to
read the compose file.

- **Project name** resolves like Docker: `-p NAME` → `COMPOSE_PROJECT_NAME` →
  the file's `name:` → the directory basename.
- **Service discovery.** Apple does not resolve service names by DNS without an
  admin `container system dns` domain. Instead, after services start, the shim
  appends `<ip>  <service>` lines to **each container's own `/etc/hosts` file**
  (IPs read live from `container inspect`). That file lives in the container's
  ephemeral layer and is discarded when the container is removed — **the macOS
  host's `/etc/hosts` is never touched.**
- **Named volumes** map onto Apple-native volumes (`container volume create`),
  scoped as `<project>_<volume>`. Host-path mounts become bind mounts, with
  relative paths resolved against the compose file's directory.
- **Teardown is self-coherent.** `down` removes the project's containers (found
  by label), then removes the network — and `-v` the volumes — **only if the
  shim created them** (verified via the project label), never external ones.
- **`up` is idempotent**: it removes the project's previous containers before
  recreating, so re-running never accumulates duplicates.
- **YAML** is parsed by a small dependency-free subset parser (block maps and
  sequences, flow collections, quoted scalars, comments, and `${VAR:-default}`
  interpolation). Anchors, multi-document streams, and `|`/`>` block scalars are
  out of scope.

Compose keys with no Apple equivalent (`restart`, `healthcheck`, `privileged`,
`hostname`, `secrets`, `configs`, `deploy` replicas, …) are parsed but ignored,
with a one-line warning per key so behavior is never silently misrepresented.

### Refused, by design

Commands and flags with no verified Apple equivalent fail loudly:
`docker system events` (a stateful watcher), `docker commit`/`diff`/`rename`/
`history`/`import` (no Apple equivalent), `docker run --network=none`,
`docker run --add-host/--hostname`, and any unknown command.

### Caveats

- `--security-opt`, `--pids-limit`, and `--storage-opt` on `run` are accepted as
  **silent no-ops** — Apple `container` documents no equivalent, so a container
  may be less constrained than the flag implies.
- `docker run -v host:ctr:ro` becomes an Apple `--mount` bind (only `ro` mode is
  honored); `--tmpfs` option suffixes are reduced to the mount path.

## State

The shim is stateless. It does not persist Docker-shaped metadata, cache files,
or a support directory. Apple `container` is the source of truth; direct Apple
container changes are reflected on the next shim command. Compose is no
exception: project bookkeeping lives in Apple's label store, not in any
shim-owned file — see the Compose section above.

## Tests

Unit tests use a fake `container` binary and do not start real containers. This
covers the Hermes contract (`tests/test_hermes_contract.py`) and compose —
parser, interpolation, topo sort, translation, and label-based orchestration
(`tests/test_compose.py`):

```bash
python3 -m unittest discover -s tests -v
```

Live smoke testing against Apple `container` is intentionally manual because it
starts and removes containers. The compose path has been verified end-to-end
against Apple `container` 1.0.0 (multi-service `up`, label reconstruction,
service-name resolution, `build:`, named volumes, and clean `down`/`down -v`).
