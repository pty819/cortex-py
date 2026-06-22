"""队列 worker:循环抢 job → 按 job_type 分发 → 成功/失败处理 + reaper。"""
from __future__ import annotations

import logging
import time
import traceback

from sqlalchemy import text

from ..config import load_config
from ..core import claim_next_job, complete_job, fail_job, reap_zombies, emit_lifecycle
from ..db import session_scope
from ..extraction.pipeline import ExtractionConfigurationError

log = logging.getLogger("cortex.worker")


def _handle_job_failure(conn, job: dict, error: Exception, backoff_base: int) -> None:
    terminal = isinstance(error, ExtractionConfigurationError)
    error_kind = "config_error" if terminal else "processing_error"
    fail_job(conn, job["job_id"], str(error), backoff_base=backoff_base,
             terminal=terminal, error_kind=error_kind)
    if terminal:
        if job.get("event_id"):
            conn.execute(text("UPDATE events SET embed_status='failed' WHERE event_id=CAST(:e AS uuid)"),
                         {"e": job["event_id"]})
        emit_lifecycle(conn, kind="failed", scope=job["scope"],
                       event_id=job.get("event_id"), job_id=job["job_id"],
                       payload={"error_kind": error_kind, "error": str(error)[:500]})


def _dispatch(job: dict) -> dict:
    """按 job_type 跑对应 handler。返回 result dict。"""
    jt = job["job_type"]
    scope = job.get("scope")
    if jt == "extract" and job.get("event_id"):
        from ..extraction.pipeline import extract_event
        return extract_event(job["event_id"])
    if jt == "segment" and scope:
        from ..episodes import segment_scope
        return segment_scope(scope)
    if jt == "methylation" and scope:
        from ..maintenance import methylation_run
        older = (job.get("payload") or {}).get("older_than_days", 30)
        return methylation_run(scope, older_than_days=older)
    if jt == "consolidate" and scope:
        from ..maintenance import consolidation_run
        return consolidation_run(scope)
    if jt == "enrich" and scope:
        # 异步 KG 增强:跨 event 批量实体消歧(MVP:复用 extraction 的链接,扫无 embedding 实体补算)
        from sqlalchemy import text as _t
        from ..db import session_scope as _ss
        from .. import services
        from ..config import load_config as _lc
        n = 0
        with _ss() as conn:
            rows = conn.execute(_t("SELECT entity_id::text, canonical_name, description FROM entities WHERE embedding IS NULL AND merged_into IS NULL AND scope=:s LIMIT 100"), {"s": scope}).fetchall()
            for r in rows:
                try:
                    emb = services.embed_one(_lc().extraction.embedding_text.format(name=r[1], description=r[2] or r[1]))
                    conn.execute(_t("UPDATE entities SET embedding=CAST(:e AS vector) WHERE entity_id=CAST(:id AS uuid)"),
                                 {"e": str(emb), "id": r[0]})
                    n += 1
                except Exception:
                    pass
        return {"enriched": n}
    if jt == "synthesize" and scope:
        from ..understanding import synthesize_scope
        payload = job.get("payload") or {}
        return synthesize_scope(scope, topics=payload.get("topics"))
    return {"ok": True, "note": f"no handler for {jt}"}


def run_worker(*, max_iterations: int = 0) -> None:
    """阻塞跑 worker。max_iterations=0 表示无限。Ctrl-C 退出。"""
    cfg = load_config()
    worker_id = f"worker-{int(time.time()) % 100000}"
    poll = cfg.worker.poll_interval_secs
    vis = cfg.worker.visibility_timeout_secs
    last_reap = 0.0
    it = 0
    log.info("worker %s started (poll=%.2fs vis=%ss)", worker_id, poll, vis)
    while max_iterations == 0 or it < max_iterations:
        it += 1
        try:
            with session_scope() as conn:
                job = claim_next_job(conn, worker_id)
                if not job:
                    conn.execute(text("SELECT 1"))  # keep tx valid
            if not job:
                now = time.time()
                if now - last_reap > cfg.worker.reaper_interval_secs:
                    with session_scope() as conn:
                        n = reap_zombies(conn, vis)
                        if n:
                            log.info("reaper reset %d zombie jobs", n)
                    last_reap = now
                time.sleep(poll)
                continue
            log.info("[%s] claimed %s type=%s", worker_id, job["job_id"], job["job_type"])
            try:
                result = _dispatch(job)
                with session_scope() as conn:
                    complete_job(conn, job["job_id"], result)
                    emit_lifecycle(conn, kind="indexed", scope=job["scope"],
                                   event_id=job.get("event_id"), job_id=job["job_id"],
                                   payload={"result": result})
                log.info("[%s] completed %s", worker_id, job["job_id"])
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] failed %s: %s\n%s", worker_id, job["job_id"], e, traceback.format_exc())
                with session_scope() as conn:
                    _handle_job_failure(conn, job, e, cfg.worker.backoff_base_secs)
        except KeyboardInterrupt:
            log.info("worker %s stopping", worker_id)
            return
        except Exception as e:  # noqa: BLE001  外层异常不应致命
            log.error("worker loop error: %s", e)
            time.sleep(poll)
