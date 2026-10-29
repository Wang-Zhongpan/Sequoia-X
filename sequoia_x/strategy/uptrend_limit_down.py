"""上升趋势跌停策略：趋势中放量跌停，捕捉错杀机会。

【策略逻辑】
  长期均线多头排列说明趋势向上；此时突然放量跌停，
  可能是恐慌性错杀，而不是基本面反转。条件：
    1. 昨日 20 日均线 > 60 日均线（中期趋势向上）；
    2. 今日跌幅 ≥ 9.5%（跌停附近）；
    3. 今日成交量 > 20 日均量的 2 倍（放量）。

【本文件涉及的 pandas 知识点】
  1. rolling(20).mean() / rolling(60).mean()：不同周期的移动平均。
  2. pd.isna()：判断 NaN，避免用缺失值参与比较。
  3. 用变量保存中间结果，最后组合。
"""

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class UptrendLimitDownStrategy(BaseStrategy):
    """上升趋势跌停策略。

    选股条件（向量化，严禁 iterrows）：
    1. 处于上升趋势：昨日20日均线 > 昨日60日均线
    2. 放量跌停：今日 close <= 昨日 close * 0.905
                且今日 volume > 20日均量的 2.0 倍

    Attributes:
        webhook_key: 路由到 'limit_down' 专属飞书机器人。
    """

    webhook_key: str = "limit_down"
    _MIN_BARS: int = 60  # 至少需要 60 根 K 线（60日均线）

    def run(self) -> list[str]:
        """
        遍历全市场，返回满足上升趋势跌停条件的股票代码列表。

        Returns:
            满足条件的股票代码列表。
        """
        symbols = self.engine.get_local_symbols()
        selected: list[str] = []

        for symbol in symbols:
            try:
                df = self.engine.get_ohlcv(symbol)
                if len(df) < self._MIN_BARS:
                    continue

                # 向量化计算均线
                # 注意：rolling 结果的前 N-1 行是 NaN，因为窗口数据不够。
                df["ma20"] = df["close"].rolling(20).mean()
                df["ma60"] = df["close"].rolling(60).mean()
                df["vol_ma20"] = df["volume"].rolling(20).mean()

                prev = df.iloc[-2]  # 昨日
                today = df.iloc[-1]  # 今日

                # 用 pd.isna 检查关键值是否为空。
                # 如果昨日的 ma60 还是 NaN（数据不足），比较结果没有意义，直接跳过。
                if pd.isna(prev["ma20"]) or pd.isna(prev["ma60"]) or pd.isna(today["vol_ma20"]):
                    continue

                # 条件 1：上升趋势（昨日均线多头排列）
                # 20 日均线在 60 日均线上方，说明近一个月强于近三个月。
                uptrend = prev["ma20"] > prev["ma60"]

                # 条件 2：放量跌停
                # 跌幅约 9.5% 以上（0.905 = 1 - 9.5%）。
                limit_down = today["close"] <= prev["close"] * 0.905

                # 今日成交量 > 20 日均量的 2 倍。
                volume_surge = today["volume"] > today["vol_ma20"] * 2.0

                if uptrend and limit_down and volume_surge:
                    selected.append(symbol)

            except Exception as exc:
                logger.warning(f"[{symbol}] UptrendLimitDownStrategy 计算失败：{exc}")
                continue

        logger.info(f"UptrendLimitDownStrategy 选出 {len(selected)} 只股票")
        return selected
