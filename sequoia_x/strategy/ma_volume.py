"""均线+成交量选股策略：5日均线上穿20日均线且成交量放大。

【策略逻辑】
  均线金叉是经典的趋势转多信号：
    - 5 日均线（短期成本）上穿 20 日均线（中期成本），说明短期买盘开始占优；
    - 同时成交量放大，说明有资金真金白银参与，而不是无量反弹。

【本文件涉及的 pandas 知识点】
  1. Series.rolling(窗口).mean()：滚动均值，也就是"移动平均线"。
  2. DataFrame.iloc[-1] / iloc[-2]：按"位置"取行，-1 表示最后一行。
  3. 布尔 Series：df["close"] > 10 得到一列 True/False，可用于条件组合。
  4. NaN：rolling 窗口不足时结果是 NaN（缺失值），比较运算通常返回 False。
"""

import pandas as pd  # noqa: F401  # 本文件未直接使用 pd，保留依赖导入

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class MaVolumeStrategy(BaseStrategy):
    """均线+成交量选股策略。

    选股条件（全部向量化，严禁 iterrows）：
    1. 5日收盘均线上穿20日收盘均线（金叉）
    2. 当日成交量 > 20日均量的 1.5 倍（放量确认）

    Attributes:
        webhook_key: 路由到 'ma_volume' 专属飞书机器人。
    """

    # 覆盖父类的类属性：推送时 Settings 会去找 STRATEGY_WEBHOOK_MA_VOLUME。
    webhook_key: str = "ma_volume"

    def run(self) -> list[str]:
        """
        遍历全市场，返回满足均线金叉+放量条件的股票代码列表。

        Returns:
            满足条件的股票代码列表。
        """
        # 只看本地数据库里已有的股票（由 backfill 决定范围）。
        symbols = self.engine.get_local_symbols()
        # 准备一个空列表，后面用 append 收集满足条件的代码。
        selected: list[str] = []

        # 逐只股票计算。虽然逐只查询数据库在性能上不是最优，
        # 但逻辑简单、彼此隔离：某只股票出错不会影响其他股票。
        for symbol in symbols:
            try:
                # get_ohlcv 返回按日期升序排列的 DataFrame。
                # 列：id/symbol/date/open/high/low/close/volume/turnover
                df = self.engine.get_ohlcv(symbol)

                # 20 日均线至少需要 20 根 K 线。
                # 严格来说判断"昨日/今日金叉"需要 21 根（原因见下方注释）。
                if len(df) < 20:
                    continue

                # ── 向量化计算均线和成交量均值 ──
                # rolling(5).mean()：对每一行，取它和它前面 4 行共 5 个值求平均。
                # 前 4 行的窗口不够 5 个值，结果自动是 NaN。
                df["ma5"] = df["close"].rolling(5).mean()
                df["ma20"] = df["close"].rolling(20).mean()
                df["vol_ma20"] = df["volume"].rolling(20).mean()

                # 取最后两行判断金叉（昨日 ma5 < ma20，今日 ma5 > ma20）
                # iloc 按位置取行；返回的是一个 Series（可以理解为"一行数据"）。
                # iloc[-1] = 最后一行（今天），iloc[-2] = 倒数第二行（昨天）。
                last = df.iloc[-1]
                prev = df.iloc[-2]

                # 金叉 = 昨天还在下方，今天穿到上方。
                # 两个条件用 and 连接；注意在 pandas 里组合整列条件要用 &，
                # 但这里取的是单个数值，所以普通 and 即可。
                golden_cross = (
                    prev["ma5"] < prev["ma20"]
                    and last["ma5"] > last["ma20"]
                )

                # 今日成交量 > 20 日均量的 1.5 倍。
                # 注意 vol_ma20 包含今天自己，属于常见但略粗糙的写法。
                volume_surge = last["volume"] > last["vol_ma20"] * 1.5

                if golden_cross and volume_surge:
                    selected.append(symbol)

            except Exception as exc:
                # 单只股票异常只记录警告，不中断整个策略。
                # f"[{symbol}] ..." 会在日志里标出是哪只股票出的问题。
                logger.warning(f"[{symbol}] 策略计算失败：{exc}")
                continue

        logger.info(f"MaVolumeStrategy 选出 {len(selected)} 只股票")
        return selected

    # 【学习备注】
    # 如果 df 恰好只有 20 行：
    #   - prev 是第 19 行，它的 ma20 窗口只有 19 个值 → 结果是 NaN；
    #   - NaN < NaN 的结果是 False，所以"金叉"永远不会成立；
    #   - 程序不会报错，但会漏掉本应选中的股票。
    # 因此更严谨的边界应该是 len(df) < 21。这里保留原逻辑，仅作说明。
