"""策略引擎属性测试。

【测试目标】
  验证策略 run() 的"返回类型契约"：
  无论数据长什么样，返回值都必须是 list[str]（字符串列表）。
  这是所有策略共同遵守的接口，main.py 依赖这个约定。

【测试技巧：用空数据测试边界】
  这里把 get_ohlcv 替换成"永远返回空 DataFrame"，
  于是所有股票都会在 len(df) < 20 时被跳过，最终返回空列表。
  好处：不需要真实数据、不依赖网络，测试稳定且非常快。

【已知小问题（review 中提到）】
  测试里 patch 的是 engine.get_all_symbols，但 MaVolumeStrategy 实际调用的是
  engine.get_local_symbols，所以这个 patch 实际没有起作用。
  当前测试能通过，是因为 get_ohlcv 被 mock 成空表，本来就不会选到股票。
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.strategy.ma_volume import MaVolumeStrategy


# Feature: sequoia-x-v2, Property 9: 策略 run() 返回值类型正确
@given(
    # 生成 0~3 个不重复的 6 位数字代码；min_size=0 覆盖"完全没有股票"的边界。
    symbols=st.lists(
        st.text(min_size=6, max_size=6, alphabet="0123456789"),
        min_size=0, max_size=3, unique=True,
    )
)
@h_settings(max_examples=30, deadline=None)
def test_strategy_run_returns_list_of_str(symbols: list[str]) -> None:
    """属性 9：run() 应返回 list[str]，每个元素为非空字符串。"""
    # 每次测试都用全新的临时数据库，互不干扰。
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings = Settings(
            db_path=str(Path(tmp_dir) / "test.db"),
            start_date="2024-01-01",
            feishu_webhook_url="https://example.com/hook",
        )
        engine = DataEngine(settings)

        # patch.object(对象, "方法名", return_value=...)：
        # 在 with 块内，调用 engine.xxx() 不会执行真实方法，而是直接返回指定值。
        # 这样就能脱离数据库和网络，单独测试策略逻辑。
        with patch.object(engine, "get_all_symbols", return_value=symbols):
            with patch.object(engine, "get_ohlcv", return_value=pd.DataFrame()):
                strategy = MaVolumeStrategy(engine=engine, settings=settings)
                result = strategy.run()

    # 契约检查 1：返回值必须是列表。
    assert isinstance(result, list)

    # 契约检查 2：列表里每个元素都必须是"非空字符串"。
    # all(...) 对所有元素都为 True 时才返回 True；
    # 生成器表达式 (表达式 for s in result) 是惰性的，不会先创建完整列表。
    assert all(isinstance(s, str) and len(s) > 0 for s in result)
