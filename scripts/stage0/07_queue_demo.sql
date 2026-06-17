-- 07_queue_demo.sql — Postgres-as-queue:SKIP LOCKED + priority + visibility timeout + 重试/死信
-- 依据:03 §7 + 决策 7(priority DESC, run_after, created_at + SKIP LOCKED + max_attempts=3 + 5min visibility)

SET search_path = cortex_stage0, public;
\set scopeA 'org:acme/dept:sales/user:alice'

-- 灌 6 个 job(混合 priority),全部 queued ─────────────────────────────────
INSERT INTO jobs (job_id, job_type, scope, status, priority, run_after) VALUES
('66666666-0000-0000-0000-000000000001', 'extract', :'scopeA', 'queued', 10, now()),
('66666666-0000-0000-0000-000000000002', 'extract', :'scopeA', 'queued',  5, now()),
('66666666-0000-0000-0000-000000000003', 'extract', :'scopeA', 'queued',  5, now()),
('66666666-0000-0000-0000-000000000004', 'segment', :'scopeA', 'queued',  1, now()),
('66666666-0000-0000-0000-000000000005', 'extract', :'scopeA', 'queued',  0, now() + interval '1 hour'),  -- 未到期(run_after 未来)
('66666666-0000-0000-0000-000000000006', 'extract', :'scopeA', 'queued', 99, now() - interval '10 min');  -- 最高优先级,最早创建

-- 原子抢任务(worker pattern)
CREATE OR REPLACE FUNCTION claim_next_job(p_worker text)
RETURNS TABLE(job_id uuid, job_type text, priority int) LANGUAGE sql AS $$
  UPDATE jobs SET
      status='running', locked_by=p_worker, locked_at=now(), started_at=now(), attempts=attempts+1
   WHERE job_id = (
      SELECT job_id FROM jobs
       WHERE status='queued' AND run_after <= now()
       ORDER BY priority DESC, run_after, created_at
       FOR UPDATE SKIP LOCKED LIMIT 1
   )
   RETURNING jobs.job_id, jobs.job_type, jobs.priority
$$;

-- ── (1) 两个 worker 顺序抢:各抢不同 job,不重复 ───────────────────────────
DO $$
DECLARE w1 uuid; w2 uuid;
BEGIN
  SELECT job_id INTO w1 FROM claim_next_job('worker-1');
  SELECT job_id INTO w2 FROM claim_next_job('worker-2');
  IF w1 IS NOT NULL AND w2 IS NOT NULL AND w1 <> w2 THEN
    RAISE NOTICE 'PASS: 2 workers each grabbed a distinct job (SKIP LOCKED prevents duplicate)';
  ELSE RAISE NOTICE 'FAIL: w1=% w2=% (must differ)', w1, w2; END IF;
END $$;

-- ── (2) priority 顺序:第一个抢到的应是最高优先级(job6 priority=99)─────────
DO $$
DECLARE first uuid;
BEGIN
  -- 先把前两个测试 worker 抢的重置,干净重测
  UPDATE jobs SET status='queued', locked_by=NULL, locked_at=NULL WHERE status='running';
  SELECT job_id INTO first FROM claim_next_job('worker-priority');
  IF first = '66666666-0000-0000-0000-000000000006' THEN
    RAISE NOTICE 'PASS: highest-priority job (priority=99) grabbed first';
  ELSE RAISE NOTICE 'FAIL: first grabbed=% (want job6 p=99)', first; END IF;
END $$;

-- ── (3) run_after 未到期的 job 不被抢 ───────────────────────────────────────
DO $$
DECLARE grabbed uuid; pending int;
BEGIN
  UPDATE jobs SET status='queued', locked_by=NULL, locked_at=NULL WHERE status='running';
  -- 抢光所有到期 job
  PERFORM claim_next_job('drain') FROM generate_series(1,10) WHERE EXISTS (SELECT 1 FROM claim_next_job('drain'));
  SELECT count(*) INTO pending FROM jobs WHERE job_id='66666666-0000-0000-0000-000000000005' AND status='queued';
  IF pending = 1 THEN RAISE NOTICE 'PASS: not-yet-due job (run_after +1h) stays queued';
  ELSE RAISE NOTICE 'FAIL: future-due job status not queued (pending=%)', pending; END IF;
END $$;

-- ── (4) visibility timeout:running 且 locked_at 超 5min → 重置为 queued ─────
DO $$
DECLARE zombied uuid;
BEGIN
  -- 造一个僵尸:5 分钟前 locked
  UPDATE jobs SET status='running', locked_by='crashed-worker', locked_at=now()-interval '6 min'
   WHERE job_id='66666666-0000-0000-0000-000000000004';
  -- reaper 扫描
  UPDATE jobs SET status='queued', locked_by=NULL, locked_at=NULL
   WHERE status='running' AND locked_at < now() - interval '5 min';
  SELECT job_id INTO zombied FROM jobs WHERE job_id='66666666-0000-0000-0000-000000000004' AND status='queued';
  IF zombied IS NOT NULL THEN RAISE NOTICE 'PASS: visibility timeout reaper reset crashed job → queued';
  ELSE RAISE NOTICE 'FAIL: zombie job not reset'; END IF;
END $$;

-- ── (5) 重试上限 → 死信(failed)──────────────────────────────────────────
DO $$
DECLARE dead uuid;
BEGIN
  INSERT INTO jobs (job_id, job_type, scope, status, attempts, max_attempts) VALUES
  ('66666666-0000-0000-0000-000000000099', 'extract', :'scopeA', 'running', 3, 3);
  -- 模拟 worker 失败:attempts 已达上限 → 标 failed
  UPDATE jobs SET status='failed', error='max attempts exceeded'
   WHERE job_id='66666666-0000-0000-0000-000000000099' AND attempts >= max_attempts;
  SELECT job_id INTO dead FROM jobs WHERE job_id='66666666-0000-0000-0000-000000000099' AND status='failed';
  IF dead IS NOT NULL THEN RAISE NOTICE 'PASS: job at max_attempts → dead-letter (failed) with error';
  ELSE RAISE NOTICE 'FAIL: dead-letter job not marked failed'; END IF;
END $$;
