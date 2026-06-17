#!/usr/bin/env bash
# 阶段 0 冒烟一键执行(入口;实际逻辑在 run_all.py,无 psql 依赖)。
# 用法:
#   ./run_all.sh                 # 默认连接串(或 DATABASE_URL 环境变量)
#   DATABASE_URL="postgresql://..." ./run_all.sh
#   ./run_all.sh --only 05_entity_resolution.sql   # 调试单文件
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$DIR/run_all.py" "$@"
