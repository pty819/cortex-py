"""核心功能测试:WAL append 幂等 + 队列 + lifecycle。"""
import pytest
from cortex.core import append_event, enqueue_job, IdempotencyConflict
from cortex.db import session_scope
from sqlalchemy import text


def test_append_event_basic(test_scope):
    """event 入库,返回 event_id + wal_offset。"""
    eid, off = append_event(scope=test_scope, modality="conversation",
                            content={"kind": "message", "role": "user", "text": "test event"},
                            context={"observed_at": "2026-01-01T00:00:00Z"},
                            caller="test", idempotency_key="test-1")
    assert eid is not None
    assert off > 0


def test_append_event_idempotent(test_scope):
    """同 key + 同 body = 幂等(返回既有)。"""
    content = {"kind": "message", "role": "user", "text": "same"}
    ctx = {"observed_at": "2026-01-01T00:00:00Z"}
    eid1, _ = append_event(scope=test_scope, modality="conversation",
                           content=content, context=ctx,
                           caller="test", idempotency_key="idem-1")
    eid2, _ = append_event(scope=test_scope, modality="conversation",
                           content=content, context=ctx,
                           caller="test", idempotency_key="idem-1")
    assert eid1 == eid2


def test_append_event_conflict(test_scope):
    """同 key + 异 body = IdempotencyConflict。"""
    append_event(scope=test_scope, modality="conversation",
                 content={"kind": "message", "role": "user", "text": "body A"},
                 context={}, caller="test", idempotency_key="conflict-1")
    with pytest.raises(IdempotencyConflict):
        append_event(scope=test_scope, modality="conversation",
                     content={"kind": "message", "role": "user", "text": "body B"},
                     context={}, caller="test", idempotency_key="conflict-1")


def test_enqueue_job(test_scope):
    """job 入队,能查到 queued 状态。"""
    eid, _ = append_event(scope=test_scope, modality="conversation",
                          content={"kind": "message", "role": "user", "text": "for job"},
                          context={}, caller="test", idempotency_key="job-1")
    jid = enqueue_job(job_type="extract", scope=test_scope, event_id=eid)
    assert jid is not None
    with session_scope() as c:
        st = c.execute(text("SELECT status FROM jobs WHERE job_id=CAST(:j AS uuid)"),
                       {"j": jid}).scalar()
    assert st in ("queued", "running")  # 可能被后台 worker 抢走


def test_lifecycle_emitted(test_scope):
    """append_event 后 captured lifecycle 发出。"""
    eid, _ = append_event(scope=test_scope, modality="conversation",
                          content={"kind": "message", "role": "user", "text": "lc test"},
                          context={}, caller="test", idempotency_key="lc-1")
    with session_scope() as c:
        n = c.execute(text("SELECT count(*) FROM lifecycle_events WHERE event_id=CAST(:e AS uuid)"),
                      {"e": eid}).scalar()
    assert n >= 1
