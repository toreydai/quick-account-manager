"""角色-组映射：管理范围（列表/改档位/停用/删除）全部 6 档都管（2026-08-20
用户明确要求，推翻了最初"只开放作者两档"的设计决定）；新建账号这个入口
2026-08-24 单独收窄回作者专业版/作者版两档（CREATE_ALLOWED_ROLES），两个
决定不冲突——管理范围没变，只是"新建"这一个操作的可选项变少了，见
docs/design.md 2 节已确认事项。"""

import pytest

from app.services.keycloak_service import (
    CREATE_ALLOWED_ROLES,
    GROUP_TO_ROLE,
    ROLE_TO_GROUP,
    VALID_ROLES,
    RoleNotAllowedError,
    KeycloakService,
)


def test_all_six_tiers_allowed():
    assert set(ROLE_TO_GROUP.keys()) == {
        "管理员版", "管理员专业版", "作者版", "作者专业版", "阅读版", "阅读专业版",
    }


def test_role_to_group_mapping_matches_admin_guide_table():
    # 跟 xuechuan-quick-sso/admin-guide-add-users.md 里
    # create_users_from_xlsx.py 的完整映射表保持一致
    assert ROLE_TO_GROUP["管理员版"] == "/quick-admin"
    assert ROLE_TO_GROUP["管理员专业版"] == "/quick-admin-pro"
    assert ROLE_TO_GROUP["作者版"] == "/quick-author"
    assert ROLE_TO_GROUP["作者专业版"] == "/quick-author-pro"
    assert ROLE_TO_GROUP["阅读版"] == "/quick-reader"
    assert ROLE_TO_GROUP["阅读专业版"] == "/quick-reader-pro"


def test_group_to_role_is_reverse_mapping():
    for role, group in ROLE_TO_GROUP.items():
        assert GROUP_TO_ROLE[group] == role


def test_valid_roles_tuple_matches_mapping_keys():
    assert set(VALID_ROLES) == set(ROLE_TO_GROUP.keys())


def test_change_tier_accepts_reader_role():
    """change_tier（管理已有账号）不受 2026-08-24 收窄的影响，还是全 6 档。"""
    service = KeycloakService(client=None)
    with pytest.raises(AttributeError):
        service.change_tier(user_id="abc", new_role="阅读版")


def test_create_user_still_rejects_genuinely_invalid_role():
    service = KeycloakService(client=None)
    with pytest.raises(RoleNotAllowedError):
        service.create_user(email="x@example.com", first_name="X", last_name="Y", role="不存在的档位")


def test_change_tier_still_rejects_genuinely_invalid_role():
    service = KeycloakService(client=None)
    with pytest.raises(RoleNotAllowedError):
        service.change_tier(user_id="abc", new_role="不存在的档位")


# ---------- 2026-08-24：新建账号单独收窄回两档 ----------


def test_create_allowed_roles_is_author_pro_and_author_only():
    assert set(CREATE_ALLOWED_ROLES) == {"作者专业版", "作者版"}


def test_create_user_accepts_the_two_allowed_roles():
    service = KeycloakService(client=None)
    for role in CREATE_ALLOWED_ROLES:
        with pytest.raises(AttributeError):
            # client=None，走到 self._client.request(...) 时才会报错——
            # 证明角色校验本身通过了，没有在 RoleNotAllowedError 那关被拦下来
            service.create_user(email="x@example.com", first_name="X", last_name="Y", role=role)


def test_create_user_rejects_roles_outside_the_two_allowed_ones():
    """2026-08-20 到 2026-08-24 之间 create_user 接受全 6 档，现在改回只
    开放这两档——管理员/阅读者这几档目前都是内部已有账号手动走 Keycloak
    控制台开的特殊情况，不该出现在"新建账号"这个高频操作的下拉框里。"""
    service = KeycloakService(client=None)
    for role in set(ROLE_TO_GROUP) - set(CREATE_ALLOWED_ROLES):
        with pytest.raises(RoleNotAllowedError):
            service.create_user(email="x@example.com", first_name="X", last_name="Y", role=role)
