"""
cortex-py 决策点实测脚本
验证 7 个数据模型决策点里能用证据拍死的部分:
  - 点1: scope TEXT+LIKE vs ltree(性能)
  - 点4/6: 递归 CTE 图遍历性能 + 双向索引价值
用真实 Postgres,假数据,带计时。
"""
import time
import uuid
import psycopg2
from psycopg2.extras import RealDictCursor

URL = "postgresql://postgres:0prV2JrQ1uJSBHZ2@192.168.1.21:5432/postgres"

def run():
    conn = psycopg2.connect(URL)
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=RealDictCursor)

    print("=== 0. 扩展检查 ===")
    cur.execute("SELECT extname FROM pg_extension ORDER BY extname")
    exts = [r['extname'] for r in cur.fetchall()]
    print("已装:", exts)

    # 清理
    cur.execute("DROP SCHEMA IF EXISTS cortex_probe CASCADE; CREATE SCHEMA cortex_probe;")
    print("\n已建干净 schema cortex_probe")

    # ---------- 点1: scope TEXT+LIKE ----------
    print("\n=== 点1A: scope TEXT + LIKE (holistic 向上遍历) ===")
    cur.execute("""
    CREATE TABLE cortex_probe.events_like (
        event_id UUID PRIMARY KEY,
        scope TEXT NOT NULL,
        observed_at TIMESTAMPTZ NOT NULL,
        content TEXT
    );
    CREATE INDEX idx_like_scope ON cortex_probe.events_like (scope, observed_at DESC);
    """)

    # 生成 10 万条数据,多 scope 层级
    print("灌 10 万条 events(LIKE 方案)...")
    t0 = time.time()
    scopes = [
        "org:acme/dept:eng/team:platform/user:alice",
        "org:acme/dept:eng/team:platform/user:bob",
        "org:acme/dept:eng/team:data/user:carol",
        "org:acme/dept:sales/team:apac/user:dave",
        "org:acme/dept:sales/team:apac/user:eve",
        "org:beta/dept:eng/user:frank",
    ]
    from datetime import datetime, timedelta
    base = datetime(2026, 1, 1)
    rows = []
    for i in range(100000):
        sc = scopes[i % len(scopes)]
        eid = str(uuid.uuid4())
        ts = base + timedelta(seconds=i)
        rows.append((eid, sc, ts, f"event content {i}"))
    cur.executemany(
        "INSERT INTO cortex_probe.events_like VALUES (%s,%s,%s,%s)", rows
    )
    print(f"  灌入耗时 {time.time()-t0:.2f}s")

    # holistic: user:alice 向上查所有祖先 scope 的数据
    # 祖先:org:acme, org:acme/dept:eng, org:acme/dept:eng/team:platform, org:acme/dept:eng/team:platform/user:alice
    target = "org:acme/dept:eng/team:platform/user:alice"
    prefixes = []
    parts = target.split("/")
    for i in range(1, len(parts)+1):
        prefixes.append("/".join(parts[:i]))
    print(f"  holistic 祖先前缀({len(prefixes)}): {prefixes}")

    # 方案A1: IN (前缀列表)
    t0 = time.time()
    cur.execute(
        "SELECT count(*) FROM cortex_probe.events_like WHERE scope = ANY(%s)",
        (prefixes,)
    )
    cnt = cur.fetchone()['count']
    elapsed_a1 = time.time() - t0
    print(f"  A1 IN(前缀列表): count={cnt}, {elapsed_a1*1000:.2f}ms")

    # 方案A2: LIKE 'prefix%' (只查 alice 自己的子树,不是 holistic)
    t0 = time.time()
    cur.execute(
        "SELECT count(*) FROM cortex_probe.events_like WHERE scope LIKE %s",
        (target + "/%",)
    )
    cnt = cur.fetchone()['count']
    elapsed_a2 = time.time() - t0
    print(f"  A2 LIKE prefix%(descend 子树): count={cnt}, {elapsed_a2*1000:.2f}ms")

    # ---------- 点1: scope ltree ----------
    print("\n=== 点1B: scope ltree (holistic 向上遍历) ===")
    try:
        cur.execute("CREATE EXTENSION IF NOT EXISTS ltree WITH SCHEMA cortex_probe;")
        print("  ltree 扩展 OK")
    except Exception as e:
        print(f"  ltree 装不了: {e}")
        print("  >>> 结论: ltree 不可用,点1 = TEXT+LIKE,无需再比")
        ltree_ok = False
    else:
        ltree_ok = True

    if ltree_ok:
        # ltree 用点分隔,scope 路径的 '/' 换成 '.'
        # org:acme/dept:eng -> org_acme.dept_eng(冒号不合法,替换)
        def to_ltree(sc):
            return sc.replace("/", ".").replace(":", "_")

        cur.execute("""
        CREATE TABLE cortex_probe.events_ltree (
            event_id UUID PRIMARY KEY,
            scope ltree NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            content TEXT
        );
        CREATE INDEX idx_ltree_scope ON cortex_probe.events_ltree (scope, observed_at DESC)
            WHERE true;
        """)
        # ltree 需要 gist 索引才能用 <@ (祖先) 操作符
        cur.execute("CREATE INDEX idx_ltree_gist ON cortex_probe.events_ltree USING gist (scope);")

        print("  灌 10 万条 events(ltree 方案)...")
        t0 = time.time()
        rows2 = [(eid, to_ltree(sc), ts, c) for (eid, sc, ts, c) in rows]
        cur.executemany(
            "INSERT INTO cortex_probe.events_ltree VALUES (%s,%s,%s,%s)", rows2
        )
        print(f"  灌入耗时 {time.time()-t0:.2f}s")

        # holistic: 查 alice 和所有祖先 <@ ancestors
        # ltree 的 ancestors@> path 表示 path 是 ancestors 的后代
        target_lt = to_ltree(target)
        # ancestors 是 target 的所有前缀(ltree 不直接给,要自己算)
        # 但 ltree 的 <@ 操作:scope <@ target 表示 scope 是 target 或其子树
        # holistic 要的是 target 和它的祖先,用 >@ 不对
        # 正确:用 ltree 的 subltree 或 text2ltree 拼祖先
        ancestor_lts = [to_ltree(p) for p in prefixes]
        # ltree 没有 IN list of ltree 直接语法,但 = ANY(array) 可以
        t0 = time.time()
        cur.execute(
            "SELECT count(*) FROM cortex_probe.events_ltree WHERE scope = ANY(%s::ltree[])",
            (ancestor_lts,)
        )
        cnt = cur.fetchone()['count']
        elapsed_b1 = time.time() - t0
        print(f"  B1 = ANY(ltree[]): count={cnt}, {elapsed_b1*1000:.2f}ms")

        # 用 @>(祖先包含后代)反过来:查所有 <@ 任意祖先的
        # 单个祖先的 holistic:scope <@ 'org_acme'(包含所有 acme 下后代)——但这是 descend 不是 holistic
        # holistic 的本质是"我 + 我的祖先",ltree 没有原生"祖先列表"操作
        # 用 ltree2text 拆段也算个办法,但失去 ltree 优势
        print("  注: ltree 的强项是 descend(子树),holistic(祖先)仍需自己算前缀列表")

    # ---------- 点4/6: 递归 CTE 图遍历 ----------
    print("\n=== 点4/6: 递归 CTE 图遍历(模拟 facts 表)===")
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

    # 生成 1 万实体,5 万 facts(平均每个实体 5 条出边)
    print("灌 1 万实体 + 5 万 facts...")
    entity_ids = [str(uuid.uuid4()) for _ in range(10000)]
    t0 = time.time()
    fr = []
    import random
    random.seed(42)
    for i in range(50000):
        subj = random.choice(entity_ids)
        obj = random.choice(entity_ids)
        pred = random.choice(["works_at","owns","uses","reports_to","depends_on","has_status"])
        fid = str(uuid.uuid4())
        fr.append((fid, "org:acme/dept:eng", subj, pred, obj, None, None))
    cur.executemany(
        "INSERT INTO cortex_probe.facts VALUES (%s,%s,%s,%s,%s,%s,%s)", fr
    )
    print(f"  灌入耗时 {time.time()-t0:.2f}s")

    seed = entity_ids[0]
    print(f"\n  seed 实体: {seed}")

    # 2 跳 BFS
    sql2 = """
    WITH RECURSIVE gw AS (
        SELECT subject_id, object_entity_id AS node, predicate, 1 AS hop
        FROM cortex_probe.facts
        WHERE subject_id = %s AND scope = %s
          AND valid_to IS NULL AND recorded_to IS NULL
        UNION ALL
        SELECT f.subject_id, f.object_entity_id, f.predicate, gw.hop+1
        FROM cortex_probe.facts f
        JOIN gw ON f.subject_id = gw.node
        WHERE f.scope = %s AND f.valid_to IS NULL AND f.recorded_to IS NULL
          AND gw.hop < 2
    )
    SELECT count(DISTINCT node) FROM gw;
    """
    t0 = time.time()
    cur.execute(sql2, (seed, "org:acme/dept:eng", "org:acme/dept:eng"))
    cnt2 = cur.fetchone()['count']
    e2 = time.time() - t0
    print(f"  2 跳 BFS: {cnt2} 个可达节点, {e2*1000:.2f}ms")

    # 3 跳 BFS
    sql3 = sql2.replace("gw.hop < 2", "gw.hop < 3")
    t0 = time.time()
    cur.execute(sql3, (seed, "org:acme/dept:eng", "org:acme/dept:eng"))
    cnt3 = cur.fetchone()['count']
    e3 = time.time() - t0
    print(f"  3 跳 BFS: {cnt3} 个可达节点, {e3*1000:.2f}ms")

    # 点6: 验证无 object 索引时的性能差异(对比)
    print("\n  点6 验证: 删 object 索引后 3 跳性能(模拟只单向索引)")
    cur.execute("DROP INDEX cortex_probe.idx_facts_obj;")
    t0 = time.time()
    cur.execute(sql3, (seed, "org:acme/dept:eng", "org:acme/dept:eng"))
    cnt3b = cur.fetchone()['count']
    e3b = time.time() - t0
    print(f"  3 跳 BFS(无 obj 索引): {cnt3b} 节点, {e3b*1000:.2f}ms  (双向时 {e3*1000:.2f}ms)")

    # 清理
    cur.execute("DROP SCHEMA cortex_probe CASCADE;")
    print("\n=== 清理完成 ===")

    print("\n" + "="*60)
    print("结论汇总:")
    print(f"  点1 LIKE IN(前缀): {elapsed_a1*1000:.2f}ms (10万行)")
    if ltree_ok:
        print(f"  点1 ltree ANY(ltree[]): {elapsed_b1*1000:.2f}ms (10万行)")
    print(f"  点4/6 2跳BFS(5万facts): {e2*1000:.2f}ms")
    print(f"  点4/6 3跳BFS(5万facts): {e3*1000:.2f}ms (双向索引)")
    print(f"  点4/6 3跳BFS(5万facts): {e3b*1000:.2f}ms (仅单向索引)")
    print("="*60)

    conn.close()

if __name__ == "__main__":
    run()
