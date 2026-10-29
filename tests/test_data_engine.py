"""数据引擎属性测试。

【测试思路】
  不连真实数据库、不连网络，而是：
    1. 用 tempfile 创建一个临时目录；
    2. 在临时目录里建一个 test.db；
    3. 测试结束后临时目录自动删除。
  这样测试可以反复运行，不会污染真实数据。

【本文件涉及的 Python 知识点】
  1. tempfile.TemporaryDirectory()：with 结束时递归删除临时目录。
  2. Path(tmp_dir) / "test.db"：用 / 拼接路径（pathlib 的语法糖）。
  3. sqlite3.IntegrityError：违反 UNIQUE 约束等完整性错误。
  4. hypothesis 的 st.dates()：生成随机日期。
"""

import sqlite3
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine


def make_engine_in(tmp_dir: str) -> tuple[DataEngine, Settings]:
    """创建使用临时数据库的 DataEngine 实例。

    这是一个"测试辅助函数"（不是测试本身，因为名字不以 test_ 开头）。
    返回元组 (engine, settings)，调用方可以用 `engine, _ = ...` 解包，
    用下划线 _ 表示"这个值我不关心"。
    """
    settings = Settings(
        db_path=str(Path(tmp_dir) / "test.db"),
        start_date="2024-01-01",
        # 测试里不需要真实 Webhook，随便给一个合法 URL 满足必填校验即可。
        feishu_webhook_url="https://example.com/hook",
    )
    engine = DataEngine(settings)
    return engine, settings


# Property 4: (symbol, date) 唯一约束防止重复写入
@given(
    # 生成 6 位纯数字字符串作为股票代码。
    symbol=st.text(min_size=6, max_size=6, alphabet="0123456789"),
    # 生成 2024-01-01 ~ 2025-12-31 之间的随机日期。
    trade_date=st.dates(min_value=date(2024, 1, 1), max_value=date(2025, 12, 31)),
)
# deadline=None：关闭"单个用例执行时间上限"检查。
# 因为每次都会创建临时数据库，偶尔会比较慢，避免误报超时。
@h_settings(max_examples=50, deadline=None)
def test_unique_symbol_date_constraint(symbol: str, trade_date: date) -> None:
    """相同 (symbol, date) 插入两次，数据库中该组合记录数应保持为 1。"""
    # with 结束时，临时目录（以及里面的 test.db）会被自动删除。
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)

        # 构造一行模拟数据。DataFrame 需要列表包字典：每个字典是一行。
        row = {
            "symbol": symbol, "date": str(trade_date),
            "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
            "volume": 1000.0, "turnover": 10500.0,
        }
        df = pd.DataFrame([row])

        with sqlite3.connect(engine.db_path) as conn:
            # 第一次插入：正常写入。
            df.to_sql("stock_daily", conn, if_exists="append", index=False, method="multi")
            try:
                # 第二次插入同样的 (symbol, date)：
                # 数据库表上有 UNIQUE(symbol, date) 约束，会抛 IntegrityError。
                # 这里用 try/except 接住，测试的是"约束确实生效了"。
                df.to_sql("stock_daily", conn, if_exists="append", index=False, method="multi")
            except sqlite3.IntegrityError:
                pass

            # 统计这个组合在表里有几行；如果唯一约束正常，应该是 1。
            count = conn.execute(
                "SELECT COUNT(*) FROM stock_daily WHERE symbol=? AND date=?",
                (symbol, str(trade_date)),
            ).fetchone()[0]

        assert count == 1
