"""建号/重置密码/改档位/停用的业务逻辑。

密码生成规则和 partialImport payload 结构直接迁移自
xuechuan-quick-sso/add_single_user.py（已经在生产验证过 34 人），
只是从 CLI 脚本的函数搬成这里的 service 方法。

角色范围（2026-08-20 用户明确要求）：全部 6 档都管，不再是最初设计里
「只开放作者两档」的边界——原因是生产上出现了"某个真实管理员账号在列表页
显示不在管理范围"的情况，用户要求所有人都能被管，不能有例外。
"""

import secrets
import string
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import List, Optional

from app.services.keycloak_client import KeycloakAPIError, KeycloakClient

# list_users() 并发查每个用户的组归属用，见 list_users() 的说明（按用户查
# 比按组查成员快一个数量级）。跟 app/api/users.py 的 _QS_LOOKUP_WORKERS
# 同一个模式，数值不用严格对齐——这边单次调用只要 ~0.1s，量级本身就快，
# 不像 QuickSight 那边贴近 botocore 连接池上限。
_ROLE_LOOKUP_WORKERS = 32

# 全部 6 档（对齐 xuechuan-quick-sso/admin-guide-add-users.md 里
# create_users_from_xlsx.py 的完整映射），不再只开放作者两档。
ROLE_TO_GROUP = {
    "管理员专业版": "/quick-admin-pro",
    "作者专业版": "/quick-author-pro",
    "阅读专业版": "/quick-reader-pro",
    "管理员版": "/quick-admin",
    "作者版": "/quick-author",
    "阅读版": "/quick-reader",
}
GROUP_TO_ROLE = {v: k for k, v in ROLE_TO_GROUP.items()}
VALID_ROLES = tuple(ROLE_TO_GROUP.keys())

# 2026-08-24：只有「新建账号」这个入口收窄回两档——跟 2026-08-20 那次扩到
# 全 6 档的决定不冲突，那次是为了让列表页能管到所有已存在的真实账号（改
# 档位/停用/删除都还是全 6 档，ROLE_TO_GROUP/VALID_ROLES 不变）；这次收窄
# 的是"新招一个人该给他开哪个档位"这个决策面——管理员/阅读者这几档目前
# 都是内部已有账号手动走 Keycloak 控制台开的特殊情况，不该让"新建账号"
# 这个高频操作的下拉框把这些低频、需要额外判断的档位也列出来，增加选错
# 的可能性。单人建号（create_user）和批量导入（batch_import.parse_xlsx）
# 都用这个常量校验，改档位（change_tier）不受影响。
CREATE_ALLOWED_ROLES = ("作者专业版", "作者版")

# 这个组的成员同时也是唯一能登录 quick-account-manager 本身的人
# （app/core/config.py 的 admin_group_path）。开放管理员档位的改档位/停用
# 之后，理论上任何一个管理员都能把最后一个 admin-pro 降级/停用，导致所有人
# （包括操作者自己）都进不去这个工具，也没法再通过工具自己修——这是这次
# 扩权限直接引入的真实风险，不是假设性的，必须在 service 层挡住，不能只
# 指望前端/使用者小心。
ADMIN_PRO_GROUP_PATH = "/quick-admin-pro"
ADMIN_PRO_ROLE = "管理员专业版"

PASSWORD_ALPHABET = {
    "upper": string.ascii_uppercase,
    "lower": string.ascii_lowercase,
    "digit": string.digits,
    "special": "!@#$%^&*()-_=+",
}

# 开头是这几个字符的字符串在 Excel/WPS 里会被当成公式解析（历史踩过的坑，
# 见 xuechuan-quick-sso/deployment.md 第 18 节），生成密码时首字符要避开；
# 这里虽然不写 xlsx 了，但保留同一条规则，避免管理员事后把密码贴进表格时
# 又踩一次同样的坑。
FORMULA_TRIGGER_CHARS = "=+-@\t\r"


class RoleNotAllowedError(ValueError):
    pass


class UserNotFoundError(Exception):
    pass


