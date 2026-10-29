"""涨停洗盘策略：昨日涨停后今日放量收阴但不破昨收。

【策略逻辑】
  涨停后出现放量阴线，可能是资金出货，也可能是"洗盘"（把不坚定的筹码洗出去）。
  这里用"不跌破昨日收盘价"来判断更像洗盘：
    1. 昨日相对前日涨停（涨幅 ≥ 9.5%）；
    2. 今日收阴（收盘 < 开盘）；
    3. 今日成交量是昨日的 2 倍以上；
    4. 今日最低价仍不低于昨日收盘价（支撑有效）。

【本文件涉及的 pandas 知识点】
  1. iloc[-3] / iloc[-2] / iloc[-1]：按位置取三天数据。
  2. 从 Series 里用列名取值：row["close"]。
  3. 所有条件先用变量存起来，最后统一用 and 组合，方便阅读和调试。
"""

import pandas as pd  # noqa: F401  # 本文件未直接使用 pd，保留依赖导入

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class LimitUpShakeoutStrategy(BaseStrategy):
    """涨停洗盘策略。

    选股条件（向量化，严禁 iterrows）：
    1. 昨日涨停：昨日 close >= 前日 close * 1.095
    2. 今日收阴：今日 close < 今日 open
    3. 今日放量：今日 volume > 昨日 volume * 2.0
    4. 支撑不破：今日 low >= 昨日 close

    Attributes:
        webhook_key: 路由到 'shakeout' 专属飞书机器人。
    """

    webhook_key: str = "shakeout"
    _MIN_BARS: int = 3  # 至少需要 3 根 K 线（前日、昨日、今日）

    def run(self) -> list[str]:
        """
        遍历全市场，返回满足涨停洗盘条件的股票代码列表。

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

                # 取最近三根 K 线（向量化索引，无 iterrows）
                # 索引为 -3 表示倒数第三个，-1 是最后一个（今天）。
                prev2 = df.iloc[-3]  # 前日
                prev1 = df.iloc[-2]  # 昨日
                today = df.iloc[-1]  # 今日

                # 条件 1：昨日涨停
                # A 股主板涨停约 10%，考虑四舍五入用 9.5% 作为阈值。
                # 这里写的是 >= 1.095，即涨幅约 9.5% 以上就算涨停。
                limit_up_yesterday = prev1["close"] >= prev2["close"] * 1.095

                # 条件 2：今日收阴
                bearish_today = today["close"] < today["open"]

                # 条件 3：今日放量（超过昨日成交量的 2 倍）
                volume_surge = today["volume"] > prev1["volume"] * 2.0

                # 条件 4：支撑不破（今日最低价不低于昨日收盘价）
                support_hold = today["low"] >= prev1["close"]

                if limit_up_yesterday and bearish_today and volume_surge and support_hold:
                    selected.append(symbol)

            except Exception as exc:
                logger.warning(f"[{symbol}] LimitUpShakeoutStrategy 计算失败：{exc}")
                continue

        logger.info(f"LimitUpShakeoutStrategy 选出 {len(selected)} 只股票")
        return selected
