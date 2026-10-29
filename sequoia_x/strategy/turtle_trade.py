"""海龟交易策略：20日新高突破 + 成交额过亿 + 动量阳线过滤。

【策略逻辑】
  海龟交易法认为"价格创新高"代表趋势启动。A 股里很多突破是"高开低走"的诱多，
  所以这里额外加了两个过滤条件：
    - 必须是阳线（收盘价 > 开盘价）；
    - 必须真涨（今日收盘价 > 昨日收盘价）。
  最后按流通市值从大到小排序，优先推送大盘股。

【本文件涉及的 pandas 知识点】
  1. Series.shift(1)：把一列整体下移一行，用来取"前一天的值"。
  2. rolling(20).max()：滚动窗口最大值。
  3. pd.isna(x)：判断是否为 NaN。
  4. list.sort(key=..., reverse=True)：自定义排序。
  5. lambda：匿名函数，常用于 sort 的 key。
"""

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class TurtleTradeStrategy(BaseStrategy):
    """海龟交易策略（A股防诱多改良版）。

    选股条件（向量化，严禁 iterrows）：
    1. 突破新高：今日 close > 前20个交易日 high 的最大值
    2. 流动性：今日 turnover > 100,000,000
    3. 防诱多过滤：今日必须是实体阳线（今日 close > 今日 open），
       且必须真涨（今日 close > 昨日 close）

    Attributes:
        webhook_key: 路由到 'turtle' 专属飞书机器人。
    """

    webhook_key: str = "turtle"
    _MIN_BARS: int = 21  # 至少需要 21 根 K 线（20日窗口 + 当日）

    def _get_market_caps(self, symbols: list[str]) -> dict[str, float]:
        """通过 baostock 查询候选股票的流通市值（不复权收盘价 × 流通股本）。

        流通股本 = 成交量 / (换手率% / 100)
        流通市值 = 流通股本 × 不复权收盘价

        为什么用"不复权"价格？
          复权价格是被历史除权除息调整过的"虚拟价格"，
          乘上股本算出来的市值不是真实市值。所以这里 adjustflag="3"。

        Args:
            symbols: 需要查询的候选股票代码列表（通常只有几只，不会太慢）。

        Returns:
            {股票代码: 流通市值（元）}。查不到的股票不会出现在字典里。
        """
        from datetime import date

        import baostock as bs

        today_str = date.today().strftime("%Y-%m-%d")
        market_caps: dict[str, float] = {}

        # 这里调用的是 DataEngine 的"私有方法"（下划线开头）。
        # Python 没有真正的私有概念，这只是约定：外部尽量不要直接调用。
        # 更规范的做法是把代码转换函数提到公共工具模块。
        bs.login()
        try:
            for symbol in symbols:
                bs_code = self.engine._to_baostock_code(symbol)
                # 只取今天一天的 close（收盘价）、volume（成交量）、turn（换手率）。
                rs = bs.query_history_k_data_plus(
                    bs_code,
                    "close,volume,turn",
                    start_date=today_str,
                    end_date=today_str,
                    frequency="d",
                    adjustflag="3",  # 不复权，真实价格
                )
                while rs.next():
                    row = rs.get_row_data()
                    try:
                        # baostock 返回字符串，需要手动转成 float。
                        close = float(row[0])
                        volume = float(row[1])
                        turn = float(row[2])
                        if turn > 0:
                            # 换手率 = 成交量 / 流通股本 × 100%
                            # 反推：流通股本 = 成交量 / (换手率 / 100)
                            circulating_shares = volume / (turn / 100)
                            market_caps[symbol] = circulating_shares * close
                    except (ValueError, ZeroDivisionError):
                        # float("") / float("--") 会抛 ValueError；除零抛 ZeroDivisionError。
                        # 数据异常就跳过这只股票，不影响其他股票。
                        continue
        finally:
            # 无论如何都登出，避免连接泄漏。
            bs.logout()

        return market_caps

    def run(self) -> list[str]:
        """
        遍历全市场，返回满足海龟突破条件的股票代码列表。
        """
        symbols = self.engine.get_local_symbols()
        candidates: list[str] = []

        for symbol in symbols:
            try:
                df = self.engine.get_ohlcv(symbol)
                if len(df) < self._MIN_BARS:
                    continue

                # 向量化：前20日 high 的滚动最大值（不含当日，shift(1) 后取 rolling(20)）
                # 拆开看：
                #   df["high"].shift(1)   → 每行拿到"前一天的最高价"
                #   .rolling(20).max()    → 再取最近 20 个值里的最大值
                # 所以对于今天这一行，结果是"昨天及之前 20 天的最高价"，不含今天。
                # 必须排除今天，否则今天自己创的新高会参与比较，条件恒不成立。
                df["high_20"] = df["high"].shift(1).rolling(20).max()

                last = df.iloc[-1]  # 今天
                prev = df.iloc[-2]  # 获取昨日数据，用于对比

                # 数据不足 20 天时 high_20 是 NaN，pd.isna 判断后跳过。
                if pd.isna(last["high_20"]):
                    continue

                # 核心条件 1：突破前 20 天最高点
                breakout = last["close"] > last["high_20"]
                # 核心条件 2：流动性过亿（1 亿 = 100,000,000）
                # Python 数字里可以用下划线分隔，方便阅读，等价于 100000000。
                liquid = last["turnover"] > 100_000_000

                # 【新增防守条件】拒绝郑州煤电式的高开低走大阴线！
                is_yang = last["close"] > last["open"]   # 实体必须是阳线（红柱）
                is_up = last["close"] > prev["close"]    # 必须是真涨，不能是假阳线

                if breakout and liquid and is_yang and is_up:
                    candidates.append(symbol)

            except Exception as exc:
                logger.warning(f"[{symbol}] TurtleTradeStrategy 计算失败：{exc}")
                continue

        # 按流通市值从大到小排序
        if candidates:
            market_caps = self._get_market_caps(candidates)
            # sort(key=函数) 会拿列表里的每个元素去调用 key 函数，
            # 按返回值排序。lambda s: ... 是一个匿名函数，等价于：
            #   def key_func(s):
            #       return market_caps.get(s, 0)
            # reverse=True 表示从大到小。
            # market_caps.get(s, 0)：查不到市值时当 0 处理，排在最后。
            candidates.sort(key=lambda s: market_caps.get(s, 0), reverse=True)

        logger.info(f"TurtleTradeStrategy 选出 {len(candidates)} 只股票")
        return candidates
