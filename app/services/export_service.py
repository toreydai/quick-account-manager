"""一键导出用户清单（xlsx）。列跟用户列表页一致（用户名/邮箱/拼音名/拼音姓/
角色档位/账号状态），额外带订阅状态——列表页那一列是页面加载完之后前端
异步补上的（见 subscription-status.js），导出这种低频操作不用赶首屏速度，
直接在生成 xlsx 之前把订阅状态一次查完整，导出一份就是完整的一份。
"""

import io
from typing import Dict, List

import openpyxl

from app.services.keycloak_service import QuickUser

HEADER = ("用户名", "邮箱", "拼音名", "拼音姓", "角色档位", "账号状态", "订阅状态")


def build_user_list_xlsx(users: List[QuickUser], subscription_status: Dict[str, str]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "用户清单"
    ws.append(HEADER)
    ws.freeze_panes = "A2"

    for u in users:
        ws.append(
            [
                u.username,
                u.email,
                u.first_name,
                u.last_name,
                u.role or "（未分配档位）",
                "启用中" if u.enabled else "已停用",
                subscription_status.get(u.id, "—") if u.role else "—",
            ]
        )

    for col_letter, width in zip("ABCDEFG", (16, 30, 14, 14, 14, 10, 16)):
        ws.column_dimensions[col_letter].width = width

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
