#!/usr/bin/env bash
# One-shot setup on a 42 school computer (Linux, small home quota, big shared /sgoinfre/goinfre/Perso/$USER).
#
#   git clone <repo> agent_smith && cd agent_smith
#   ./scripts/school_setup.sh --moulinette /path/to/moulinette[.tar.gz|.zip] --env /path/to/.env
#   source .school.env            # same shell: PATH to uv, caches in sgoinfre, DOCKER_HOST
#
#   ./scripts/school_setup.sh --uninstall    # remove everything this script created (./.env is kept)
#
# What goes where
#   home ($HOME)            the clone itself (~15 MB), ~/.agent_smith.env (API keys, chmod 600), uv binary
#   sgoinfre ($SGO)        Python 3.10, uv cache, both virtualenvs, HuggingFace cache, runs/, evaluations/,
#                           cache/, Docker images (if Docker stored them in home), logs
#   The heavy folders of the clone are symlinks into $SGO, so every command keeps working unchanged.
#
# Speed: Python + both `uv sync` run in parallel, and the Docker images (python:3.11-slim for MBPP
# validation, then the subject's starter SWE-bench tasks) are pulled in the background while you start working.
#
# Options
#   --sgo DIR              data folder (default /sgoinfre/goinfre/Perso/$USER/agent_smith; without sgoinfre:
#                          /goinfre/$USER/agent_smith, else ~/agent_smith_data)
#   --moulinette PATH      moulinette folder or archive (default: ./moulinette, then ~/moulinette, ~/Downloads, $SGO)
#   --env FILE             .env with the API keys (copied to ~/.agent_smith.env; default: keep ./.env if present)
#   --prefetch starters|none|ID[,ID]   SWE-bench images to pull in the background (default starters = the subject's
#                          suggested tasks; any other image is pulled by the agent when its task starts)
#   --no-check             skip the sandbox security check at the end (~15 s)
#   --yes                  do not ask before moving Docker's image store out of home
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
SGO_BASE="/sgoinfre/goinfre/Perso/$USER"; SGO=""; MOUL_SRC=""; ENV_SRC=""; PREFETCH="starters"; CHECK=1; YES=0; UNINSTALL=0
args=()
for a in "$@"; do   # a chat or an editor may turn "--sgo" into "—sgo" / "–sgo": accept those too
  case "$a" in —*) a="--${a#—}" ;; –*) a="--${a#–}" ;; esac
  args+=("$a")
done
set -- "${args[@]+"${args[@]}"}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sgo) SGO="$2"; shift 2 ;;
    --moulinette) MOUL_SRC="$2"; shift 2 ;;
    --env) ENV_SRC="$2"; shift 2 ;;
    --prefetch) PREFETCH="$2"; shift 2 ;;
    --no-check) CHECK=0; shift ;;
    --yes|-y) YES=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown option $1 (see --help)"; exit 2 ;;
  esac
done
# Data folder: --sgo / SGO=..., else sgoinfre; on a computer without sgoinfre, /goinfre/$USER, else the home folder.
if [[ -z "$SGO" ]]; then
  if [[ -d "$SGO_BASE" || -w "$(dirname "$SGO_BASE")" ]] || mkdir -p "$SGO_BASE" 2>/dev/null; then
    SGO="$SGO_BASE/agent_smith"
  elif [[ -d "/goinfre/$USER" || -w /goinfre ]]; then
    SGO="/goinfre/$USER/agent_smith"
  else
    SGO="$HOME/agent_smith_data"
  fi
fi
STATE="$SGO/state"; LOGS="$SGO/logs"; KEYS="$HOME/.agent_smith.env"
G=$'\e[32m'; R=$'\e[31m'; Y=$'\e[33m'; B=$'\e[1m'; N=$'\e[0m'
say()  { echo "${B}==>${N} $*"; }
ok()   { echo "  ${G}ok${N}  $*"; }
warn() { echo "  ${Y}!!${N}  $*"; }
die()  { echo "  ${R}xx${N}  $*"; exit 1; }
T0=$SECONDS; lap() { echo "      ($((SECONDS - T0)) s)"; }
ask()  { [[ $YES == 1 ]] && return 0; read -r -p "  $1 [Y/n] " a; [[ -z "$a" || "$a" =~ ^[Yy] ]]; }
mark() { mkdir -p "$STATE"; echo "$1" >> "$STATE/done"; }
did()  { [[ -f "$STATE/done" ]] && grep -qx "$1" "$STATE/done"; }
HEAVY=(.venv moulinette/.venv runs evaluations cache)

