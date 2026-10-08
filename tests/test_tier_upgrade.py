"""改档位只升不降，升级在 Quick 里原地完成（app/services/tier_service.py）。

背景：Quick 联邦用户名是 <IAM Role>/<邮箱>，换 IAM Role 就会新建一个同邮箱的用户。
QuickSight UpdateUser 实测只能升级（READER→AUTHOR 成功，AUTHOR→READER 报
"You cannot downgrade a user role."），所以本应用不提供降级。
"""

import pytest

from app.services.keycloak_service import PartialFailureError, QuickUser
from app.services.quicksight_service import QuickSightStatusChecker
from app.services.tier_service import (
    DowngradeNotSupportedError,
    RolePinningNotReadyError,
    is_upgrade,
    saml_role_value,
    upgrade_tier,
    upgrade_targets,
)

ACCOUNT = "111122223333"
EMAIL = "a@example.com"


def saml(role_name):
    return saml_role_value(ACCOUNT, role_name, "keycloak")


class NotFound(Exception):
    response = {"Error": {"Code": "ResourceNotFoundException"}}


class FakeQuickClient:
    def __init__(self, users=None, fail_update=False):
        self.users = {u["UserName"]: u for u in (users or [])}
        self.fail_update = fail_update
        self.updates = []

    def describe_user(self, **kw):
        if kw["UserName"] not in self.users:
            raise NotFound()
        return {"User": self.users[kw["UserName"]]}

    def list_users(self, **kw):
        return {"UserList": list(self.users.values())}

    def update_user(self, **kw):
        if self.fail_update:
            raise RuntimeError("boom")
        self.updates.append(kw)


def make_qs(client):
    qs = QuickSightStatusChecker.__new__(QuickSightStatusChecker)
    qs._client, qs._account_id = client, ACCOUNT
    return qs


class FakeKC:
    def __init__(self):
        self.calls = []

    def set_pinned_saml_role(self, user_id, value):
        self.calls.append(("pin", value))

    def change_tier(self, user_id, new_role):
        self.calls.append(("group", new_role))


def user(role, pinned=None):
    return QuickUser(
        id="u1", username="a", email=EMAIL, first_name="", last_name="",
        enabled=True, role=role, pinned_saml_role=pinned,
    )


def run(kc, qs, target, new_role, pinning=True):
    upgrade_tier(kc, qs, target, new_role, aws_account_id=ACCOUNT, provider_name="keycloak", pinning_enabled=pinning)


def test_upgrade_matrix():
    assert is_upgrade("阅读版", "作者版")
    assert is_upgrade("作者版", "作者专业版")
    assert is_upgrade("阅读版", "管理员专业版")
    assert is_upgrade(None, "阅读版")  # 不在任何档位组里：首次分配
    assert not is_upgrade("作者专业版", "作者版")
    assert not is_upgrade("作者版", "阅读版")
    assert not is_upgrade("阅读专业版", "作者版")  # 专业版→非专业版，保守拒绝
    assert not is_upgrade("作者版", "作者版")
    assert upgrade_targets("管理员专业版") == []
    assert upgrade_targets("阅读版") == ["阅读专业版", "作者版", "作者专业版", "管理员版", "管理员专业版"]


def test_downgrade_is_rejected_without_touching_anything():
    kc, client = FakeKC(), FakeQuickClient()
    with pytest.raises(DowngradeNotSupportedError):
        run(kc, make_qs(client), user("作者专业版"), "作者版")
    assert kc.calls == [] and client.updates == []


def test_pinned_user_is_upgraded_in_place_and_keeps_username():
    name = "QuickAuthorRole/" + EMAIL
    client = FakeQuickClient([{"UserName": name, "Email": EMAIL, "Role": "AUTHOR"}])
    kc = FakeKC()
    run(kc, make_qs(client), user("作者版", pinned=saml("QuickAuthorRole")), "作者专业版")
    assert client.updates == [
        {"AwsAccountId": ACCOUNT, "Namespace": "default", "UserName": name, "Email": EMAIL, "Role": "AUTHOR_PRO"}
    ]
    assert kc.calls == [("group", "作者专业版")]  # 已固定，不重复写属性；Role 前缀保持 QuickAuthorRole


