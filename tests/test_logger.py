"""日志系统属性测试。

【测试目标】
  验证 get_logger() 的幂等性：同名重复调用不会创建新 logger，也不会重复加 handler。
  如果这里出错，真实运行时每条日志可能会被打印两遍、三遍。

【hypothesis 用法】
  @given(name=...) 会自动生成 100 个不同的 name 分别测试，
  比手写 100 个 assert 覆盖更广，也更省事。
"""

from hypothesis import given, settings as h_settings
from hypothesis import strategies as st


# Feature: sequoia-x-v2, Property 3: get_logger 同名返回同一实例
@given(name=st.text(min_size=1, max_size=50, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="._")))
@h_settings(max_examples=100)
def test_get_logger_same_instance(name: str) -> None:
    """属性 3：对任意 name，多次调用 get_logger(name) 应返回同一 Logger 实例。"""
    from sequoia_x.core.logger import get_logger

    logger1 = get_logger(name)
    logger2 = get_logger(name)

    # `is` 比较的是"是不是同一个对象"（内存地址），
    # 不是 `==`（值相等）。这里要验证的正是对象同一性。
    assert logger1 is logger2

    # 确保 handler 没有被重复添加
    # 如果 get_logger 每次调用都 addHandler，这里会是 2、3……
    assert len(logger1.handlers) == 1
