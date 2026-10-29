"""RPS 极强动量突破策略：选相对强度排名前 10% 且接近 120 日高点的股票。

【什么是 RPS？】
  RPS（Relative Price Strength，相对价格强度）来自欧奈尔 CANSLIM 体系：
  先算每只股票过去 N 天的涨幅，再把全市场涨幅从低到高排名，
  排名百分位就是 RPS。RPS = 95 表示"跑赢了 95% 的股票"。

【和其他策略的区别】
  其他策略都是"逐只股票读数据 → 判断"；
  本策略反过来：一次性把整张表读进内存，用 groupby 批量计算全市场排名。
  数据量大时内存占用更高，但可以方便地做"横向比较"（rank）。

【本文件涉及的 pandas 知识点】
  1. groupby("symbol")：按股票分组，组内计算互不干扰。
  2. shift(120)：组内把收盘价下移 120 行，用来取 120 天前的价格。
  3. rank(pct=True)：把数值转成 0~1 的百分位排名。
  4. rolling(...).max() + reset_index：分组滚动计算后重新对齐索引。
  5. merge：按 symbol 列把两张表"拼"在一起（类似 SQL JOIN）。
  6. 布尔掩码：df[df["rps"] >= 90] 只保留满足条件的行。
"""

import pandas as pd
import sqlite3

from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)


class RpsBreakoutStrategy(BaseStrategy):
    """RPS 极强动量突破策略

    Attributes:
        webhook_key: 路由到 'rps' 专属飞书机器人。
        rps_period: 计算涨幅的回看周期（120 个交易日，约半年）。
        rps_threshold: RPS 阈值，90 表示只保留排名前 10% 的股票。
    """

    webhook_key: str = "rps"
    rps_period: int = 120
    rps_threshold: int = 90

    def run(self) -> list[str]:
        """执行 RPS 计算，返回满足条件的股票代码列表。

        Returns:
            满足条件的股票代码列表；数据库读取失败时返回空列表。
        """
        try:
            # 直接连接数据库，一次性读取所有股票的 symbol/date/close/high。
            # 注意：这里用 sqlite3.connect 而不是 engine.get_ohlcv()，
            # 因为需要"全市场一起算"，逐只读取反而更麻烦。
            with sqlite3.connect(self.engine.db_path) as conn:
                df = pd.read_sql("SELECT symbol, date, close, high FROM stock_daily", conn)
        except Exception as exc:
            logger.error(f"读取数据库失败: {exc}")
            return []

        if df.empty:
            return []

        # 把字符串日期转成 datetime 类型，才能正确排序和取最大值。
        df['date'] = pd.to_datetime(df['date'])
        # 先按股票、再按日期排序，保证每只股票内部是按时间顺序排列的。
        df = df.sort_values(['symbol', 'date'])

        # 纵向计算涨幅
        # groupby('symbol')['close'].shift(120)：
        #   在每只股票内部，把收盘价往下移 120 行；
        #   于是当前行的 close_shift 就是"120 个交易日前的收盘价"。
        #   前 120 行移不满，结果是 NaN。
        df['close_shift'] = df.groupby('symbol')['close'].shift(self.rps_period)

        # 区间涨幅 = (当前价 - 120 天前价格) / 120 天前价格。
        # 若 close_shift 为 0，结果可能是 inf；若为 NaN，结果也是 NaN。
        df['pct_change'] = (df['close'] - df['close_shift']) / df['close_shift']

        # 全市场最新交易日（通常是今天，或最近一个交易日）。
        latest_date = df['date'].max()

        # df[df['date'] == latest_date]：布尔掩码，只保留最新一天的所有股票。
        # .copy() 复制一份，避免后续修改时触发 pandas 的 SettingWithCopyWarning。
        latest_df = df[df['date'] == latest_date].copy()

        # 涨幅为 NaN 的股票（上市不足 120 天）不参与排名。
        latest_df = latest_df.dropna(subset=['pct_change'])

        # 横向排位 (RPS)
        # rank(pct=True) 把涨幅转成 0~1 的百分位：最小的约 0，最大的 1。
        # 乘 100 后就是 0~100 的分数。
        latest_df['rps'] = latest_df['pct_change'].rank(pct=True) * 100
        # 只保留 RPS ≥ 90 的股票（前 10%）。
        strong_stocks = latest_df[latest_df['rps'] >= self.rps_threshold].copy()

        # 计算滚动最高价
        # groupby(...).rolling(window=120).max()：
        #   对每只股票取最近 120 天的最高价。
        #   结果是一个 MultiIndex（多级索引）的 Series：
        #       第一级是 symbol，第二级是原始行索引；
        #   reset_index(level=0, drop=True)：丢掉 symbol 这一级索引，
        #   只保留原始行索引，方便后面直接赋值回 df。
        # min_periods=60：数据不足 120 天时，只要满 60 天也算，
        #   否则新股的 roll_high 全是 NaN。
        roll_high = df.groupby('symbol')['high'].rolling(
            window=self.rps_period, min_periods=self.rps_period // 2
        ).max().reset_index(level=0, drop=True)

        # 把滚动最高价写回原表（按行索引自动对齐）。
        df['roll_high'] = roll_high

        # 取出"最新一天"每只股票的高点，作为一张只含 symbol/roll_high 的小表。
        latest_roll_high = df[df['date'] == latest_date][['symbol', 'roll_high']]

        # merge 类似 SQL JOIN：按 symbol 把高点拼到 strong_stocks 上，
        # 得到"强势股 + 各自 120 日高点"的完整表。
        strong_stocks = strong_stocks.merge(latest_roll_high, on='symbol')

        # 突破判定
        # 这里其实是"当前价不低于 120 日高点的 90%"，
        # 属于"接近新高"而非严格意义上的"突破新高"。
        breakout_condition = strong_stocks['close'] >= strong_stocks['roll_high'] * 0.90
        selected = strong_stocks[breakout_condition]

        logger.info(f"RpsBreakoutStrategy 选出 {len(selected)} 只股票")
        # Series.tolist() 把 pandas 的列转成普通 Python 列表。
        return selected['symbol'].tolist()
