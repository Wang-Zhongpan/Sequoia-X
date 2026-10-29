"""配置管理属性测试。

【怎么运行测试？】
    pytest                       # 运行 tests/ 下所有测试
    pytest tests/test_config.py  # 只运行本文件
    pytest -k env_overrides      # 只运行名字包含 env_overrides 的测试

【pytest 的基本约定】
  - 文件名以 test_ 开头；
  - 函数名以 test_ 开头；
  - 用 `assert 条件` 判断是否通过，不需要自己写 if/print。

【hypothesis 是什么？】
  普通测试只测一个固定输入；hypothesis 是"属性测试"（property-based testing），
  它会按你给的规则自动生成几十上百组输入，帮我们发现边界情况。
  例如 st.text(...) 会生成各种字符串，包括空串、特殊字符、超长字符串。
  @given(...) 把生成的数据作为参数传进测试函数；
  @settings(...) 控制生成数量（max_examples）等。
"""

import os

import pytest
from hypothesis import given, settings as h_settings, HealthCheck
from hypothesis import strategies as st
from pydantic import ValidationError


# Feature: sequoia-x-v2, Property 1: 环境变量覆盖配置默认值
# @given 的参数名（db_path）必须和测试函数的参数名一致，hypothesis 会按名字传值。
# st.text(...) 生成满足条件的字符串：
#   min_size=1, max_size=100 → 长度在 1~100 之间；
#   alphabet=...             → 只允许这些字符（字母/数字/路径符号）。
@given(db_path=st.text(min_size=1, max_size=100, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="/_.-")))
# max_examples=100：最多生成 100 组输入；
# suppress_health_check：用了 monkeypatch 这类"函数级 fixture"时，
# hypothesis 默认会警告"每次喂数据都重新执行 fixture 开销大"，这里明确忽略。
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_env_overrides_default(db_path: str, monkeypatch) -> None:
    """属性 1：任意合法 db_path 通过环境变量设置后，Settings 实例应反映该值。"""
    import sequoia_x.core.config as cfg_module

    # monkeypatch 是 pytest 提供的 fixture，作用是"临时修改，测试结束自动还原"。
    # setenv 修改环境变量；setattr 修改对象属性。
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://example.com/hook")
    # 把模块级单例置空，强制下一次 Settings() 重新读取环境变量。
    monkeypatch.setattr(cfg_module, "_settings", None)

    from sequoia_x.core.config import Settings

    s = Settings()
    assert s.db_path == db_path


# Feature: sequoia-x-v2, Property 2: 缺失必填字段触发 ValidationError
def test_missing_required_field_raises() -> None:
    """属性 2：缺少 feishu_webhook_url 时，实例化 Settings 应抛出 ValidationError。"""
    import os
    from sequoia_x.core.config import Settings

    # 确保环境变量中没有该字段
    # os.environ.pop(key, default)：删除并返回该变量；不存在时返回默认值 None。
    env_backup = os.environ.pop("FEISHU_WEBHOOK_URL", None)
    try:
        # pytest.raises(异常类型) 是一个上下文管理器：
        # 只有当 with 块里真的抛出指定异常时，测试才算通过。
        # as exc_info 可以把异常对象取出来做进一步断言。
        with pytest.raises(ValidationError) as exc_info:
            # _env_file=None 表示"这次不要读 .env 文件"，
            # 避免本地 .env 里恰好配置了 Webhook 导致测试不生效。
            Settings(_env_file=None)
        # str(异常) 里应包含缺失的字段名。
        assert "feishu_webhook_url" in str(exc_info.value).lower()
    finally:
        # 不管断言成功还是失败，都要把环境变量恢复，避免污染其他测试。
        if env_backup is not None:
            os.environ["FEISHU_WEBHOOK_URL"] = env_backup