class PartialFailureError(Exception):
    """一个操作需要两次以上 Keycloak 调用，前面几步已经生效、后面某一步失败了，
    Keycloak 里的真实状态和"这次操作到底成没成功"对不上号。这种情况不能像普通
    KeycloakAPIError 一样简单报个"失败"就完事——调用方必须把这里的详细说明原样
    透传给管理员（flash 消息 + 审计日志），不能让人以为"失败了=什么都没发生"。
    """


class LastAdminGuardError(ValueError):
    """阻止把 quick-admin-pro 组降到 0 人——这个组同时是登录本应用的门槛，
    降到 0 人等于所有人都进不来了，也没法再通过本应用自己修，只能回头走
    Keycloak 控制台手动救回来。"""


class SelfLockoutError(ValueError):
    """阻止管理员通过本应用停用/降级自己的账号——防呆，不是防坏人。"""


class MustDisableFirstError(ValueError):
    """硬删除前必须先停用（enabled: false）——2026-08-24 加的第二层防呆。
    硬删除不可逆，如果目标账号还在启用状态就允许直接删，等于把"停用"这个
    本该先走的可逆步骤跳过了；一个还在正常使用的账号被删掉，本人会直接
    登不进 SSO，比"先停用"这种当事人能看到异常、有机会反馈的中间状态更
    容易在删错人的时候没人及时发现。"""


def generate_password(username: str, email: str, length: int = 14) -> str:
    all_chars = "".join(PASSWORD_ALPHABET.values())
    while True:
        required = [secrets.choice(cs) for cs in PASSWORD_ALPHABET.values()]
        rest = [secrets.choice(all_chars) for _ in range(length - len(required))]
        chars = required + rest
        secrets.SystemRandom().shuffle(chars)
        pwd = "".join(chars)
        if pwd[0] in FORMULA_TRIGGER_CHARS:
            continue
        if pwd.lower() != username.lower() and pwd.lower() != email.lower():
            return pwd


@dataclass
class QuickUser:
    id: str
    username: str
    email: str
    first_name: str
    last_name: str
    enabled: bool
    role: Optional[str]  # None = 不在 quick-author(-pro) 组里，本应用管不了这个人


@dataclass
class CreateUserResult:
    created: bool  # False = 该 username 在 Keycloak 里已存在，被 SKIP 了
    password: Optional[str]


