"""飞书通知属性测试。

【测试的关键思想：不要真的发消息】
  测试如果每次都真的 POST 到飞书，会产生大量垃圾消息，还依赖网络。
  这里用 unittest.mock.patch 把 requests.post "换掉"：
  patch("requests.post") 会在 with 块内把 requests.post 替换成 MagicMock，
  调用它不会发网络请求，只会记录"被调用时的参数"，供我们断言。

【本文件涉及的 Python 知识点】
  1. unittest.mock.patch / MagicMock：打桩（mock），替换外部依赖。
  2. Mock.call_args：记录调用参数的对象。
  3. logging.Handler：自定义 handler 可以把日志收集到列表里。
  4. hypothesis 的 st.from_regex：按正则生成字符串。

【已知问题（review 中提到）】
  FeishuNotifier.send() → _build_card() → _get_stock_names() 会真实登录 baostock。
  本文件只 mock 了 requests.post，没有 mock 股票名称查询，
  所以这些测试仍然会发起网络请求，比较慢且依赖网络。
  改进方向：在测试里 patch FeishuNotifier._get_stock_names，让它直接返回 {}。
"""

import json
import logging
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

from sequoia_x.core.config import Settings
from sequoia_x.notify.feishu import FeishuNotifier


def make_settings(webhook_url: str = "https://example.com/default") -> Settings:
    """构造一个测试用 Settings，避免每次都重复写三个必填字段。"""
    return Settings(
        db_path="data/test.db",
        start_date="2024-01-01",
        feishu_webhook_url=webhook_url,
    )


# Feature: sequoia-x-v2, Property 10: 飞书通知包含所有选股结果
@given(
    # 生成 1~10 个不重复的 6 位数字代码。
    # unique=True 保证列表里没有重复元素。
    symbols=st.lists(
        st.text(min_size=6, max_size=6, alphabet="0123456789"),
        min_size=1, max_size=10, unique=True,
    )
)
@h_settings(max_examples=50)
def test_notification_contains_all_symbols(symbols: list[str]) -> None:
    """属性 10：send() 发出的请求体应包含所有 symbol。"""
    settings = make_settings()
    notifier = FeishuNotifier(settings)

    # patch 的目标是"模块里实际被调用的名字"。
    # feishu.py 里写的是 `requests.post(...)`，所以 patch("requests.post") 有效。
    with patch("requests.post") as mock_post:
        # mock_post.return_value 是"伪造的响应对象"。
        # status_code=200 让 send() 走成功分支；
        # .json() 默认返回一个 MagicMock，其 .get("code") 也是 MagicMock，
        # 与 0 比较不相等 → 会走失败分支。但本测试只关心请求体，不影响断言。
        mock_post.return_value = MagicMock(status_code=200)
        notifier.send(symbols=symbols, strategy_name="TestStrategy")

    call_args = mock_post.call_args
    # 这一行在判断"请求体到底放在位置参数还是关键字参数里"
    # （requests.post(url, data=...) 时，url 是位置参数，data 是关键字参数）。
    # 条件表达式优先级：`(A or B) if 条件 else C`。
    body = json.loads(call_args.kwargs.get("data") or call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs["data"])
    card_text = json.dumps(body)
    # 每个股票代码都应该出现在卡片内容里。
    for symbol in symbols:
        assert symbol in card_text


# Feature: sequoia-x-v2, Property 11: 飞书通知使用 ConfigManager 中的 Webhook URL
@given(
    # 按正则生成看起来合法的飞书 Webhook 地址。
    # fullmatch=True 要求整个字符串都匹配，而不是只匹配一部分。
    webhook_url=st.from_regex(r"https://open\.feishu\.cn/open-apis/bot/v2/hook/[a-z0-9\-]{8,36}", fullmatch=True)
)
@h_settings(max_examples=50)
def test_notification_uses_config_url(webhook_url: str) -> None:
    """属性 11：send() 发出的 HTTP 请求目标 URL 应等于 settings.feishu_webhook_url。"""
    settings = make_settings(webhook_url=webhook_url)
    notifier = FeishuNotifier(settings)

    with patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)
        notifier.send(symbols=["000001"], strategy_name="Test", webhook_key="default")

    # 取"第一个位置参数"，也就是 requests.post 的第一个参数 url。
    # 如果没有位置参数，就退回关键字参数里的 "url"。
    called_url = mock_post.call_args.args[0] if mock_post.call_args.args else mock_post.call_args.kwargs.get("url")
    assert called_url == webhook_url


# Feature: sequoia-x-v2, Property 12: HTTP 失败时记录 ERROR 日志
@given(status_code=st.integers(min_value=400, max_value=599))
@h_settings(max_examples=50)
def test_http_failure_logs_error(status_code: int) -> None:
    """属性 12：非 200 响应时，send() 应记录 ERROR 级别日志，不抛出异常。"""
    import logging as _logging
    import sequoia_x.notify.feishu as feishu_module

    settings = make_settings()
    notifier = FeishuNotifier(settings)

    # feishu logger 设置了 propagate=False，需直接在其上挂 handler
    # 如果不挂 handler，日志只会输出到终端，测试代码无法检查"有没有记录 ERROR"。
    feishu_logger = _logging.getLogger(feishu_module.__name__)
    log_records: list[_logging.LogRecord] = []

    # 自定义一个 handler，把每条日志记录追加到列表里。
    # 类名以下划线开头表示"只在内部使用"。
    class _ListHandler(_logging.Handler):
        def emit(self, record: _logging.LogRecord) -> None:
            # record 是 logging 的日志记录对象，里面有 levelno、msg 等信息。
            log_records.append(record)

    # 只接收 ERROR 及以上级别的日志。
    handler = _ListHandler(_logging.ERROR)
    feishu_logger.addHandler(handler)
    try:
        with patch("requests.post") as mock_post:
            # 伪造一个 4xx/5xx 响应，text 给一个非 JSON 的字符串。
            mock_post.return_value = MagicMock(status_code=status_code, text="error")
            notifier.send(symbols=["000001"], strategy_name="Test")
    finally:
        # 一定要移除自定义 handler，否则会影响后续测试，造成日志重复。
        feishu_logger.removeHandler(handler)

    # any(...) 只要列表里有一条 ERROR 日志就返回 True。
    assert any(r.levelno == _logging.ERROR for r in log_records)