# ------------------------------------------------------------------ uninstall
if [[ $UNINSTALL == 1 ]]; then
  did setup || die "$SGO was not created by this script (no $STATE/done): nothing removed — check --sgo"
  say "Removing what school_setup.sh created"
  if [[ -f "$STATE/images" ]] && command -v docker >/dev/null; then
    while read -r img; do docker rmi "$img" >/dev/null 2>&1 && ok "image $img"; done < "$STATE/images"
  fi
  ids=$(docker ps -aq --filter label=agent-smith 2>/dev/null); [[ -n "$ids" ]] && docker rm -f $ids >/dev/null && ok "agent containers"
  if did docker-moved; then
    if [[ -f "$STATE/daemon.json.bak" ]]; then cp "$STATE/daemon.json.bak" "$HOME/.config/docker/daemon.json"
    else rm -f "$HOME/.config/docker/daemon.json"; fi
    systemctl --user restart docker 2>/dev/null; ok "Docker image store back to its default place"
  fi
  if did podman-moved; then
    if [[ -f "$STATE/storage.conf.bak" ]]; then cp "$STATE/storage.conf.bak" "$HOME/.config/containers/storage.conf"
    else rm -f "$HOME/.config/containers/storage.conf"; fi
    ok "Podman storage back to its default place"
  fi
  if did podman-registries; then
    if [[ -f "$STATE/registries.conf.bak" ]]; then cp "$STATE/registries.conf.bak" "$HOME/.config/containers/registries.conf"
    else rm -f "$HOME/.config/containers/registries.conf"; fi
    ok "Podman registries config restored"
  fi
  did uv-installed && rm -f "$HOME/.local/bin/uv" "$HOME/.local/bin/uvx" && ok "uv binary"
  for p in "${HEAVY[@]}"; do [[ -L "$p" ]] && rm -f "$p"; done
  # ./.env stays: the link to ~/.agent_smith.env becomes a real file (chmod 600) in the project
  if [[ -L .env ]]; then
    if [[ -f "$KEYS" ]]; then cp --remove-destination "$KEYS" .env 2>/dev/null || { rm -f .env; cp "$KEYS" .env; }; chmod 600 .env
    else rm -f .env; fi
  fi
  rm -f .school.env "$KEYS" && ok "links removed; API keys kept in ./.env only (~/.agent_smith.env removed)"
  chmod -R u+w "$SGO" 2>/dev/null; rm -rf "$SGO" && ok "$SGO"
  echo; echo "Left: the clone itself. Remove it with:  rm -rf \"$ROOT\""
  exit 0
fi

# ------------------------------------------------------------------ 1. places
say "1. Folders"
[[ "$(uname -s)" == Linux ]] || warn "not Linux: fine for a dry run, the script targets the school computers"
if [[ ! -d "$(dirname "$SGO")" ]]; then
  mkdir -p "$(dirname "$SGO")" 2>/dev/null || die "cannot create $(dirname "$SGO") — is /sgoinfre mounted? use --sgo DIR"
  chmod 700 "$(dirname "$SGO")"
