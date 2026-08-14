#!/bin/bash
# Executable inside the Dock launcher bundle. Generated from
# scripts/macos/launcher.template.sh by scripts/macos/install_dock_app.sh —
# re-run the installer instead of editing the copy in ~/Applications.
#
# Click the Dock icon -> make sure the local web GUI is listening, then bring
# its window to the front (the Safari web app if one exists, plain Safari if not).
set -u

REPO="__REPO__"
UV="__UV__"
HOST="__HOST__"
PORT="__PORT__"
WEBAPP_NAME="__WEBAPP_NAME__"

URL="http://$HOST:$PORT/"
LOG_DIR="$HOME/Library/Logs/ibkr-tax"
LOG="$LOG_DIR/webapp.log"
MAX_LOG_BYTES=$((5 * 1024 * 1024))
WAIT_SECONDS=90

# One-line messages only — AppleScript strings take no \n escape.
notify() {
    osascript -e "display notification \"$1\" with title \"IBKR Tax Engine\"" \
        >/dev/null 2>&1 || true
}

# Notifications can be suppressed system-wide; failures use a modal alert so
# a broken start is never silent.
alert() {
    osascript -e "display alert \"IBKR Tax Engine\" message \"$1\" as critical" \
        >/dev/null 2>&1 || true
}

# Cheap readiness probe — /healthz skips rendering the dashboard. Silent: the
# polling loop below expects failures, and only the exit code matters.
ready() {
    curl -fs -m 2 -o /dev/null "http://$HOST:$PORT/healthz"
}

port_taken() {
    nc -z -G 1 "$HOST" "$PORT" >/dev/null 2>&1
}

start_server() {
    mkdir -p "$LOG_DIR" || return 1
    if [ -f "$LOG" ] && [ "$(stat -f%z "$LOG" 2>/dev/null || echo 0)" -gt "$MAX_LOG_BYTES" ]; then
        mv -f "$LOG" "$LOG.1" 2>/dev/null || true
    fi
    printf '\n=== %s launcher start ===\n' "$(date '+%Y-%m-%d %H:%M:%S')" >>"$LOG"
    cd "$REPO" || return 1
    nohup "$UV" run --extra web python -m src.webapp \
        --no-browser --host "$HOST" --port "$PORT" >>"$LOG" 2>&1 &
    return 0
}

# Safari names the bundle from whatever the "Add to Dock" dialog was left at,
# so fall back to scanning the Safari web apps in ~/Applications for one that
# points at our port. Their bundles are tiny, and non-web-apps are skipped
# before any grep, so this stays fast even next to a multi-gigabyte app.
find_webapp() {
    local name app bundle_id
    for name in "$WEBAPP_NAME" "IBKR Tax Engine" "Tax Engine"; do
        [ -n "$name" ] || continue
        if [ -d "$HOME/Applications/$name.app" ]; then
            printf '%s' "$HOME/Applications/$name.app"
            return 0
        fi
    done
    for app in "$HOME"/Applications/*.app; do
        [ -d "$app" ] || continue
        bundle_id="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' \
            "$app/Contents/Info.plist" 2>/dev/null)" || continue
        case "$bundle_id" in
            com.apple.Safari.WebApp*) ;;
            *) continue ;;
        esac
        if grep -rqs "$HOST:$PORT" "$app/Contents" 2>/dev/null; then
            printf '%s' "$app"
            return 0
        fi
    done
    return 1
}

open_window() {
    local app
    # Escape hatch for checking the server side without a window popping up.
    if [ "${IBKR_TAX_LAUNCHER_NO_OPEN:-0}" = "1" ]; then
        echo "launcher: $URL je připravené (okno se neotevírá)"
        return 0
    fi
    if app="$(find_webapp)"; then
        open "$app"
    else
        open -a Safari "$URL"
    fi
}

if ready; then
    open_window
    exit 0
fi

if port_taken; then
    alert "Port $PORT drží jiný proces, který neodpovídá. Zastav ho a zkus to znovu."
    exit 1
fi

notify "Spouštím server…"
if ! start_server; then
    alert "Server se nepodařilo spustit. Log: ~/Library/Logs/ibkr-tax/webapp.log"
    exit 1
fi

# First run after a dependency change spends this time in uv, not uvicorn.
for _ in $(seq 1 $((WAIT_SECONDS * 2))); do
    if ready; then
        open_window
        exit 0
    fi
    sleep 0.5
done

alert "Server nenaběhl do $WAIT_SECONDS s. Log: ~/Library/Logs/ibkr-tax/webapp.log"
exit 1
