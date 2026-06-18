# container-docker-shim

`container-docker-shim` is a small first-party `docker` command wrapper for
Apple's `container` CLI. It exists to satisfy Hermes' Docker backend without
installing Docker Desktop, Podman, or a third-party adapter.

It is a **stateless translator**, not a Docker replacement: it maps the Docker
commands with a clean Apple `container` equivalent and fails loudly on the rest.
Stateful features (compose, event streams) are deliberately out of scope. Apple
`container` is the single source of truth — the shim persists nothing.

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

### Refused, by design

Commands and flags with no verified Apple equivalent fail loudly: `docker compose`
(needs persistent project state — out of scope for a stateless shim),
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
container changes are reflected on the next shim command.

## Tests

Unit tests use a fake `container` binary and do not start real containers:

```bash
python3 -m unittest discover -s tests -v
```

Live smoke testing against Apple `container` is intentionally manual because it
starts and removes containers.
