#!/usr/bin/env python3
"""
cortex-py 阶段 0 冒烟套件执行器(无 psql 依赖,走 psycopg2)。

职责:
  1. 重置 schema(DROP SCHEMA cortex_stage0 CASCADE)
  2. 按序执行 00–09 .sql
  3. psql 变量预处理:\set name value  →  :'name'(带引号字面量) / :name(裸替换)
  4. 捕获每文件的 RAISE NOTICE(PASS/FAIL/PERF/SCHEMA/SEED)
  5. 汇总 PASS/FAIL,写 stage0_report.txt

用法:
  python3 run_all.py                 # 用默认连接串(或 DATABASE_URL 环境变量)
  python3 run_all.py --url "..."     # 指定连接串
  python3 run_all.py --no-drop       # 跳过 DROP(调试用)
"""
import os
import re
import sys
import argparse
from datetime import datetime

import psycopg2

DEFAULT_URL = "postgresql://postgres:0prV2JrQ1uJSBHZ2@192.168.1.21:5432/postgres"
HERE = os.path.dirname(os.path.abspath(__file__))
FILES = [
    "00_extensions.sql", "01_schema.sql", "02_seed_data.sql",
    "03_temporal_tests.sql", "04_graph_traversal.sql", "05_entity_resolution.sql",
    "06_scope_isolation.sql", "07_queue_demo.sql", "08_blobs_vocab.sql",
    "09_erasure_refcount.sql",
]
SCHEMA = "cortex_stage0"

# ── psql 变量预处理 ──────────────────────────────────────────────────────────
_SET_RE = re.compile(r"^\s*\\set\s+(\w+)\s+(.*?)\s*$")
# 单趟组合:先匹配带引号 :'name',再匹配裸 :name。re.sub 不回扫替换后的文本,
# 因此插入字面量 'org:acme/...' 里的 :acme 不会被二次替换。
_VAR_RE = re.compile(r":'(\w+)'|:(\w+)")


def preprocess(sql: str):
    """剥离 \\set 行,建变量表;单趟替换 :'name'(带引号字面量) 与 :name(裸)。返回处理后的 SQL。"""
    vars_ = {}
    out_lines = []
    for line in sql.splitlines():
        m = _SET_RE.match(line)
        if m:
            name, val = m.group(1), m.group(2)
            if len(val) >= 2 and val[0] == "'" and val[-1] == "'":
                val = val[1:-1]
            vars_[name] = val
        else:
            out_lines.append(line)
    sql = "\n".join(out_lines)

    def sub(mo):
        if mo.group(1) is not None:          # 带引号形式 :'name' → 'value'
            name = mo.group(1)
            return "'" + vars_[name] + "'" if name in vars_ else mo.group(0)
        name = mo.group(2)                    # 裸形式 :name → value
        return vars_[name] if name in vars_ else mo.group(0)

    return _VAR_RE.sub(sub, sql)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("DATABASE_URL", DEFAULT_URL))
    ap.add_argument("--no-drop", action="store_true", help="跳过 schema 重置")
    ap.add_argument("--only", help="只跑某个文件名(逗号分隔),用于调试")
    args = ap.parse_args()

    files = [f for f in FILES if not args.only or f in args.only.split(",")]

    report = []
    total_pass, total_fail = 0, 0
    errors = []

    conn = psycopg2.connect(args.url)
    conn.autocommit = False
    cur = conn.cursor()

    def run_one(fname):
        nonlocal total_pass, total_fail
        path = os.path.join(HERE, fname)
        report.append(f"\n{'='*78}\n>>> {fname}\n{'='*78}")
        if conn.notices:
            conn.notices.clear()
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
        sql = preprocess(raw)
        try:
            cur.execute(sql)
            conn.commit()
        except Exception as e:
            conn.rollback()
            report.append(f"  ERROR: {e}")
            errors.append((fname, str(e)))
            return
        notices = list(conn.notices)
        for n in notices:
            # n 形如 "NOTICE:  PASS: ...\n"
            line = n.strip()
            if line.startswith("NOTICE:"):
                line = line[len("NOTICE:"):].strip()
            report.append(f"  {line}")
            if line.startswith("PASS"):
                total_pass += 1
            elif line.startswith("FAIL"):
                total_fail += 1
        if conn.notices:
            conn.notices.clear()

    # ── 重置 ───────────────────────────────────────────────────────────────
    if not args.no_drop:
        report.append(f">>> RESET: DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        conn.commit()

    # ── 按序执行 ───────────────────────────────────────────────────────────
    for f in files:
        run_one(f)

    cur.close()
    conn.close()

    # ── 汇总 ───────────────────────────────────────────────────────────────
    header = (
        f"cortex-py 阶段 0 冒烟报告\n"
        f"生成: {datetime.now().isoformat(timespec='seconds')}\n"
        f"DB: {args.url.split('@')[-1]}\n"
        f"结果: PASS={total_pass}  FAIL={total_fail}  FILE_ERRORS={len(errors)}"
    )
    verdict = "ALL GREEN ✅" if total_fail == 0 and not errors else "HAS FAILURES ❌"
    body = "\n".join(report)
    summary = f"\n{'='*78}\n>>> 判定: {verdict}\n{'='*78}"
    if errors:
        summary += "\n文件级错误:\n" + "\n".join(f"  - {f}: {e}" for f, e in errors)

    full = f"{header}\n{summary}\n{body}\n"
    report_path = os.path.join(HERE, "stage0_report.txt")
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(full)

    print(header)
    print(verdict)
    if errors:
        for f, e in errors:
            print(f"  ERR {f}: {e}")
    print(f"\n完整报告: {report_path}")
    sys.exit(0 if total_fail == 0 and not errors else 1)


if __name__ == "__main__":
    main()
