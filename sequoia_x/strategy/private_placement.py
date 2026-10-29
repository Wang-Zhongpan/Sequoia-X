"""定增公告监控策略：推送最近发布的定向增发公告。

【策略逻辑】
  这是一个"事件驱动"策略，不依赖本地 K 线数据库：
  直接调用 akshare 的东方财富接口，拉取全部增发数据，
  筛选"定向增发 + 发行日期在最近 7 天内"的股票。

【和 README 的差异】
  项目其他数据都走 baostock，只有这里用了 akshare（东方财富）。
  如果东财接口改版或限流，本策略会失败；好在异常被捕获，
  只会返回空列表，不影响其他策略。

【本文件涉及的 pandas / 正则知识点】
  1. df[df["列"] == 值]：布尔过滤。
  2. pd.to_datetime(errors="coerce")：转换失败的值变成 NaT（时间版的 NaN）。
  3. .dt.date：从 datetime 列里取出纯日期部分。
  4. sort_values(ascending=False)：降序排序。
  5. Series.str.extract(正则)：从字符串里抽取匹配部分。
  6. set + 列表：去重且保留原始顺序。
"""

from datetime import date, timedelta

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class PrivatePlacementStrategy(BaseStrategy):
    """定增公告监控策略。

    数据源：akshare stock_qbzf_em()（东方财富-全部增发）
    逻辑：筛选最近 7 天内发行日期的定向增发公告，推送至飞书。

    Attributes:
        webhook_key: 路由到 'private_placement' 飞书机器人。
    """

    webhook_key: str = "private_placement"
    _LOOKBACK_DAYS: int = 7  # 回看天数，覆盖一周内的新公告

    def run(self) -> list[str]:
        """拉取定增公告，返回近期有定向增发的股票代码列表。"""
        try:
            # 局部导入 akshare：它体积较大、加载较慢，
            # 只有真正需要这个策略时才导入，可以加快程序启动。
            import akshare as ak

            # stock_qbzf_em 返回一个 DataFrame，列名是中文，
            # 例如：股票代码、股票简称、发行方式、发行日期、发行价格……
            # 注意：akshare 的列名可能随版本变化，升级依赖后要回归测试。
            df = ak.stock_qbzf_em()
        except Exception as exc:
            # 网络失败、接口改版、字段缺失都在这里统一兜底。
            logger.error(f"PrivatePlacementStrategy 获取定增数据失败：{exc}")
            return []

        # `or` 短路求值：df 为 None 时不会再去访问 df.empty，避免 AttributeError。
        if df is None or df.empty:
            logger.info("PrivatePlacementStrategy 无定增数据")
            return []

        # 只保留定向增发（排除公开增发）
        # df["发行方式"] == "定向增发" 得到一列 True/False，
        # 再用它做行筛选，只留下 True 的行。
        df = df[df["发行方式"] == "定向增发"]

        if df.empty:
            return []

        # 按发行日期过滤：只保留最近 N 天内的公告
        today = date.today()
        # timedelta 表示"时间间隔"：today - 7 天 = 一周前的日期。
        cutoff = today - timedelta(days=self._LOOKBACK_DAYS)

        # to_datetime 把字符串日期转成 datetime；
        # errors="coerce" 表示转不了的值变成 NaT，而不是抛异常。
        df["发行日期"] = pd.to_datetime(df["发行日期"], errors="coerce")

        # 丢掉日期无效的行（NaT 也是缺失值，dropna 能处理）。
        df = df.dropna(subset=["发行日期"])

        # .dt.date 取出日期部分（去掉时分秒），再和 cutoff 比较。
        # 比较结果是布尔列，用于筛选。
        df = df[df["发行日期"].dt.date >= cutoff]

        if df.empty:
            logger.info("PrivatePlacementStrategy 近期无新定增公告")
            return []

        # 按发行日期降序（最新的在前）
        df = df.sort_values("发行日期", ascending=False)

        # 提取股票代码（去掉可能的前缀，保留纯数字）
        # astype(str)：确保每行都是字符串；
        # .str.extract(r"(\d{6})")：用正则匹配连续 6 位数字；
        #   \d 表示数字，{6} 表示正好 6 个；括号表示"捕获这一组"；
        #   [0] 取第一列捕获结果；
        # dropna() 丢掉匹配失败的 NaT/NaN；
        # tolist() 转成普通列表。
        symbols = df["股票代码"].astype(str).str.extract(r"(\d{6})")[0].dropna().tolist()

        # 去重（同一只票可能有多次定增）
        # 用 set 记录"已经见过的代码"，查找是 O(1)，比 list 快。
        # 同时用列表按原顺序保存结果——直接用 set 会丢失顺序。
        seen = set()
        unique_symbols = []
        for s in symbols:
            if s not in seen:
                seen.add(s)
                unique_symbols.append(s)

        logger.info(f"PrivatePlacementStrategy 选出 {len(unique_symbols)} 只股票")
        return unique_symbols
