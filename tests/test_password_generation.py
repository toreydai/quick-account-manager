"""密码生成规则（迁移自 xuechuan-quick-sso/add_single_user.py）：14 位，强制
大小写/数字/特殊字符齐全，首字符不能触发 Excel/WPS 公式解析，且不能等于
username/email。见 docs/design.md 4.2 节 + xuechuan-quick-sso/deployment.md
第 18 节踩过的坑。
"""

import string

from app.services.keycloak_service import FORMULA_TRIGGER_CHARS, generate_password


def test_password_length_is_14():
    pwd = generate_password("someuser", "someuser@example.com")
    assert len(pwd) == 14


def test_password_has_all_character_classes():
    pwd = generate_password("someuser", "someuser@example.com")
    assert any(c in string.ascii_uppercase for c in pwd)
    assert any(c in string.ascii_lowercase for c in pwd)
    assert any(c in string.digits for c in pwd)
    assert any(c in "!@#$%^&*()-_=+" for c in pwd)


def test_password_first_char_never_triggers_formula_parsing():
    for _ in range(500):
        pwd = generate_password("someuser", "someuser@example.com")
        assert pwd[0] not in FORMULA_TRIGGER_CHARS


def test_password_never_equals_username_or_email():
    for _ in range(200):
        pwd = generate_password("abc", "abc@example.com")
        assert pwd.lower() != "abc"
        assert pwd.lower() != "abc@example.com"


def test_password_is_randomized_across_calls():
    passwords = {generate_password("someuser", "someuser@example.com") for _ in range(20)}
    assert len(passwords) == 20