def test_unpinned_user_is_pinned_to_the_role_he_actually_logs_in_with():
    # 以前换过组：现在在作者专业版组，但 Quick 里实际是 QuickAuthorRole 这个用户
    name = "QuickAuthorRole/" + EMAIL
    client = FakeQuickClient([{"UserName": name, "Email": EMAIL, "Role": "AUTHOR"}])
    kc = FakeKC()
    run(kc, make_qs(client), user("作者专业版"), "管理员专业版")
    assert kc.calls == [("pin", saml("QuickAuthorRole")), ("group", "管理员专业版")]
    assert client.updates[0]["UserName"] == name and client.updates[0]["Role"] == "ADMIN_PRO"


def test_unpinned_user_with_duplicate_quick_users_is_refused():
    client = FakeQuickClient([
        {"UserName": "QuickAuthorRole/" + EMAIL, "Email": EMAIL},
        {"UserName": "QuickAuthorProRole/" + EMAIL, "Email": EMAIL},
    ])
    kc = FakeKC()
    with pytest.raises(PartialFailureError):
        run(kc, make_qs(client), user("作者版"), "作者专业版")
    assert kc.calls == [] and client.updates == []


def test_user_without_quick_account_gets_new_role_pinned_then_group_changed():
    kc, client = FakeKC(), FakeQuickClient()
    run(kc, make_qs(client), user("作者版"), "作者专业版")
    assert kc.calls == [("pin", saml("QuickAuthorProRole")), ("group", "作者专业版")]
    assert client.updates == []


def test_quick_update_failure_leaves_keycloak_group_untouched():
    name = "QuickAuthorRole/" + EMAIL
    client = FakeQuickClient([{"UserName": name, "Email": EMAIL}], fail_update=True)
    kc = FakeKC()
    with pytest.raises(PartialFailureError):
        run(kc, make_qs(client), user("作者版", pinned=saml("QuickAuthorRole")), "作者专业版")
    assert ("group", "作者专业版") not in kc.calls


def test_without_pinning_existing_quick_account_refuses_group_swap():
    name = "QuickAuthorRole/" + EMAIL
    client = FakeQuickClient([{"UserName": name, "Email": EMAIL}])
    kc = FakeKC()
    with pytest.raises(RolePinningNotReadyError):
        run(kc, make_qs(client), user("作者版"), "作者专业版", pinning=False)
    assert kc.calls == []


def test_without_pinning_user_never_logged_in_just_swaps_group():
    kc = FakeKC()
    run(kc, make_qs(FakeQuickClient()), user("作者版"), "作者专业版", pinning=False)
    assert kc.calls == [("group", "作者专业版")]


def test_backfill_pins_actual_quick_role_and_flags_conflicts():
    from app.services.tier_service import plan_backfill

    def u(email, role, pinned=None):
        return QuickUser(id=email, username=email, email=email, first_name="", last_name="",
                         enabled=True, role=role, pinned_saml_role=pinned)

    quick_users = [
        {"UserName": "QuickAuthorProRole/a@x.com", "Email": "a@x.com"},      # 实际前缀和组对不上
        {"UserName": "QuickAuthorRole/dup@x.com", "Email": "dup@x.com"},
        {"UserName": "QuickAuthorProRole/dup@x.com", "Email": "dup@x.com"},
        {"UserName": "admin", "Email": "native@x.com"},                       # 非联邦用户占用邮箱
    ]
    users = [
        u("a@x.com", "作者版"), u("dup@x.com", "作者版"), u("native@x.com", "作者版"),
        u("new@x.com", "阅读版"), u("done@x.com", "作者版", pinned=saml("QuickAuthorRole")),
    ]
    out = {t.email: (v, status) for t, v, status, _ in
           plan_backfill(users, quick_users, aws_account_id=ACCOUNT, provider_name="keycloak")}
    assert out["a@x.com"] == (saml("QuickAuthorProRole"), "success")
    assert out["dup@x.com"][1] == "failed" and out["native@x.com"][1] == "failed"
    assert out["new@x.com"] == (saml("QuickReaderRole"), "success")
    assert out["done@x.com"] == (None, "skipped")
