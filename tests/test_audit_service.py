from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.models.audit_log import AuditAction
from app.services import audit_service


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_record_writes_entry_with_expected_fields():
    db = make_session()
    entry = audit_service.record(
        db,
        actor_email="alice@example.com",
        action=AuditAction.CREATE_USER,
        target_email="newhire@example.org",
        detail="作者版",
        success=True,
    )
    assert entry.id is not None
    assert entry.actor_email == "alice@example.com"
    assert entry.action == AuditAction.CREATE_USER
    assert entry.success is True


def test_record_captures_failure_with_error_message():
    db = make_session()
    audit_service.record(
        db,
        actor_email="bob@example.org",
        action=AuditAction.RESET_PASSWORD,
        target_email="someone@example.org",
        success=False,
        error_message="Keycloak API error 500",
    )
    entries = audit_service.list_recent(db)
    assert len(entries) == 1
    assert entries[0].success is False
    assert "500" in entries[0].error_message


def test_list_recent_orders_by_created_at_desc():
    db = make_session()
    for i in range(3):
        audit_service.record(
            db,
            actor_email="admin@example.com",
            action=AuditAction.DISABLE_USER,
            target_email=f"user{i}@example.com",
        )
    entries = audit_service.list_recent(db)
    assert [e.target_email for e in entries] == ["user2@example.com", "user1@example.com", "user0@example.com"]


def test_list_recent_respects_limit():
    db = make_session()
    for i in range(5):
        audit_service.record(
            db, actor_email="admin@example.com", action=AuditAction.ENABLE_USER, target_email=f"user{i}@example.com"
        )
    entries = audit_service.list_recent(db, limit=2)
    assert len(entries) == 2


# ---------- 过滤条件（对齐 kiro-fleet「操作日志支持按账号/操作类型/状态过滤」）----------


def _seed_mixed_entries(db):
    audit_service.record(
        db, actor_email="alice@example.com", action=AuditAction.CREATE_USER,
        target_email="a@example.org", success=True,
    )
    audit_service.record(
        db, actor_email="bob@example.org", action=AuditAction.DISABLE_USER,
        target_email="b@example.org", success=True,
    )
    audit_service.record(
        db, actor_email="alice@example.com", action=AuditAction.CHANGE_TIER,
        target_email="a@example.org", success=False, error_message="boom",
    )


def test_list_recent_filters_by_actor_email_substring():
    db = make_session()
    _seed_mixed_entries(db)
    entries = audit_service.list_recent(db, actor_email="alice")
    assert len(entries) == 2
    assert all(e.actor_email == "alice@example.com" for e in entries)


def test_list_recent_filters_by_target_email_substring():
    db = make_session()
    _seed_mixed_entries(db)
    entries = audit_service.list_recent(db, target_email="b@example.org")
    assert len(entries) == 1
    assert entries[0].action == AuditAction.DISABLE_USER


def test_list_recent_filters_by_action():
    db = make_session()
    _seed_mixed_entries(db)
    entries = audit_service.list_recent(db, action=AuditAction.CHANGE_TIER)
    assert len(entries) == 1
    assert entries[0].action == AuditAction.CHANGE_TIER


def test_list_recent_filters_by_success():
    db = make_session()
    _seed_mixed_entries(db)
    failed = audit_service.list_recent(db, success=False)
    assert len(failed) == 1
    assert failed[0].error_message == "boom"

    succeeded = audit_service.list_recent(db, success=True)
    assert len(succeeded) == 2


def test_list_recent_combines_multiple_filters():
    db = make_session()
    _seed_mixed_entries(db)
    entries = audit_service.list_recent(db, actor_email="alice", success=False)
    assert len(entries) == 1
    assert entries[0].action == AuditAction.CHANGE_TIER


def test_list_recent_without_filters_behaves_like_before():
    db = make_session()
    _seed_mixed_entries(db)
    entries = audit_service.list_recent(db)
    assert len(entries) == 3


# ---------- 写审计日志失败不能往上抛（对着真实 Keycloak 做批量场景端到端验证时
# 意外触发过：DB 写失败会打断批量路由的 for 循环，已经生效的 Keycloak 操作没
# 记上审计、后面排队的行完全不被处理）----------


def test_record_swallows_db_errors_and_returns_none():
    db = make_session()
    original_commit = db.commit
    calls = {"n": 0}

    def flaky_commit():
        calls["n"] += 1
        raise OperationalError("INSERT INTO audit_log ...", {}, Exception("database is locked"))

    db.commit = flaky_commit

    result = audit_service.record(
        db, actor_email="a@example.com", action=AuditAction.CREATE_USER, target_email="b@example.com",
    )

    assert result is None
    assert calls["n"] == 1
    db.commit = original_commit  # 还原，避免影响这条测试后面的断言


def test_record_rolls_back_after_failure_so_session_stays_usable():
    """第一次写失败之后，同一个 db session 不能被拖进"事务已废弃"的状态——
    调用方（路由层的 db: Session = Depends(get_db)）后面还要继续用这同一个
    session 处理下一行/下一次请求。"""
    db = make_session()
    original_commit = db.commit
    db.commit = lambda: (_ for _ in ()).throw(OperationalError("INSERT", {}, Exception("locked")))

    failed = audit_service.record(
        db, actor_email="a@example.com", action=AuditAction.CREATE_USER, target_email="b@example.com",
    )
    assert failed is None

    db.commit = original_commit
    entry = audit_service.record(
        db, actor_email="a@example.com", action=AuditAction.RESET_PASSWORD, target_email="c@example.com",
    )

    assert entry is not None
    assert audit_service.list_recent(db) == [entry]
