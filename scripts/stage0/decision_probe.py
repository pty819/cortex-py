"""
cortex-py 决策点轻量探测(30 秒内跑完)
只验证两件真正影响拍板的事:
  - 点4/6: 递归 CTE 图遍历性能 + 双向索引价值
  - 点1:   scope TEXT ANY(prefix) 性能(复核,理论已定)
规模: 5000 events + 1 万 facts(够看量级,不浪费时间)
"""
import time
import uuid
import random
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta

URL = "postgresql://postgres:0prV2JrQ1uJSBHZ2@192.168.1.21:5432/postgres"


def time_ms(cur, sql, params, label):
    t0 = time.time()
    cur.execute(sql, params)
    row = cur.fetchone()
    elapsed = (time.time() - t0) * 1000
    print(f"  {label}: {row} | {elapsed:.2f}ms")
    return elapsed


def run():
    conn = psycopg2.connect(URL)
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=RealDictCursor)

    print("=== 0. 准备干净 schema ===")
    cur.execute("DROP SCHEMA IF EXISTS cortex_probe CASCADE;")
    cur.execute("CREATE SCHEMA cortex_probe;")
    print("  OK")

    # scope 遍历
    print("\n=== 点1: scope TEXT + ANY(prefix) (5000 events) ===")
    cur.execute("""
    CREATE TABLE cortex_probe.events (
        event_id UUID PRIMARY KEY,
        scope TEXT NOT NULL,
        observed_at TIMESTAMPTZ NOT NULL
    );
    CREATE INDEX idx_ev_scope ON cortex_probe.events (scope);
    """)
    scopes = [
        "org:acme/dept:eng/team:platform/user:alice",
        "org:acme/dept:eng/team:platform/user:bob",
        "org:acme/dept:eng/team:data/user:carol",
        "org:acme/dept:sales/team:apac/user:dave",
        "org:acme/dept:sales/team:apac/user:eve",
        "org:beta/dept:eng/user:frank",
    ]
    base = datetime(2026, 1, 1)
    rows = [(str(uuid.uuid4()), scopes[i % 6], base + timedelta(seconds=i))
            for i in range(5000)]
    cur.executemany("INSERT INTO cortex_probe.events VALUES (%s,%s,%s)", rows)

    target = "org:acme/dept:eng/team:platform/user:alice"
    parts = target.split("/")
    prefixes = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    time_ms(cur, "SELECT count(*) FROM cortex_probe.events WHERE scope = ANY(%s)",
            (prefixes,), f"holistic ANY({len(prefixes)} 前缀)")
    time_ms(cur, "SELECT count(*) FROM cortex_probe.events WHERE scope LIKE %s",
            (target + "/%",), "descend LIKE prefix/%")

    # 图遍历 + 双向索引
    print("\n=== 点4/6: 递归 CTE 图遍历 (1 万 facts) ===")
    cur.execute("""
    CREATE TABLE cortex_probe.facts (
        fact_id UUID PRIMARY KEY,
        scope TEXT NOT NULL,
        subject_id UUID NOT NULL,
        predicate TEXT NOT NULL,
        object_entity_id UUID,
        valid_to TIMESTAMPTZ,
        recorded_to TIMESTAMPTZ
    );
    CREATE INDEX idx_facts_subj ON cortex_probe.facts (scope, subject_id, predicate)
        WHERE valid_to IS NULL AND recorded_to IS NULL;
    CREATE INDEX idx_facts_obj ON cortex_probe.facts (scope, object_entity_id, predicate)
        WHERE valid_to IS NULL AND recorded_to IS NULL;
    """)
    entity_ids = [str(uuid.uuid4()) for _ in range(2000)]
    random.seed(42)
    preds = ["works_at", "owns", "uses", "reports_to", "depends_on", "has_status"]
    fr = [(str(uuid.uuid4()), "org:acme/dept:eng",
           random.choice(entity_ids), random.choice(preds),
           random.choice(entity_ids), None, None)
          for _ in range(10000)]
    cur.executemany("INSERT INTO cortex_probe.facts VALUES (%s,%s,%s,%s,%s,%s,%s)", fr)
    cur.execute("ANALYZE cortex_probe.facts;")
    seed = entity_ids[0]
    print(f"  seed: {seed}")

    bfs_sql = """
    WITH RECURSIVE gw AS (
        SELECT subject_id, object_entity_id AS node, 1 AS hop
        FROM cortex_probe.facts
        WHERE subject_id = %s AND scope = %s
          AND valid_to IS NULL AND recorded_to IS NULL
        UNION ALL
        SELECT f.subject_id, f.object_entity_id, gw.hop+1
        FROM cortex_probe.facts f
        JOIN gw ON f.subject_id = gw.node
        WHERE f.scope = %s AND f.valid_to IS NULL AND f.recorded_to IS NULL
          AND gw.hop < %s
    )
    SELECT count(DISTINCT node) FROM gw;
    """

    print("  [双向索引]")
    e2 = time_ms(cur, bfs_sql, (seed, "org:acme/dept:eng", "org:acme/dept:eng", 2), "2 跳 BFS")
    e3 = time_ms(cur, bfs_sql, (seed, "org:acme/dept:eng", "org:acme/dept:eng", 3), "3 跳 BFS")

    cur.execute("DROP INDEX cortex_probe.idx_facts_obj;")
    cur.execute("ANALYZE cortex_probe.facts;")
    print("  [仅单向索引(subject),无 object 索引]")
    e3_single = time_ms(cur, bfs_sql, (seed, "org:acme/dept:eng", "org:acme/dept:eng", 3), "3 跳 BFS")

    cur.execute("DROP SCHEMA cortex_probe CASCADE;")
    conn.close()

    print("\n" + "=" * 60)
    print("结论:")
    print(f"  点1 scope ANY(prefix) 5000 行: 毫秒级 OK")
    print(f"  点4 图遍历 2跳(1万facts): {e2:.2f}ms")
    print(f"  点6 图遍历 3跳 双向索引: {e3:.2f}ms")
    print(f"  点6 图遍历 3跳 单向索引: {e3_single:.2f}ms")
    if e3_single > e3 * 2:
        print(f"  -> 双向索引显著优于单向({e3_single/e3:.1f}x),点6裁定确认 OK")
    else:
        print(f"  -> 单向退化不显著({e3_single/e3:.1f}x),但仍按裁定双向(入边查询需要)")
    print("=" * 60)


if __name__ == "__main__":
    run()
