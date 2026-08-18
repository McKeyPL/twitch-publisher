#!/usr/bin/env bash
set -uo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="config.yaml"
ONCE=false
DRY_RUN=false
BROWSER_DEBUG=false
LOGIN=false
RESTART_DELAY=10
VIDEO_IDS=()
RESET_VIDEO_IDS=()
CHILD_PID=""

usage() {
    cat <<'EOF'
Usage: ./start-copyright-guard.sh [options]
  --config PATH
  --once
  --dry-run
  --browser-debug
  --login
  --video-id ID             May be repeated
  --reset-video ID           Cancel UNCERTAIN actions; may be repeated
  --restart-delay SECONDS
EOF
}

while (($#)); do
    case "$1" in
        --config) CONFIG="$2"; shift 2 ;;
        --once) ONCE=true; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        --browser-debug) BROWSER_DEBUG=true; shift ;;
        --login) LOGIN=true; shift ;;
        --video-id) VIDEO_IDS+=("$2"); shift 2 ;;
        --reset-video) RESET_VIDEO_IDS+=("$2"); shift 2 ;;
        --restart-delay) RESTART_DELAY="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) printf '[ERROR] Unknown option: %s\n' "$1" >&2; usage; exit 2 ;;
    esac
done

cd "$PROJECT_DIR"
if [[ ! -x .venv/bin/python ]]; then
    printf '[ERROR] The .venv environment does not exist. Run ./install.sh first.\n' >&2
    exit 2
fi
mkdir -p logs

stop_child() {
    if [[ -n "$CHILD_PID" ]] && kill -0 "$CHILD_PID" 2>/dev/null; then
        kill -INT "$CHILD_PID" 2>/dev/null || true
        wait "$CHILD_PID" 2>/dev/null || true
    fi
    exit 130
}
trap stop_child INT TERM

restart_count=0
while true; do
    printf '%s Starting copyright_guard.py (restart number %d).\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" "$restart_count" | tee -a logs/start_copyright_guard_sh.log
    args=(copyright_guard.py --config "$CONFIG")
    $ONCE && args+=(--once)
    $DRY_RUN && args+=(--dry-run)
    $BROWSER_DEBUG && args+=(--browser-debug)
    $LOGIN && args+=(--login)
    for id in "${VIDEO_IDS[@]}"; do args+=(--video-id "$id"); done
    for id in "${RESET_VIDEO_IDS[@]}"; do args+=(--reset-video "$id"); done

    .venv/bin/python "${args[@]}" &
    CHILD_PID=$!
    wait "$CHILD_PID"
    exit_code=$?
    CHILD_PID=""
    if ((exit_code == 130)); then exit 130; fi
    if ((exit_code == 0)); then exit 0; fi
    if $ONCE || $LOGIN || ((${#RESET_VIDEO_IDS[@]})); then exit "$exit_code"; fi
    ((restart_count += 1))
    sleep "$RESTART_DELAY"
done
