"""高旗形整理策略：强动量后极度收敛缩量。

【策略逻辑（"高而窄的旗形"）】
  一只股票先快速上涨（旗杆），然后在高位横盘、波动收窄、成交量萎缩（旗面），
  通常被视为"上涨中继"形态。本策略用三个条件近似识别：
    1. 40 天内最高/最低价之比 > 1.6 → 前期涨幅足够大；
    2. 最近 10 天最高/最低价之比 < 1.15 → 近期振幅很小；
    3. 今天成交量 < 过去 20 日均量的 0.6 倍 → 明显缩量。
  另外加了一个 high_level 条件，确保横盘位置仍在高位。

【本文件涉及的 pandas 知识点】
  1. df.tail(n)：取最后 n 行。
  2. Series.max() / .min()：整列最大/最小值。
  3. iloc[-21:-1]：切片取"倒数第 21 行到倒数第 2 行"（不含最后一行）。
  4. 链式比较与布尔变量组合。
"""

import pandas as pd  # noqa: F401  # 本文件未直接使用 pd，保留依赖导入

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class HighTightFlagStrategy(BaseStrategy):
    """高旗形整理策略。

    选股条件（向量化，严禁 iterrows）：
    1. 强动量：过去40天区间最高价 / 区间最低价 > 1.6（涨幅超60%）
    2. 极度收敛：最近10天区间最高价 / 区间最低价 < 1.15（振幅低于15%）
    3. 缩量：今日 volume < 过去20日 volume 均值的 0.6 倍

    Attributes:
        webhook_key: 路由到 'flag' 专属飞书机器人。
    """

    webhook_key: str = "flag"
    _MIN_BARS: int = 40  # 至少需要 40 根 K 线

    def run(self) -> list[str]:
        """
        遍历全市场，返回满足高旗形整理条件的股票代码列表。

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

                # 向量化计算各窗口指标
                # tail(40) 取最近 40 行（不足 40 行时返回全部）；
                # tail(10) 取最近 10 行。
                tail40 = df.tail(40)
                tail10 = df.tail(10)

                # 区间最高价 / 最低价：类似"区间涨幅"的粗略估计。
                high40 = tail40["high"].max()
                low40 = tail40["low"].min()
                high10 = tail10["high"].max()
                low10 = tail10["low"].min()

                # 防御性检查：价格为 0 会导致除零，直接跳过。
                if low40 == 0 or low10 == 0:
                    continue

                # 条件 1：强动量
                # 区间最高 / 最低 > 1.6，说明 40 天内有过一波约 60% 的上涨。
                momentum = high40 / low40 > 1.6

                # 条件 2：极度收敛
                # 最近 10 天最高 / 最低 < 1.15，说明近期波动收窄（旗面）。
                consolidation = high10 / low10 < 1.15

                # 条件 3：高位抗跌（近10天最低点不得低于40天最高点的80%）
                # 防止把"已经跌了一大截又横盘"的形态误判为高位旗形。
                high_level = low10 >= high40 * 0.8

                # 条件 4：缩量（向量化均值）
                # 切片 df["volume"].iloc[-21:-1]：
                #   -21 表示倒数第 21 个元素；
                #   -1  表示最后一个元素，但切片"左闭右开"，所以不包含它。
                # 结果正好是"今天之前的 20 个交易日成交量"，避免今天自己影响均值。
                vol_ma20 = df["volume"].iloc[-21:-1].mean()

                # iloc[-1] 是今天；今天量 < 前面 20 天均量 × 0.6 即视为缩量。
                shrink = df["volume"].iloc[-1] < vol_ma20 * 0.6

                if momentum and consolidation and high_level and shrink:
                    selected.append(symbol)

            except Exception as exc:
                logger.warning(f"[{symbol}] HighTightFlagStrategy 计算失败：{exc}")
                continue

        logger.info(f"HighTightFlagStrategy 选出 {len(selected)} 只股票")
        return selected
