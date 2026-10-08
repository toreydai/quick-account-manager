"""改档位：只升不降，且升级在 Quick 里原地完成。

背景（docs/design.md 的"升级为什么要固定 IAM Role"一节）：Quick 联邦用户名是
`<IAM Role>/<邮箱>`。以前改档位只是换 Keycloak 的组，下次登录 SAML 带上另一个
IAM Role，Quick 就新建一个同邮箱的用户——开了邮箱同步会直接拒绝登录
（"found more than 1 user for your email"），老用户名下的分析/仪表板也看不到，
旧席位还继续计费。

现在的做法：
1. 每个人的 SAML Role 属性固定在 Keycloak 用户属性里（QUICK_SAML_ROLE_ATTR），
   换组不会换 IAM Role，Quick 用户名不变。
2. 升级时直接对已存在的 Quick 用户调 UpdateUser 改角色，历史数据原样保留。
3. QuickSight 不允许降级（实测 UpdateUser 报 "You cannot downgrade a user
   role."），所以本应用不提供降级。
"""

from typing import Optional

from app.services.keycloak_service import (
    GROUP_TO_ROLE,
    ROLE_TO_GROUP,
    KeycloakService,
    PartialFailureError,
    QuickUser,
    RoleNotAllowedError,
)
from app.services.quicksight_service import (
    GROUP_TO_ROLE_PREFIX,
    QuickSightLookupError,
    QuickSightStatusChecker,
    find_user,
    list_quick_users,
    update_user_role,
)

# 档位 -> (级别, 是否专业版)。一次变更要求两个维度都不降、至少一个升。
# 只比"级别不降且专业版不降"，不去猜"作者版→阅读专业版"这类跨维度变更算不算
# 降级——QuickSight 对这类组合怎么判定没有实测过，保守起见一律不允许。
_TIER_RANK = {
    "阅读版": (0, 0),
    "阅读专业版": (0, 1),
    "作者版": (1, 0),
    "作者专业版": (1, 1),
    "管理员版": (2, 0),
    "管理员专业版": (2, 1),
}

QUICK_ROLE_BY_TIER = {
    "阅读版": "READER",
    "阅读专业版": "READER_PRO",
    "作者版": "AUTHOR",
    "作者专业版": "AUTHOR_PRO",
    "管理员版": "ADMIN",
    "管理员专业版": "ADMIN_PRO",
}


class DowngradeNotSupportedError(ValueError):
    pass


class RolePinningNotReadyError(ValueError):
    pass


def is_upgrade(current: Optional[str], new: str) -> bool:
    if current is None:
        return True  # 不在任何档位组里：这是首次分配，不是升降级
    cur, tgt = _TIER_RANK[current], _TIER_RANK[new]
    return tgt[0] >= cur[0] and tgt[1] >= cur[1] and tgt != cur


def upgrade_targets(current: Optional[str]) -> list:
    return [t for t in _TIER_RANK if is_upgrade(current, t)]


def saml_role_value(aws_account_id: str, role_name: str, provider_name: str) -> str:
    return (
        f"arn:aws:iam::{aws_account_id}:role/{role_name},"
        f"arn:aws:iam::{aws_account_id}:saml-provider/{provider_name}"
    )


def role_name_from_saml_value(value: str) -> str:
    return value.split(",", 1)[0].rsplit("/", 1)[-1]


def tier_role_name(tier: str) -> str:
    return GROUP_TO_ROLE_PREFIX[ROLE_TO_GROUP[tier]]