class KeycloakService:
    def __init__(self, client: KeycloakClient):
        self._client = client
        self._group_id_cache: dict = {}

    # ---------- 建号 ----------
    #
    # 原来（add_single_user.py / 建号最初版本）用的是 `POST /partialImport`——
    # 端到端本地测试对着真实 Keycloak 26.6.3 打的时候发现这个接口对 4.1 节配的
    # service account（只有 manage-users/query-groups/view-users）返回 403：
    # partialImport 能导入任意 realm 资源（client、role 等），Keycloak 把它的
    # 权限检查放在了比 manage-users 更粗的粒度上，不是"能管用户"就能调。
    # add_single_user.py 当年能用是因为它拿的是 master realm 人类管理员的
    # 完整权限，不是这里刻意收窄过的 service account。
    #
    # 改成 `POST /users`（建号）+ `PUT /users/{id}/groups/{groupId}`（加组）
    # 两步——manage-users 权限范围内就能做，且跟 change_tier() 用的是同一种
    # 直接调用风格，不是走批量导入接口。单人建号场景下两次调用的开销可以忽略，
    # 不是 partialImport 当初为批量场景做单次提交优化要解决的那个问题。

    def create_user(
        self,
        email: str,
        first_name: str,
        last_name: str,
        role: str,
        username: Optional[str] = None,
        chinese_name: Optional[str] = None,
    ) -> CreateUserResult:
        if role not in CREATE_ALLOWED_ROLES:
            raise RoleNotAllowedError(f"新建账号只能选 {CREATE_ALLOWED_ROLES} 这两档，{role!r} 不在其中")

        username = username or email.split("@", 1)[0]
        group = ROLE_TO_GROUP[role]
        password = generate_password(username, email)

        payload = {
            "username": username,
            "email": email,
            "firstName": first_name,
            "lastName": last_name,
            "enabled": True,
            "emailVerified": True,
            "requiredActions": [],
            # 显式指定，不依赖 Keycloak 自动赋默认角色——xuechuan-quick-sso 那两个
            # 走 partialImport 的建号脚本 2026-08-21 发现过偶发漏赋 default-roles-quick
            # 导致 Desktop 客户端登录报 "Offline tokens not allowed for the user or
            # client"（troubleshooting.md 第 10 条）。这里走的是 POST /users，本地对
            # 真实 Keycloak 26.6.3 实测过默认就会正确赋，不是同一个 bug，但补上显式声明
            # 作为双保险，不额外增加调用次数。
            "realmRoles": ["default-roles-quick"],
            "credentials": [{"type": "password", "value": password, "temporary": True}],
        }
        if chinese_name:
            # 只是给人看的中文姓名，不参与任何权限/登录逻辑。
            # 注意：Keycloak 26 默认只允许写入 realm User Profile 里显式声明过的
            # 属性，没声明的自定义属性会被静默丢弃（不报错，写了跟没写一样）。
            # 生产 Keycloak 要开 Realm settings → User profile → Unmanaged
            # attributes，这个属性才会真的存进去，见 README「部署前置依赖」第 3
            # 条——这是本地对着真实 Keycloak 26.6.3 做端到端测试才发现的，纯 mock
            # 测试测不出来。不开这个开关不影响其他任何功能，只是这个字段白填。
            payload["attributes"] = {"chineseName": [chinese_name]}
        try:
            resp = self._client.request("POST", "/users", json=payload)
        except KeycloakAPIError as exc:
            if exc.status_code == 409:
                # username 已存在——跟旧版 partialImport 的 ifResourceExists=SKIP
                # 语义对齐：跳过、不报错、不建号。
                return CreateUserResult(created=False, password=None)
            raise

        location = resp.headers.get("Location", "")
        user_id = location.rsplit("/", 1)[-1] if location else None
        if not user_id:
            raise KeycloakAPIError(resp.status_code, "创建用户成功但响应里没有 Location header，拿不到新用户 id")

        # 建号是"建用户 + 加组"两步调用（不是当年 partialImport 那种单次原子提交，
        # 见上面的注释），第二步失败会留下一个真实存在但没加组、密码没人知道的
        # 孤儿账号。这里尝试自动回滚删掉它，回滚也失败就把两件事都如实报出来，
        # 不能让管理员以为"失败了=Keycloak 里什么都没发生"。
        try:
            group_id = self._get_group_id(group)
            self._client.request("PUT", f"/users/{user_id}/groups/{group_id}")
        except (KeycloakAPIError, UserNotFoundError) as exc:
            try:
                self._client.request("DELETE", f"/users/{user_id}")
            except KeycloakAPIError as rollback_exc:
                raise PartialFailureError(
                    f"账号已创建（user_id={user_id}）但加组失败（{exc}），"
                    f"尝试自动回滚删除也失败（{rollback_exc}）："
                    f"Keycloak 里有一个孤儿账号 {user_id}（{email}），需要管理员手动处理。"
                ) from exc
            raise PartialFailureError(
                f"账号创建后加组失败（{exc}），已自动回滚删除刚创建的账号（user_id={user_id}），"
                "本次建号未生效，可以重试。"
            ) from exc

        return CreateUserResult(created=True, password=password)

    # ---------- 重置密码 ----------

    def reset_password(self, user_id: str, username: str, email: str) -> str:
        password = generate_password(username, email)
        self._client.request(
            "PUT",
            f"/users/{user_id}/reset-password",
            json={"type": "password", "value": password, "temporary": True},
        )
        return password

    # ---------- 用户列表 ----------

    def list_users(self, max_results: int = 500) -> List[QuickUser]:
        """一次性拉全量（几十人规模够用，见 docs/design.md 2 节）。

        角色解析用的是并发的按用户查（GET /users/{id}/groups），不是按组查
        成员（GET /groups/{id}/members）批量 join。这是生产实测踩过的坑：
        一开始为了避免 N+1 改成了"先各拉一次两个组的成员列表"（固定 2 次
        调用），理论上调用次数更少，但真实 Keycloak 上 GET /groups/{id}/members
        这个接口单次实测要 ~5 秒（Keycloak 这个接口本身性能弱是有据可查的
        通病），2 次就是 ~10 秒，直接导致用户反馈"用户列表非常慢"。反过来
        实测 GET /users/{id}/groups 单次只要 ~0.1-0.15 秒，38 个用户全部
        并发查（线程池，见 _QS_LOOKUP_WORKERS 同款模式）比"更少但更慢的
        批量调用"快一个数量级。调用次数不是唯一指标，单次调用的真实延迟
        也要考虑——这里是反直觉但经过真实生产环境验证的结论。
        """
        users_raw = self._fetch_all_users_raw(max_results)

        with ThreadPoolExecutor(max_workers=_ROLE_LOOKUP_WORKERS) as pool:
            roles = list(pool.map(lambda raw: self._resolve_role(raw["id"]), users_raw))

        users: List[QuickUser] = []
        for raw, role in zip(users_raw, roles):
            users.append(
                QuickUser(
                    id=raw["id"],
                    username=raw.get("username", ""),
                    email=raw.get("email", ""),
                    first_name=raw.get("firstName", ""),
                    last_name=raw.get("lastName", ""),
                    enabled=raw.get("enabled", False),
                    role=role,
                )
            )
        return users

    def _fetch_all_users_raw(self, max_results: int = 500) -> List[dict]:
        users: List[dict] = []
        first = 0
        page_size = 100
        while True:
            resp = self._client.request(
                "GET", "/users", params={"first": first, "max": min(page_size, max_results - len(users))}
            )
            page = resp.json()
            if not page:
                break
            users.extend(page)
            first += len(page)
            if len(page) < page_size or len(users) >= max_results:
                break
        return users

    def _resolve_role(self, user_id: str) -> Optional[str]:
        """单个用户查所在组，反查角色。列表页和单人查询（get_user_by_id）
        都走这条路径——见 list_users() 里的说明，这是刻意选择，不是 N+1 疏漏。
        """
        resp = self._client.request("GET", f"/users/{user_id}/groups")
        for group in resp.json():
            path = group.get("path", "")
            if path in GROUP_TO_ROLE:
                return GROUP_TO_ROLE[path]
        return None

    def get_user_by_id(self, user_id: str) -> QuickUser:
        resp = self._client.request("GET", f"/users/{user_id}")
        raw = resp.json()
        return QuickUser(
            id=raw["id"],
            username=raw.get("username", ""),
            email=raw.get("email", ""),
            first_name=raw.get("firstName", ""),
            last_name=raw.get("lastName", ""),
            enabled=raw.get("enabled", False),
            role=self._resolve_role(user_id),
        )

    # ---------- 订阅档位变更（4.5 节：先加新组再删旧组）----------

    def change_tier(self, user_id: str, new_role: str) -> None:
        if new_role not in ROLE_TO_GROUP:
            raise RoleNotAllowedError(f"角色 {new_role!r} 不在本应用管理范围内，只能是 {VALID_ROLES}")

        current_role = self._resolve_role(user_id)

        if current_role == ADMIN_PRO_ROLE and new_role != ADMIN_PRO_ROLE:
            self._guard_last_admin_pro(exempt_user_id=user_id)

        new_group_id = self._get_group_id(ROLE_TO_GROUP[new_role])

        self._client.request("PUT", f"/users/{user_id}/groups/{new_group_id}")

        if not current_role or current_role == new_role:
            return

        # 新组已经加成功了——从这里开始，任何失败都是"部分生效"，不能再简单地
        # 报个"变更失败"就当没发生过，用户现在实打实地已经在新组里了。
        old_group_path = ROLE_TO_GROUP[current_role]
        try:
            old_group_id = self._get_group_id(old_group_path)
        except UserNotFoundError as exc:
            raise PartialFailureError(
                f"已加入「{new_role}」组，但找不到旧组 {old_group_path}（{exc}）："
                f"用户 {user_id} 现在同时在新旧两个组里，需要管理员去 Keycloak 手动检查/移除旧组。"
            ) from exc

        last_exc: Optional[KeycloakAPIError] = None
        for _ in range(2):  # 低频操作，简单重试一次，不做指数退避
            try:
                self._client.request("DELETE", f"/users/{user_id}/groups/{old_group_id}")
                return
            except KeycloakAPIError as exc:
                last_exc = exc
        raise PartialFailureError(
            f"已加入「{new_role}」组，但移除「{current_role}」组失败（重试后仍失败：{last_exc}）："
            f"用户 {user_id} 现在同时在两个组里，需要管理员去 Keycloak 手动移除 {old_group_path}。"
        ) from last_exc

    def _get_group_id(self, group_path: str) -> str:
        if group_path in self._group_id_cache:
            return self._group_id_cache[group_path]
        group_name = group_path.lstrip("/")
        resp = self._client.request("GET", "/groups", params={"search": group_name, "exact": "true"})
        for group in resp.json():
            if group.get("path") == group_path:
                self._group_id_cache[group_path] = group["id"]
                return group["id"]
        raise UserNotFoundError(f"Keycloak 里找不到组 {group_path!r}，检查 realm 里这个组是否存在")

    # ---------- 账号停用/启用（4.5 节：enabled 开关，不做硬删除）----------

    def set_enabled(self, user_id: str, enabled: bool) -> None:
        if not enabled:
            current_role = self._resolve_role(user_id)
            if current_role == ADMIN_PRO_ROLE:
                self._guard_last_admin_pro(exempt_user_id=user_id)
        self._client.request("PUT", f"/users/{user_id}", json={"enabled": enabled})

    # ---------- 硬删除（design.md 4.5 节原来评估后决定留作后续迭代，现已启用）----------

    def delete_user(self, user_id: str) -> None:
        """DELETE /admin/realms/quick/users/{id}，不可逆，Keycloak 里的用户记录
        整条消失（对比 set_enabled(False) 只是把人锁在门外，记录还在）。路由层
        （app/api/users.py）在调这个方法之前已经做过"不能删自己"的防呆检查，
        这里保留两层业务规则（不管路由怎么调都要生效，不指望调用方小心）：
        必须先停用（MustDisableFirstError，2026-08-24 加）、最后一个管理员
        保护——跟 set_enabled 一样，不能让最后一个 admin-pro 被删掉导致所有
        人都进不来这个工具。

        代价：多打一次 GET /users/{id}（拿 enabled 状态），硬删除本来就是
        低频操作，不值得为了省这一次调用而把"先停用"这条规则退化成只在
        路由层查一次、可能被绕过的检查。
        """
        target = self.get_user_by_id(user_id)
        if target.enabled:
            raise MustDisableFirstError(
                f"「{target.email}」目前还是启用状态，硬删除前必须先停用——"
                "在用户列表里先点「停用」，确认没问题之后再回来删除。"
            )
        if target.role == ADMIN_PRO_ROLE:
            self._guard_last_admin_pro(exempt_user_id=user_id)
        self._client.request("DELETE", f"/users/{user_id}")

    # ---------- 最后一个管理员保护 ----------

    def _guard_last_admin_pro(self, exempt_user_id: str) -> None:
        """把 `exempt_user_id` 这个人从 quick-admin-pro 排除在外，数一下这个组
        剩下还有几个真实成员——如果只剩这一个人（也就是 `exempt_user_id` 本人），
        说明这次操作会把这个组清空，直接拒绝。

        代价是要拉一次全量用户列表（~2 秒，见 list_users() 的说明），但改档位/
        停用本来就是低频的交互操作（人点一下按钮，等几秒拿结果），不是页面加载
        那种要死磕毫秒级的路径，不值得为了这个再单独去优化。
        """
        users = self.list_users()
        remaining = [u for u in users if u.role == ADMIN_PRO_ROLE and u.id != exempt_user_id]
        if not remaining:
            raise LastAdminGuardError(
                f"「{ADMIN_PRO_ROLE}」组现在只剩这一个人了，不能通过本应用把最后一个管理员"
                "降级/停用——这会导致所有人（包括你自己）都登录不了这个工具，也没法再用"
                "工具自己修，只能去 Keycloak 控制台手动处理。如果确实要这么做，请先在 "
                f"Keycloak 里手动把至少一个人加进「{ADMIN_PRO_ROLE}」组，再回来操作。"
            )
