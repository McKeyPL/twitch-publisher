#!/usr/bin/env bash
set -uo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="config.yaml"
ONCE=false
BROWSER_DEBUG=false
RESTART_DELAY_SECONDS=10
CHILD_PID=""

color() {
    local code="$1"
    shift
    if [[ -t 1 ]]; then
        printf '\033[%sm%s\033[0m\n' "$code" "$*"
    else
        printf '%s\n' "$*"
    fi
}

usage() {
    cat <<'EOF'
Usage: ./start.sh [options]

Options:
  --config PATH             Configuration file (default: config.yaml)
  --once                    Run one watcher cycle without restarting
  --browser-debug           Show Playwright and capture diagnostics
  --restart-delay SECONDS   Delay after an unexpected failure (default: 10)
  -h, --help                Show this help
EOF
}

while (($#)); do
    case "$1" in
        --config)
            [[ $# -ge 2 ]] || { color 31 "[ERROR] --config requires a path."; exit 2; }
            CONFIG="$2"
            shift 2
            ;;
        --once)
            ONCE=true
            shift
            ;;
        --browser-debug)
            BROWSER_DEBUG=true
            shift
            ;;
        --restart-delay)
            [[ $# -ge 2 && "$2" =~ ^[1-9][0-9]*$ ]] || {
                color 31 "[ERROR] --restart-delay requires a positive integer."
                exit 2
            }
            RESTART_DELAY_SECONDS="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            color 31 "[ERROR] Unknown option: $1"
            usage
            exit 2
            ;;
    esac
done

cd "$PROJECT_DIR"

if [[ ! -x .venv/bin/python ]]; then
    color 31 "[ERROR] The .venv environment does not exist."
    color 33 "Run ./install.sh first."
    exit 2
fi

if [[ ! -f .env ]]; then
    color 33 "[WARNING] .env is missing; configuration may require environment variables."
fi

mkdir -p logs
LAUNCHER_LOG="logs/start_sh.log"
RESTART_COUNT=0

launcher_log() {
    local line
    line="$(date '+%Y-%m-%d %H:%M:%S') $*"
    printf '%s\n' "$line" >>"$LAUNCHER_LOG"
    color 36 "$line"
}

stop_child() {
    local signal_name="$1"
    launcher_log "Received $signal_name; stopping the active publisher process."
    if [[ -n "$CHILD_PID" ]] && kill -0 "$CHILD_PID" 2>/dev/null; then
        kill -INT "$CHILD_PID" 2>/dev/null || true
        wait "$CHILD_PID" 2>/dev/null || true
    fi
    exit 130
}

trap 'stop_child SIGINT' INT
trap 'stop_child SIGTERM' TERM

while true; do
    launcher_log "Starting main.py (restart number $RESTART_COUNT)."
    python_args=(main.py --config "$CONFIG")
    $ONCE && python_args+=(--once)
    $BROWSER_DEBUG && python_args+=(--browser-debug)

    .venv/bin/python "${python_args[@]}" &
    CHILD_PID=$!
    wait "$CHILD_PID"
    EXIT_CODE=$?
    CHILD_PID=""

    if ((EXIT_CODE == 0)); then
        launcher_log "main.py exited successfully."
        exit 0
    fi

    if $ONCE; then
        launcher_log "main.py exited with code $EXIT_CODE; Once mode will not restart."
        exit "$EXIT_CODE"
    fi

    ((RESTART_COUNT += 1))
    launcher_log "main.py failed with code $EXIT_CODE. Restart $RESTART_COUNT in $RESTART_DELAY_SECONDS s."
    color 33 "[WARNING] Restarting process..."
    sleep "$RESTART_DELAY_SECONDS"
done