fi
mkdir -p "$SGO" "$LOGS" "$STATE" && chmod 700 "$SGO" || die "cannot write $SGO"
did setup || mark setup
ok "data in $SGO ($(df -h "$SGO" | awk 'NR==2{print $4}') free), home has $(df -h "$HOME" | awk 'NR==2{print $4}') free"
case "$ROOT/" in "$SGO"/*|"$SGO_BASE"/*) IN_SGO=1 ;; *) IN_SGO=0 ;; esac
for p in "${HEAVY[@]}"; do
  [[ $IN_SGO == 1 ]] && break
  dst="$SGO/data/${p//\//_}"
  mkdir -p "$(dirname "$p")" "$dst"
  if [[ -L "$p" ]]; then continue; fi
  if [[ -e "$p" ]]; then cp -a "$p/." "$dst/" 2>/dev/null; rm -rf "$p"; fi
  ln -s "$dst" "$p"
done
[[ $IN_SGO == 1 ]] && ok "clone already in sgoinfre: no links needed" || ok "linked ${HEAVY[*]} -> $SGO/data/"

# ------------------------------------------------------------------ 2. moulinette
say "2. Moulinette"
is_moul() { [[ -f "$1/pyproject.toml" && -d "$1/moulinette" ]]; }
if ! is_moul moulinette; then
  if [[ -z "$MOUL_SRC" ]]; then
    for c in "$HOME/moulinette" "$HOME/Downloads/moulinette" "$SGO_BASE/moulinette" "$ROOT/../moulinette"; do
      is_moul "$c" && MOUL_SRC="$c" && break
    done
    [[ -z "$MOUL_SRC" ]] && MOUL_SRC=$(ls -t "$HOME"/Downloads/moulinette*.{tar.gz,tgz,zip} 2>/dev/null | head -1)
  fi
  [[ -n "$MOUL_SRC" && -e "$MOUL_SRC" ]] || die "moulinette not found: pass --moulinette <folder or archive> (from the subject page)"
  src="$MOUL_SRC"
  if [[ -f "$src" ]]; then
    tmp=$(mktemp -d)
    case "$src" in *.zip) unzip -q "$src" -d "$tmp" ;; *) tar -xf "$src" -C "$tmp" ;; esac || die "cannot extract $src"
    src=$(dirname "$(find "$tmp" -maxdepth 3 -name pyproject.toml -path '*moulinette*' | head -1)")
    [[ -z "$src" || "$src" == . ]] && src=$(dirname "$(find "$tmp" -maxdepth 3 -name pyproject.toml | head -1)")
  fi
  is_moul "$src" || die "$MOUL_SRC does not look like the moulinette (no pyproject.toml + moulinette/)"
  # sources only: its .venv is rebuilt below (into sgoinfre)
  mkdir -p moulinette && (cd "$src" && tar --exclude=.venv --exclude=__pycache__ -cf - .) | (cd moulinette && tar -xf -)
  [[ -n "${tmp:-}" ]] && rm -rf "$tmp"
fi
ok "moulinette sources in ./moulinette"
[[ -L moulinette/.venv || $IN_SGO == 1 ]] || { mkdir -p "$SGO/data/moulinette_.venv"; ln -s "$SGO/data/moulinette_.venv" moulinette/.venv; }

# ------------------------------------------------------------------ 3. API keys
say "3. API keys"
if [[ -n "$ENV_SRC" ]]; then
  [[ -f "$ENV_SRC" ]] || die "no such file: $ENV_SRC"
  # re-run with --env .env while .env already links to the keys: nothing to copy
  [[ "$(readlink -f "$ENV_SRC")" == "$(readlink -f "$KEYS" 2>/dev/null)" ]] || install -m 600 "$ENV_SRC" "$KEYS"
elif [[ -f .env && ! -L .env ]]; then
  install -m 600 .env "$KEYS" && rm -f .env
elif [[ ! -f "$KEYS" ]]; then
  install -m 600 .env.example "$KEYS"; warn "no keys given: fill $KEYS (template copied from .env.example)"
fi
ln -sfn "$KEYS" .env
n=$(grep -cE '^[A-Z_]+_API_KEYS?=.+' "$KEYS" || true)
ok "keys in $KEYS (chmod 600, home only), ./.env links to it — $n key line(s) filled"

# ------------------------------------------------------------------ 4. Docker
say "4. Docker"
export UV_CACHE_DIR="$SGO/uv-cache" UV_PYTHON_INSTALL_DIR="$SGO/uv-python" HF_HOME="$SGO/hf"
DOCKER_OK=0; PODMAN=0; DH=""
if ! command -v docker >/dev/null; then
  warn "no docker command: MBPP/SWE-bench validation and SWE-bench runs need Docker (or podman-docker)"
else
  docker --version 2>/dev/null | grep -qi podman && PODMAN=1
  if [[ $PODMAN == 1 ]]; then
    systemctl --user start podman.socket 2>/dev/null
    DH="unix://${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock"; export DOCKER_HOST="$DH"
    # Podman refuses short image names (swebench/..., python:3.11-slim) without a TTY: resolve them on Docker Hub
    reg="$HOME/.config/containers/registries.conf"
    if ! grep -qs 'unqualified-search-registries *= *\["docker.io"\]' "$reg"; then
      mkdir -p "$HOME/.config/containers"
      [[ -f "$reg" ]] && cp "$reg" "$STATE/registries.conf.bak"
      printf 'unqualified-search-registries = ["docker.io"]\nshort-name-mode = "permissive"\n' > "$reg"
      mark podman-registries
    fi
    ok "Podman: short image names resolve on docker.io ($reg)"
  else
    DH=$(docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null); [[ -n "$DH" ]] && export DOCKER_HOST="$DH"
  fi
  if root=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null) || root=$(docker info --format '{{.Store.GraphRoot}}' 2>/dev/null); then
    DOCKER_OK=1; ok "daemon reachable, images stored in $root"
    case "$root/" in
      "$HOME"/*)
        warn "images would fill your home quota (one SWE-bench image = 1.5-4 GB)"
        if ask "move the image store to $SGO/docker?"; then
          if [[ $PODMAN == 1 ]]; then
            mkdir -p "$HOME/.config/containers" "$SGO/containers"
            [[ -f "$HOME/.config/containers/storage.conf" ]] && cp "$HOME/.config/containers/storage.conf" "$STATE/storage.conf.bak"
            printf '[storage]\ndriver = "overlay"\ngraphroot = "%s"\n' "$SGO/containers" > "$HOME/.config/containers/storage.conf"
            podman system migrate >/dev/null 2>&1; systemctl --user restart podman.socket 2>/dev/null; mark podman-moved
          else
            mkdir -p "$HOME/.config/docker" "$SGO/docker"
            f="$HOME/.config/docker/daemon.json"; [[ -f "$f" ]] && cp "$f" "$STATE/daemon.json.bak"
            python3 - "$f" "$SGO/docker" <<'PY'
import json, sys, os
f, root = sys.argv[1], sys.argv[2]
d = json.load(open(f)) if os.path.exists(f) and os.path.getsize(f) else {}
d["data-root"] = root
json.dump(d, open(f, "w"), indent=2)
PY
            systemctl --user restart docker || die "could not restart rootless Docker (systemctl --user restart docker)"
            mark docker-moved
          fi
          root=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || docker info --format '{{.Store.GraphRoot}}' 2>/dev/null)
          ok "images now stored in $root"
        else
          warn "kept in home: SWE-bench images are NOT prefetched"; PREFETCH=none
        fi ;;
    esac
  else
    warn "docker daemon not reachable (not started, or you are not in the docker group): validation will fail"
  fi
fi

# ------------------------------------------------------------------ 5. background image pulls
if [[ $DOCKER_OK == 1 ]]; then
  starters="sympy__sympy-14711 sympy__sympy-13480 pydata__xarray-4629"   # the subject's suggested tasks
  case "$PREFETCH" in starters|all) ids="$starters" ;; none) ids="" ;; *) ids="${PREFETCH//,/ }" ;; esac
  plat=""; [[ "$(uname -m)" != x86_64 ]] && plat="--platform linux/amd64"
  imgs=("docker.io/library/python:3.11-slim")   # full names: Podman refuses short ones without a TTY
  for id in $ids; do imgs+=("docker.io/swebench/sweb.eval.x86_64.$(echo "$id" | tr '[:upper:]' '[:lower:]' | sed 's/__/_1776_/g'):latest"); done
  for i in "${imgs[@]}"; do docker image inspect "$i" >/dev/null 2>&1 || echo "$i" >> "$STATE/images"; done
  [[ -f "$STATE/images" ]] && sort -u -o "$STATE/images" "$STATE/images"
  ( for i in "${imgs[@]}"; do
      docker image inspect "$i" >/dev/null 2>&1 && { echo "have $i"; continue; }
      echo "pull $i"; err=$(docker pull $plat "$i" 2>&1 >/dev/null) && echo "done $i" || echo "FAILED $i: ${err: -300}"
    done; echo "ALL DONE" ) > "$LOGS/pull.log" 2>&1 &
  echo $! > "$STATE/pull.pid"
  ok "pulling ${#imgs[@]} image(s) in the background — progress: tail -f $LOGS/pull.log"
fi

# ------------------------------------------------------------------ 6. uv + Python + dependencies
say "5. Python 3.10 and dependencies (parallel)"
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >"$LOGS/uv-install.log" 2>&1 || die "uv install failed (see $LOGS/uv-install.log)"
  mark uv-installed
fi
ok "uv $(uv --version | awk '{print $2}')"
uv python install 3.10 >"$LOGS/python.log" 2>&1 || die "Python 3.10 install failed (see $LOGS/python.log)"
ok "Python 3.10 in $UV_PYTHON_INSTALL_DIR"
(uv sync --frozen --python 3.10 >"$LOGS/sync-agent.log" 2>&1; echo $? > "$STATE/sync-agent.rc") &
p1=$!
(cd moulinette && uv sync --python 3.10 $( [[ -f uv.lock ]] && echo --frozen ) >"$LOGS/sync-moulinette.log" 2>&1; echo $? > "$STATE/sync-moul.rc") &
p2=$!
wait $p1 $p2
[[ "$(cat "$STATE/sync-agent.rc")" == 0 ]] || die "agent dependencies failed (see $LOGS/sync-agent.log)"
[[ "$(cat "$STATE/sync-moul.rc")" == 0 ]] || die "moulinette dependencies failed (see $LOGS/sync-moulinette.log)"
# The moulinette is an editable install (a .pth file points at its sources); seen on 42 computers: the .pth missing
# after the parallel sync, so `moulinette_eval` failed with "No module named 'moulinette'". Check and reinstall.
if ! (cd moulinette && .venv/bin/python -c "import moulinette" >/dev/null 2>&1); then
  warn "moulinette not importable after sync: reinstalling it"
  (cd moulinette && uv sync --python 3.10 --reinstall-package moulinette >>"$LOGS/sync-moulinette.log" 2>&1)
  (cd moulinette && .venv/bin/python -c "import moulinette" >/dev/null 2>&1) \
    || die "moulinette still not importable (see $LOGS/sync-moulinette.log)"
fi
ok "agent and moulinette environments ready"; lap

# ------------------------------------------------------------------ 7. shell file
cat > .school.env <<EOF
# written by scripts/school_setup.sh — load with: source .school.env
export PATH="\$HOME/.local/bin:\$PATH"
export UV_CACHE_DIR="$UV_CACHE_DIR" UV_PYTHON_INSTALL_DIR="$UV_PYTHON_INSTALL_DIR" HF_HOME="$HF_HOME"
# Every uv command uses Python 3.10: the school's system Python is newer, and a moulinette synced on it fails to build
# numpy (no wheel for that version). Matters for a plain "uv sync" in a fresh moulinette folder, as a corrector does.
export UV_PYTHON=3.10
${DH:+export DOCKER_HOST="$DH"}
EOF
ok "wrote .school.env"

# ------------------------------------------------------------------ 8. check
if [[ $CHECK == 1 ]]; then
  say "6. Sandbox security check"
  if ./scripts/mock_exam.sh sandbox --student-path . --moulinette-path ./moulinette --env-file .env > "$LOGS/sandbox.log" 2>&1; then
    ok "$(grep -E '[0-9]+/[0-9]+' "$LOGS/sandbox.log" | tail -1 | sed 's/\x1b\[[0-9;]*m//g' | xargs)"
  else
    warn "sandbox check failed — see $LOGS/sandbox.log"; grep -E 'FAIL' "$LOGS/sandbox.log" | head -5
  fi
fi

# ------------------------------------------------------------------ summary
echo
say "Ready in $((SECONDS - T0)) s.  Home: $(du -sh "$ROOT" 2>/dev/null | cut -f1) clone + keys;  sgoinfre: $(du -sh "$SGO" 2>/dev/null | cut -f1) so far"
cat <<EOF

  ${B}make up${N}        # UI on http://127.0.0.1:8765   (make loads .school.env by itself)
  make exam      # full mock exam            make   = every command
  tail -f $LOGS/pull.log    # background Docker pulls
  make purge     # when you leave: images, sgoinfre data, caches (./.env is kept)
EOF
