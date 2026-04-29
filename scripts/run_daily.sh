#!/usr/bin/env bash
# 日次実行ラッパー: launchd / cron から呼ばれる想定。
# 1. プロジェクトディレクトリに移動
# 2. .env があれば環境変数を読み込み (ANTHROPIC_API_KEY 等)
# 3. venv があれば activate
# 4. python main.py を実行
# 5. ログを logs/daily-YYYY-MM-DD.log に追記

set -u

# プロジェクトルート (このスクリプトの2階層上)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${PROJECT_DIR}"

# .env 読み込み (任意)
if [ -f "${PROJECT_DIR}/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "${PROJECT_DIR}/.env"
    set +a
fi

# venv activate (任意)
if [ -f "${PROJECT_DIR}/.venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "${PROJECT_DIR}/.venv/bin/activate"
fi

mkdir -p "${PROJECT_DIR}/logs"
LOG_FILE="${PROJECT_DIR}/logs/daily-$(date +%Y-%m-%d).log"

{
    echo "===== $(date '+%Y-%m-%d %H:%M:%S') 開始 ====="
    python main.py
    EXIT_CODE=$?
    echo "===== $(date '+%Y-%m-%d %H:%M:%S') 終了 (exit=${EXIT_CODE}) ====="
} >> "${LOG_FILE}" 2>&1

exit ${EXIT_CODE:-0}
