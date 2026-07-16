# docker-for-apple-container

[![PyPI version](https://img.shields.io/pypi/v/docker-for-apple-container?style=flat-square)](https://pypi.org/project/docker-for-apple-container/)
[![Homebrew](https://img.shields.io/badge/homebrew-tap-orange?style=flat-square)](https://github.com/appautomaton/homebrew-tap)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue?style=flat-square)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-macOS%20%28Apple%20Silicon%29-lightgrey?style=flat-square)](#requirements)
[![Website](https://img.shields.io/badge/website-appautomaton.github.io-blue?style=flat-square)](https://appautomaton.github.io/docker-for-apple-container/)

> Run `docker` and `docker compose` on macOS, backed by Apple's native `container` CLI. No Docker Desktop.

`docker-for-apple-container` is a small `docker` command wrapper for Apple's
`container` CLI. It lets tools that expect a `docker` binary run against Apple
`container` on macOS, without installing Docker Desktop, Podman, or a
third-party adapter.

It is a **stateless translator**, not a Docker replacement. It maps each Docker
command to a clean Apple `container` equivalent and fails loudly on the rest.
Apple `container` is the single source of truth, so the shim persists nothing of
its own (no sidecar file, registry, or database). Even `docker compose` stays
stateless: project membership is stored as labels **in Apple's own object
store**, exactly as Docker Compose does, so every verb reconstructs the project
by querying Apple rather than reading shim-owned state.

## Install

Every method below gives you a `docker` command backed by Apple `container`.
Use them on a Mac that runs Apple `container` rather than Docker Desktop.

With Homebrew:

```bash
brew install appautomaton/tap/docker-for-apple-container
```

With uv:

```bash
uv tool install docker-for-apple-container
```

From source, symlink the launcher onto your PATH:

```bash
git clone https://github.com/appautomaton/docker-for-apple-container.git
cd docker-for-apple-container
ln -sf "$(pwd)/bin/docker" ~/.local/bin/docker
```

After any of these, run `docker` as usual. If a tool resolves its Docker binary
from an environment variable or config setting, point that at the installed
`docker` (or the repo's `bin/docker`).

## Requirements

- macOS 26 with Apple `container` 1.1.0 or newer
- The `container` apiserver running (check with `container system status`)

The current compatibility baseline is Apple `container` 1.1.0. The shim depends
on the `container` executable, not directly on Apple's Containerization Swift
package. Apple selects and bundles Containerization as part of `container`, so
there is no separate framework to install or manage.

Nothing else to install. The shim is pure Python standard library with no
third-party packages, and it runs on the Python that ships with macOS.

Start the apiserver with:

```bash
container system start
```

## Supported Docker Subset

Three tiers. Anything outside them fails with an explicit exit-64 error instead
of pretending to work.

Docker's official CLI reference defines the behavior of the subset documented
here, including [`docker container ls`](https://docs.docker.com/reference/cli/docker/container/ls/),
[`docker inspect`](https://docs.docker.com/reference/cli/docker/inspect/),
[`docker container exec`](https://docs.docker.com/reference/cli/docker/container/exec/),
[`docker system df`](https://docs.docker.com/reference/cli/docker/system/df/),
[`docker compose`](https://docs.docker.com/reference/cli/docker/compose/),
and [Docker output formatting](https://docs.docker.com/go/formatting/). Apple
`container --help` and its runtime JSON define which of those behaviors can be
translated faithfully. Unlisted Docker behavior is not implied; when no
verified Apple equivalent exists, the shim refuses it explicitly.

### Fully translated (the core contract)

- `docker version`
- `docker info --format "{{.Driver}}"`
- `docker build -f DOCKERFILE -t TAG CONTEXT`
- `docker image inspect [--platform OS/ARCH] [-f|--format TEMPLATE] IMAGE...`
- `docker images` / `docker image ls` with Docker-shaped default, quiet,
  digest, no-truncation, JSON, and bounded template output
- `docker run -d ... IMAGE CMD...`
- `docker create ... IMAGE CMD...` uses the same flag translation as `run` and
  prints the new container ID
- `docker ps -a --filter ... --format ...` with Docker-shaped default columns,
  JSON lines, and bounded templates. Stateless filters cover `id`, `name`,
  `label`, `status`, `ancestor`, `network`, and `volume`.
- `docker inspect [--type container] [-f|--format TEMPLATE] CONTAINER...`
- `docker container inspect ...` is an alias for `docker inspect`
- `docker port CONTAINER [PRIVATE_PORT[/PROTO]]` and
  `docker container port ...`
- `docker start CONTAINER...`; attach and interactive modes (`-a`/`-i`)
  require exactly one container
- `docker exec [OPTIONS] CONTAINER CMD...`, including detach, interactive/TTY,
  user, environment, environment-file, and working-directory options
- `docker stop [-s SIGNAL] [-t N] CONTAINER...`
- `docker rm [-f] CONTAINER`

`docker inspect` supports a deliberate template subset: case-sensitive field
paths, optional whitespace, multiple expressions mixed with literal text, and
`json` rendering such as `{{json .Config.Labels}}` or `{{json .}}`. The
Docker-shaped container object includes identity, image, labels, lifecycle
state and timestamps, process arguments and environment, working directory and
user, mounts, published ports, CPU and memory limits, DNS settings, selected
security settings, and primary and per-network addresses. `--format json`
prints one compact object per requested container. Dictionaries and lists
require `json`; unsupported fields and full Go-template features fail clearly
instead of being guessed.

Image inspection uses the same bounded formatter and selects the requested
platform, or the host platform when none is given. Its Docker-shaped object
includes IDs, tags, repository digests, creation time, size, platform, image
configuration, and root filesystem layers. Image-list templates support
`ID`, `Repository`, `Tag`, `Digest`, `CreatedSince`, `CreatedAt`, and `Size`.

### Translated extras

- `docker logs [-f] [--tail N] CONTAINER`. `--tail N` maps to Apple `-n N`
  (and `--tail all` to "print all"). `--since` and `--timestamps` have no Apple
  equivalent, so they are refused.
- `docker stats [--no-stream] CONTAINER`. Go-template `--format` is refused.
  Apple `--format` accepts only `json|table|yaml|toml`.
- `docker cp SRC DEST`. The positional `container:path` form maps 1:1 onto
  Apple `container copy`. Docker `-a` and `-L` flags are refused.
- `docker restart [-t N] CONTAINER...`, composed from `stop` + `start` (Apple
  has no `restart`). No state is kept between the two calls.
- `docker export [-o FILE] CONTAINER` maps onto `container export -o`.
  Note: Apple `container export` requires the container to be **stopped**
  (Docker also exports running ones). The shim surfaces Apple's "container
  is not stopped" error rather than silently stopping it for you.
- `docker login [-u USER] [--password-stdin] SERVER` and `docker logout SERVER`
  delegate to `container registry login/logout`. **Apple stores the
  credential. The shim keeps nothing.** Docker `-p/--password` is refused in
  favor of `--password-stdin`.
- `docker system info` maps to `docker info`. `docker system prune [--volumes]`
  runs Apple's `prune` + `image prune` + `network prune` (+ `volume prune`). It is
  **non-interactive**: there is no confirmation prompt, and `-f`/`-a` are no-ops.
- `docker system df [--format json|table|yaml|toml]` maps directly to Apple's
  Docker-shaped disk-usage report. Go templates and Docker's verbose mode are
  refused because Apple has no faithful equivalent.
- `docker container prune [-f]` maps to Apple's non-interactive stopped-container
  prune. Docker prune filters are refused rather than silently ignored.

### Thin passthrough (basic forms only)

`docker pull`, `docker push`, `docker tag`, `docker save`, `docker load`,
`docker rmi` (the top-level alias for `docker image rm`),
`docker image <sub>` (`pull`/`rm`/`tag`/`push`/`save`/`load`/`prune`),
`docker network <sub>` and `docker volume <sub>`
(`create`/`ls`/`rm`/`inspect`/`prune`), and `docker kill [-s SIG]` forward to the
matching Apple `container` command.
Subcommand names and common flags line up, but Docker-only flags are not
translated. Go-template `--format` on `ls`-style commands is refused rather
than mis-forwarded, and subcommands Apple lacks (e.g. `network connect`) fail
loudly.

### Compose (stateless orchestration)

`docker compose up/down/ps/logs/build/pull/exec/start/stop/restart/rm/config/ls`
orchestrate multi-service stacks **without persisting any shim-owned state**.
Apple `container` has no native compose, so the shim issues a sequence of
`container` commands but keeps no project file. Every resource is tagged with
Docker's own label schema (`com.docker.compose.project`,
`com.docker.compose.service`, and so on) on the containers, the project network,
and any named volumes. Runtime verbs reconstruct membership by querying Apple
and filtering on those labels. `up`, `pull`, `build`, and `config` require the
compose file. `start`, `stop`, `restart`, and `rm` use it for dependency order
when available, then fall back to stable service-name order from labels.

- **Project name** resolves like Docker: `-p NAME` → `COMPOSE_PROJECT_NAME` →
  the file's `name:` → the directory basename.
- **Common runtime verbs remain stateless.** `exec` resolves one service to its
  labeled container and defaults to Compose's interactive TTY behavior (`-T`
  disables the TTY). `start` runs dependencies first; `stop` and `rm` run in
  reverse order; `restart` stops in reverse and starts forward. Without a file,
  service-name sorting supplies a deterministic order. `rm` never prompts;
  `-f` is accepted, and `-s/--stop` stops running services before removal.
- **Pull uses declared images.** `compose pull [SERVICE...]` reads the file and
  forwards each selected service image to Apple's image pull. Build-only
  services are skipped with a clear warning instead of being built implicitly.
- **One container per service.** Membership is reconstructed from Compose labels.
  Duplicate service containers or a container number other than 1 fail clearly;
  the shim never guesses which scaled replica to use.
- **Service discovery.** Apple does not resolve service names by DNS without an
  admin `container system dns` domain. The shim closes the gap in two layers,
  both writing `<ip>  <service>` lines only to **each container's own
  `/etc/hosts` file** (ephemeral, discarded with the container — **the macOS
  host's `/etc/hosts` is never touched**):
  - *Boot-time, for dependencies.* Services start in `depends_on` order, so a
    dependent service's dependencies already have known IPs. The shim wraps
    its entrypoint in a `/bin/sh` prelude that writes those lines **before**
    exec'ing the real process — an app that dials its database in its first
    millisecond still resolves the name (post-start injection alone loses
    that race, and Apple has no restart policies to give the app a second
    try). Requires `/bin/sh` in the image; if the wrapped launch fails, the
    shim retries unwrapped. Opt out per service with
    `x-shim-boot-hosts: false`.
  - *Post-start, for all peers.* After everything is up, the full project's
    lines are appended idempotently into every container via `container exec`
    (IPs read live from `container inspect`), covering peers that aren't
    declared dependencies. With multiple networks, each receiver gets a peer's
    address from the first network they share, matching Docker's network-scoped
    service discovery instead of leaking an unrelated interface address.
- **DNS configuration.** Compose `dns`, `dns_search`, and `dns_opt` values are
  forwarded directly to Apple `container run`, preserving scalar or list order.
- **`host.docker.internal`.** The same `/etc/hosts` injection also publishes
  `host.docker.internal` and `gateway.docker.internal` pointing at the
  container's gateway, which on Apple `container` **is the macOS host.** This
  mirrors Docker Desktop (which adds these names automatically on macOS/Windows),
  so a service that dials the host by that name (for example
  `http://host.docker.internal:8317`) works unchanged. The gateway is read
  per-network from `container inspect`, not hardcoded. Injection is idempotent.
  On shell-less images (e.g. distroless, cloudflare/cloudflared) where `exec sh`
  is impossible, it falls back to `container cp`: /etc/hosts is copied out,
  merged, and copied back via the guest agent — only a container that exits
  before injection lands is skipped, with a warning. It is **compose-only**.
  Bare `docker run` is left alone, since injecting into a possibly short-lived
  container would race its exit (Apple has no `--add-host` flag to set it at
  creation, so it must be done via a post-start `exec`).
- **Named volumes** map onto Apple-native volumes (`container volume create`),
  scoped as `<project>_<volume>`. Host-path mounts become bind mounts, with
  relative paths resolved against the compose file's directory.
- **Teardown is self-coherent.** `down` removes the project's containers (found
  by label), then removes the network (and with `-v`, the volumes) **only if the
  shim created them** (verified via the project label), never external ones.
- **`up` is idempotent**: it removes the project's previous containers before
  recreating, so re-running never accumulates duplicates.
- **YAML** is parsed by a small dependency-free subset parser (block maps and
  sequences, flow collections, quoted scalars, comments, and `${VAR:-default}`
  interpolation). Anchors, multi-document streams, and `|`/`>` block scalars are
  out of scope.

Compose keys with no Apple equivalent (the service-level `restart` policy,
`healthcheck`, `privileged`, `hostname`, `secrets`, `configs`, `deploy`
replicas, `extra_hosts`, network aliases, and `depends_on` conditions beyond
`service_started`) are parsed but ignored, with a one-line warning per service
so behavior is never silently misrepresented.
`compose run`, scaling, health-gated dependencies, network aliases, and
anonymous-volume removal remain intentionally out of scope.

### Refused, by design

Commands and flags with no verified Apple equivalent fail loudly:
`docker system events` (a stateful watcher), `docker commit`/`diff`/`rename`/
`history`/`import` (no Apple equivalent), `docker run --network=none`,
`docker run --add-host/--hostname`, and any unknown command.

### Caveats

- `--security-opt`, `--pids-limit`, and `--storage-opt` on `run` are accepted as
  **silent no-ops**. Apple `container` documents no equivalent, so a container
  may be less constrained than the flag implies.
- `docker run -v host:ctr:ro` becomes an Apple `--mount` bind (only `ro` mode is
  honored). `--tmpfs` option suffixes are reduced to the mount path.

## State

The shim is stateless. It does not persist Docker-shaped metadata, cache files,
or a support directory. Apple `container` is the source of truth, so direct
Apple container changes are reflected on the next shim command. Compose is no
exception: project bookkeeping lives in Apple's label store, not in any
shim-owned file. See the Compose section above.

## Tests

Unit tests use a fake `container` binary and do not start real containers. The
suite separates generic CLI and inspect behavior from focused consumer
contracts, while compose tests cover parsing, interpolation, dependency order,
translation, and label-based orchestration:

```bash
python3 -m unittest discover -s tests -v
```

Live smoke testing against Apple `container` is intentionally manual because it
starts and removes containers. The current development and test-fixture
baseline is Apple `container` 1.1.0.