def upgrade_tier(
    kc: KeycloakService,
    qs: QuickSightStatusChecker,
    target: QuickUser,
    new_role: str,
    *,
    aws_account_id: str,
    provider_name: str,
    pinning_enabled: bool,
) -> None:
    if new_role not in ROLE_TO_GROUP:
        raise RoleNotAllowedError(f"角色 {new_role!r} 不在本应用管理范围内，只能是 {tuple(ROLE_TO_GROUP)}")
    if target.role == new_role:
        return
    if not is_upgrade(target.role, new_role):
        raise DowngradeNotSupportedError(
            f"不支持从「{target.role}」变更到「{new_role}」：Amazon Quick 不允许降低已有用户的角色"
            "（UpdateUser 会报 \"You cannot downgrade a user role.\"），本应用只提供升级。"
            "确实需要降档请先在 Quick 里处理该用户名下的资产，再联系管理员手动处理。"
        )

    if not pinning_enabled:
        # SAML Role mapper 还是按组发 Role：换组必然换 IAM Role，Quick 会新建同邮箱用户。
        # 只有这个人在 Quick 里还没有账号时，换组才是安全的。
        existing = None
        if target.role:
            try:
                existing = find_user(qs, f"{tier_role_name(target.role)}/{target.email}")
            except QuickSightLookupError as exc:
                raise RolePinningNotReadyError(f"无法确认该用户在 Quick 里是否已有账号：{exc}") from exc
        if existing:
            raise RolePinningNotReadyError(
                f"{target.email} 在 Quick 里已经有账号，而 Keycloak 的 SAML Role 还在按组发放，换组会让 Quick "
                "新建一个同邮箱的用户并导致登录失败。请先按 docs/deployment.md「固定 IAM Role 的切换步骤」"
                "完成回填并切换 mapper，再把 ROLE_PINNING_ENABLED 设为 true。"
            )
        kc.change_tier(target.id, new_role)
        return

    # --- 已固定 IAM Role 的模式 ---
    pinned_value = target.pinned_saml_role
    quick_user = None
    current_role_name = None
    try:
        if pinned_value:
            current_role_name = role_name_from_saml_value(pinned_value)
            quick_user = find_user(qs, f"{current_role_name}/{target.email}")
        else:
            # 还没固定过的存量用户：不能只按"所在组推出来的 Role"去猜——生产里用户名前缀
            # 和现在的组对不上的情况是存在的（以前换过组）。按邮箱把 Quick 全部用户扫一遍，
            # 以他实际登录用的那个用户为准。
            matches = [
                u for u in list_quick_users(qs)
                if (u.get("Email") or "").lower() == target.email.lower()
            ]
            if len(matches) > 1:
                raise PartialFailureError(
                    f"Quick 里已有多个用户占用邮箱 {target.email}：{[m['UserName'] for m in matches]}。"
                    "请先按 troubleshooting.md 第 3 条清理重复用户，再重试升级。本次未做任何改动。"
                )
            if matches:
                quick_user = matches[0]
                if "/" not in quick_user["UserName"]:
                    raise PartialFailureError(
                        f"邮箱 {target.email} 被非联邦用户 {quick_user['UserName']} 占用，无法固定 IAM Role。"
                        "请先把它的邮箱改成占位邮箱或删除，再重试。本次未做任何改动。"
                    )
                current_role_name = quick_user["UserName"].split("/", 1)[0]
    except QuickSightLookupError as exc:
        raise PartialFailureError(f"{exc}。为避免走错分支，本次升级未做任何改动，可以重试。") from exc

    if quick_user is None:
        # Quick 里还没有这个人：首次登录会用新档位对应的 Role 创建，Lambda 负责设角色。
        kc.set_pinned_saml_role(
            target.id, saml_role_value(aws_account_id, tier_role_name(new_role), provider_name)
        )
        kc.change_tier(target.id, new_role)
        return

    if not pinned_value:
        # 老用户还没固定过：趁这次把他现在实际使用的 Role 固定下来，再换组。
        kc.set_pinned_saml_role(
            target.id, saml_role_value(aws_account_id, current_role_name, provider_name)
        )

    username = quick_user["UserName"]
    try:
        update_user_role(qs, username, target.email, QUICK_ROLE_BY_TIER[new_role])
    except Exception as exc:  # noqa: BLE001
        raise PartialFailureError(
            f"Quick 里 {username} 升级到「{new_role}」失败：{exc}。Keycloak 的组还没改，本次升级未生效，可以重试。"
        ) from exc

    try:
        kc.change_tier(target.id, new_role)
    except Exception as exc:  # noqa: BLE001
        raise PartialFailureError(
            f"Quick 里 {username} 已升级到「{new_role}」，但 Keycloak 换组失败（{exc}）："
            "请重试本次升级（Quick 侧是幂等的），或去 Keycloak 手动把该用户加进新组、移出旧组。"
        ) from exc


def plan_backfill(users, quick_users, *, aws_account_id: str, provider_name: str) -> list:
    """给还没固定 IAM Role 的存量用户算出该固定成哪个 Role。返回
    [(user, saml_value 或 None, 状态, 说明)]。

    以 Quick 里该邮箱实际存在的联邦用户为准（保证用户名不变、历史数据不丢）；
    Quick 里没有这个人，就按他现在的组推出来的 Role 固定；邮箱被多个用户或
    非联邦用户占用的，不替他们做决定，留给管理员先清理。
    """
    by_email = {}
    for q in quick_users:
        by_email.setdefault((q.get("Email") or "").lower(), []).append(q["UserName"])

    plan = []
    for u in users:
        if not u.role:
            plan.append((u, None, "skipped", "不在任何档位组里，跳过"))
            continue
        if u.pinned_saml_role:
            plan.append((u, None, "skipped", "已固定，跳过"))
            continue
        names = by_email.get(u.email.lower(), [])
        if len(names) > 1:
            plan.append((u, None, "failed", f"Quick 里有多个用户占用该邮箱 {names}，请先清理重复用户"))
        elif names and "/" not in names[0]:
            plan.append((u, None, "failed", f"邮箱被非联邦用户 {names[0]} 占用，请先改邮箱或删除"))
        elif names:
            role_name = names[0].split("/", 1)[0]
            plan.append((u, saml_role_value(aws_account_id, role_name, provider_name), "success", f"按 Quick 实际用户固定为 {role_name}"))
        else:
            role_name = tier_role_name(u.role)
            plan.append((u, saml_role_value(aws_account_id, role_name, provider_name), "success", f"Quick 里还没有账号，按所在组固定为 {role_name}"))
    return plan
