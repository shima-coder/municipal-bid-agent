#!/usr/bin/env bash
# launchd ジョブのインストール / アンインストール (macOS)
#
# 使い方:
#   ./scripts/install_launchd.sh install [HH:MM]   # デフォルト 08:00
#   ./scripts/install_launchd.sh uninstall
#   ./scripts/install_launchd.sh status
#   ./scripts/install_launchd.sh run-now           # 手動でいま1回実行

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
LABEL="com.user.municipal-bid-agent"
TEMPLATE="${PROJECT_DIR}/cron/${LABEL}.plist.template"
PLIST_PATH="${HOME}/Library/LaunchAgents/${LABEL}.plist"

cmd="${1:-}"

case "${cmd}" in
install)
    schedule="${2:-08:00}"
    if ! [[ "${schedule}" =~ ^([0-9]{1,2}):([0-9]{2})$ ]]; then
        echo "Error: schedule must be HH:MM (got '${schedule}')" >&2
        exit 1
    fi
    HH="${BASH_REMATCH[1]}"
    MM="${BASH_REMATCH[2]}"
    # 0埋め除去 (launchd は integer 期待だが10進で問題ない)
    HOUR=$((10#${HH}))
    MIN=$((10#${MM}))

    if [ ! -f "${TEMPLATE}" ]; then
        echo "Error: template not found at ${TEMPLATE}" >&2
        exit 1
    fi

    mkdir -p "${HOME}/Library/LaunchAgents"
    sed \
        -e "s|{{PROJECT_DIR}}|${PROJECT_DIR}|g" \
        -e "s|{{HOUR}}|${HOUR}|g" \
        -e "s|{{MINUTE}}|${MIN}|g" \
        "${TEMPLATE}" > "${PLIST_PATH}"

    chmod 644 "${PLIST_PATH}"
    chmod +x "${PROJECT_DIR}/scripts/run_daily.sh"

    # 既存 unload してから load (冪等)
    launchctl unload "${PLIST_PATH}" 2>/dev/null || true
    launchctl load "${PLIST_PATH}"

    echo "✅ Installed: ${PLIST_PATH}"
    echo "   schedule: 毎日 $(printf '%02d:%02d' "${HOUR}" "${MIN}")"
    echo "   logs:    ${PROJECT_DIR}/logs/daily-YYYY-MM-DD.log"
    echo "   launchd: ${PROJECT_DIR}/logs/launchd.{out,err}"
    ;;

uninstall)
    if [ -f "${PLIST_PATH}" ]; then
        launchctl unload "${PLIST_PATH}" 2>/dev/null || true
        rm -f "${PLIST_PATH}"
        echo "✅ Uninstalled: ${PLIST_PATH}"
    else
        echo "(not installed: ${PLIST_PATH})"
    fi
    ;;

status)
    if [ -f "${PLIST_PATH}" ]; then
        echo "Plist installed: ${PLIST_PATH}"
        launchctl list | grep -F "${LABEL}" || echo "(loaded list に見当たらない)"
    else
        echo "Not installed."
    fi
    ;;

run-now)
    echo "Running ${PROJECT_DIR}/scripts/run_daily.sh now..."
    exec "${PROJECT_DIR}/scripts/run_daily.sh"
    ;;

*)
    cat <<EOF
Usage: $0 {install [HH:MM] | uninstall | status | run-now}

Examples:
    $0 install            # 毎日 08:00 に実行
    $0 install 07:30      # 毎日 07:30 に実行
    $0 uninstall
    $0 status
    $0 run-now            # 1回手動実行 (テスト用)
EOF
    exit 1
    ;;
esac
