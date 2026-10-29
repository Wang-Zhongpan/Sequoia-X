"""数据引擎模块：负责 SQLite 行情数据存储与 baostock 增量同步。

【这个文件在项目里的位置】
  上层策略（strategy/*.py）不直接接触网络和数据库，只调用 DataEngine 的两个入口：
      get_local_symbols() → 本地有哪些股票
      get_ohlcv(symbol)   → 某只股票的全部日 K 数据
  数据的获取、清洗、入库都封装在本文件里。这叫"分层"，好处是：
      - 换数据源时只改这里；
      - 策略代码可以脱离网络单独测试。

【本文件涉及的 Python 知识点】
  1. sqlite3：Python 内置的 SQLite 数据库驱动，无需安装额外服务。
  2. 上下文管理器 with：进入时建立资源，退出时自动提交/回滚（注意：不会自动关闭连接）。
  3. 参数化查询 `?`：把变量安全地传给 SQL，避免 SQL 注入和引号转义问题。
  4. pandas：DataFrame 是"带行索引和列名的二维表格"，本文件用它做数据清洗。
  5. 多进程 multiprocessing.Pool：把任务分给多个进程并行执行，绕开 Python GIL。
  6. enumerate / 切片步长 tasks[i::n] / try-finally / 指数退避重试。
"""

import sqlite3
from pathlib import Path

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger

# 模块级 logger：本文件所有日志都用它，名字是 "sequoia_x.data.engine"。
logger = get_logger(__name__)


# ── 建表 SQL ──
# CREATE TABLE IF NOT EXISTS：表已存在时不报错，方便反复执行。
# UNIQUE (symbol, date)：同一只股票的同一天只能有一条记录，
#   pandas 的 to_sql 重复写入时会触发 IntegrityError（下面有捕获）。
# 字段含义：open/high/low/close = 开高低收，volume = 成交量，turnover = 成交额。
_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stock_daily (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol   TEXT    NOT NULL,
    date     TEXT    NOT NULL,
    open     REAL,
    high     REAL,
    low      REAL,
    close    REAL,
    volume   REAL,
    turnover REAL,
    UNIQUE (symbol, date)
);
"""

# 索引：加速 "WHERE symbol = ? ORDER BY date" 这类查询。
_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_symbol_date ON stock_daily (symbol, date);
"""


def _bs_fetch_batch(tasks: list) -> list:
    """多进程 worker：独立 login，批量拉取 baostock 数据。

    【为什么要写成模块级函数？】
      Windows / macOS 默认用 "spawn" 方式启动子进程：子进程会重新导入本模块，
      然后通过 pickle 反序列化要执行的函数。只有定义在模块顶层的函数才能被 pickle。
      如果把 worker 写成类方法或闭包，multiprocessing 在 Windows 上会报
      "Can't pickle local object"。

    【为什么每个进程都要 login？】
      baostock 的连接不是线程/进程安全的，不能跨进程共享，
      所以每个子进程自己登录、自己拉取、自己退出。

    Args:
        tasks: 形如 [(symbol, bs_code, start_date, end_date), ...] 的任务列表。

    Returns:
        list[list[str]]：每行是 [symbol, date, open, high, low, close, volume, amount]。
        注意 baostock 返回的都是字符串，后面统一用 pandas 转成数字。
    """
    import baostock as bs

    # 子进程内登录 baostock。返回值这里没有检查，失败时后续查询会返回错误码。
    bs.login()
    results = []
    for symbol, bs_code, start, end in tasks:
        # query_history_k_data_plus 是 baostock 的历史 K 线接口。
        # adjustflag="1" 表示后复权：历史价格保持不变，适合增量存储（详见 README）。
        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,open,high,low,close,volume,amount",
            start_date=start,
            end_date=end,
            frequency="d",
            adjustflag="1",  # 后复权
        )
        # error_code 是字符串 "0" 表示成功。这里失败会静默跳过（没有日志），
        # 这是 review 中提到的一个风险点，后续可以改成记录失败数量。
        if rs.error_code != "0":
            continue
        # rs.next() 是游标式读取：每次调用前进一行，返回 False 表示读完了。
        # rs.get_row_data() 返回当前行的列表，例如 ["2024-01-02", "10.0", ...]。
        while rs.next():
            # [symbol] + 列表 → 把 symbol 拼到这一行的最前面。
            results.append([symbol] + rs.get_row_data())
    bs.logout()
    return results


class DataEngine:
    """行情数据引擎，负责 SQLite 存储和 baostock 数据同步。"""

    def __init__(self, settings: Settings) -> None:
        """初始化：保存配置并确保数据库表存在。

        Args:
            settings: 全局配置对象，提供 db_path 和 start_date。
        """
        self.db_path: str = settings.db_path
        self.start_date: str = settings.start_date
        # 每次创建引擎都执行一次建表语句，因为语句里有 IF NOT EXISTS，所以是安全的。
        self._init_db()

    def _init_db(self) -> None:
        """创建数据库文件、数据表和索引（已存在则跳过）。"""
        # Path("data/sequoia_v2.db").parent 是 "data" 目录。
        # mkdir(parents=True, exist_ok=True)：
        #   parents=True  → 多层目录都会创建，例如 a/b/c；
        #   exist_ok=True → 目录已存在时不报错。
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        # `with sqlite3.connect(...) as conn:` 的行为：
        #   - 正常结束时自动 commit()；
        #   - 抛异常时自动 rollback()；
        #   - 但注意它 **不会自动 close()**，连接要等垃圾回收时才关闭。
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_INDEX_SQL)
            # 显式 commit 与 with 的自动提交重复，但写出来更直观。
            conn.commit()
        logger.info(f"数据库初始化完成：{self.db_path}")

    def _get_last_date(self, symbol: str) -> str | None:
        """查询某只股票在本地库中的最新日期；没有数据时返回 None。

        `?` 是参数占位符，第二个参数 (symbol,) 是元组。
        注意单元素元组必须写成 (symbol,)，只写 (symbol) 会被当成普通括号。
        使用参数化查询可以自动处理引号转义，避免 SQL 注入。
        """
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT MAX(date) FROM stock_daily WHERE symbol = ?",
                (symbol,),
            ).fetchone()  # fetchone() 返回第一行元组，如 ("2024-01-31",)；无结果返回 None
        # `row and row[0]`：先确认 row 不是 None，再取第一列。
        # SQL 的 MAX(date) 在无匹配行时返回 NULL，pandas/sqlite 会给出 None。
        return row[0] if row and row[0] else None

    def get_ohlcv(self, symbol: str) -> pd.DataFrame:
        """读取某只股票的全部日 K 线，按日期升序排列。

        Args:
            symbol: 纯数字代码，如 "000001"。

        Returns:
            pandas.DataFrame，列包括 id/symbol/date/open/high/low/close/volume/turnover。
            注意这里的 date 是字符串（"YYYY-MM-DD"），不是 datetime 类型。
        """
        with sqlite3.connect(self.db_path) as conn:
            # pd.read_sql 直接把 SQL 结果转成 DataFrame。
            # params 用于替换 SQL 里的 ?，同样是参数化查询。
            df = pd.read_sql(
                "SELECT * FROM stock_daily WHERE symbol = ? ORDER BY date",
                conn,
                params=(symbol,),
            )
        return df

    @staticmethod
    def _to_baostock_code(symbol: str) -> str:
        """将纯数字代码转为 baostock 格式：6/9开头 -> sh，其余 -> sz。

        @staticmethod 表示这个方法不需要访问 self（实例数据），
        所以既可以 engine._to_baostock_code("000001") 调用，
        也可以 DataEngine._to_baostock_code("000001") 直接通过类调用。

        startswith 可以接收元组：symbol.startswith(("6", "9")) 表示
        "以 6 开头 或 以 9 开头"。
        """
        prefix = "sh" if symbol.startswith(("6", "9")) else "sz"
        return f"{prefix}.{symbol}"

    # ── 数据同步 ──

    def sync_today_bulk(self) -> int:
        """多进程并行通过 baostock 拉取增量数据（后复权），写入 SQLite。

        流程：
          1. 查每只股票本地最新日期；
          2. 对落后的股票生成"从下一天到今天"的拉取任务；
          3. 把任务切成 N 份交给 N 个进程并行拉；
          4. 汇总结果、清洗、写回数据库。

        Returns:
            本次写入的 K 线条数（不是股票只数）。
        """
        # 局部导入：只有调用本方法时才加载这两个模块。
        # 这样可以减少模块导入时间和启动开销（虽小，但是常见习惯）。
        from datetime import date, timedelta
        from multiprocessing import Pool

        today_str = date.today().strftime("%Y-%m-%d")

        tasks = []
        # GROUP BY symbol 后 MAX(date) 得到"每只股票的最新日期"。
        # 返回的 rows 形如 [("000001", "2024-01-31"), ("600000", "2024-01-30")]。
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT symbol, MAX(date) FROM stock_daily GROUP BY symbol"
            ).fetchall()

        if not rows:
            logger.warning("本地无股票数据，请先执行 --backfill")
            return 0

        # 解包元组：for symbol, last_date in rows 等价于 for row in rows: symbol, last_date = row
        for symbol, last_date in rows:
            # 已经是今天的数据，不用再拉。
            if last_date and last_date >= today_str:
                continue

            # 默认从今天开始拉（理论上不会走到这里，因为 last_date 为空说明本地没数据，
            # 而本地没数据的股票根本不会出现在上面的 GROUP BY 结果里）。
            start = today_str

            # 有历史数据时，从"最新日期的下一天"开始拉，实现增量更新。
            # date.fromisoformat 把 "2024-01-31" 转成 date 对象；
            # + timedelta(days=1) 表示加一天；strftime 再格式化回字符串。
            if last_date:
                start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")

            tasks.append((symbol, self._to_baostock_code(symbol), start, today_str))

        if not tasks:
            logger.info("所有股票已是最新，无需更新")
            return 0

        logger.info(f"需要更新 {len(tasks)} 只股票，启动多进程并行拉取...")

        # 最多开 8 个进程；任务少于 8 个时，有多少任务开多少进程。
        n_workers = min(8, len(tasks))

        # 切片步长写法：tasks[i::n_workers] 表示"从第 i 个开始，每隔 n_workers 取一个"。
        # 例如 tasks = [0,1,2,3,4], n_workers=2：
        #   i=0 → [0,2,4]
        #   i=1 → [1,3]
        # 这样能把任务尽量均匀地分给各进程（分块法）。
        chunks = [tasks[i::n_workers] for i in range(n_workers)]

        # Pool 管理一个进程池；pool.map(函数, 可迭代对象) 会把 chunks 中的每个元素
        # 分给一个子进程执行，并按原顺序返回结果列表。
        # 注意：Pool 必须配合 main.py 里的 if __name__ == "__main__" 保护使用。
        with Pool(n_workers) as pool:
            batch_results = pool.map(_bs_fetch_batch, chunks)

        # 把 N 个进程返回的列表合并成一个大列表。
        all_rows = []
        for batch in batch_results:
            all_rows.extend(batch)

        if not all_rows:
            logger.info("无新数据（可能非交易日）")
            return 0

        # DataFrame 是 pandas 的二维表格。
        # columns 明确指定列名，否则 pandas 会用 0/1/2... 当列名。
        df = pd.DataFrame(
            all_rows,
            columns=["symbol", "date", "open", "high", "low", "close", "volume", "turnover"],
        )

        # baostock 返回的是字符串；to_numeric 转成数字。
        # errors="coerce"：转不了的值（如空字符串、"--"）变成 NaN，而不是直接报错。
        for col in ["open", "high", "low", "close", "volume", "turnover"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        # 数据清洗：
        #   dropna(subset=["close"]) → 收盘价为空的行不要（停牌、异常数据）；
        #   df[df["volume"] > 0]     → 布尔索引，只保留成交量大于 0 的行。
        df = df.dropna(subset=["close"])
        df = df[df["volume"] > 0]

        count = len(df)

        # ⚠️ 注意这里的写入策略（review 中提到的风险点）：
        # 先按"日期"整批删除当天所有股票的数据，再插入本次拉到的数据。
        # 如果某只股票当天已经是最新（上面被 continue 跳过、不在 tasks 里），
        # 它的当天数据也会被这行 DELETE 删掉，且不会被重新插入。
        # 更安全的做法是按 (symbol, date) 逐条删除，或改用 UPSERT（ON CONFLICT）。
        # 这里保持原逻辑不变，仅加注释说明。
        with sqlite3.connect(self.db_path) as conn:
            for d in df["date"].unique().tolist():
                conn.execute("DELETE FROM stock_daily WHERE date = ?", (d,))

            # to_sql 把 DataFrame 写入表：
            #   if_exists="append" → 追加，不删表；
            #   index=False        → 不把 DataFrame 的行号写成额外一列；
            #   method="multi"     → 多条记录拼成一条 INSERT，比逐条插入快很多；
            #   chunksize=500      → 每次最多 500 行。
            df.to_sql(
                "stock_daily",
                conn,
                if_exists="append",
                index=False,
                method="multi",
                chunksize=500,
            )
            conn.commit()

        logger.info(f"sync_today_bulk: 写入 {count} 条数据")
        return count

    def backfill(self, symbols: list[str]) -> None:
        """通过 baostock 批量回填历史日 K 线数据（后复权）。

        容错机制：
        - 单只股票失败自动重试 3 次，间隔递增（2s/4s/8s）
        - 每 200 只股票自动重连 baostock（防止长连接超时）
        - 已入库的自动 skip，中断后可重跑续传

        和 sync_today_bulk 的区别：
          - sync_today_bulk 面向"每天增量"，多进程、速度快；
          - backfill 面向"首次灌历史"，单进程、保守、带重试，追求稳定。
        """
        import time
        from datetime import date, timedelta

        import baostock as bs

        today_str = date.today().strftime("%Y-%m-%d")
        max_retries = 3
        reconnect_interval = 200  # 每处理 N 只股票重连一次

        # 内部函数（闭包）：定义在 backfill 内部，可以直接使用外层的 logger、bs。
        # 每次需要重新登录时调用它，避免重复写 login + 错误检查。
        def _login():
            """登录 baostock，返回 True/False 表示是否成功。"""
            lg = bs.login()
            if lg.error_code != "0":
                logger.error(f"baostock 登录失败: {lg.error_msg}")
                return False
            return True

        if not _login():
            return

        # 计数器
        success = 0  # 成功写入的股票数
        skipped = 0  # 跳过（已是最新 / 无新数据）的股票数
        failed = 0  # 重试后仍失败的股票数
        since_reconnect = 0  # 距离上次重连处理了多少只股票

        # try/finally：无论循环正常结束、中途 return，还是抛异常，
        # finally 里的 bs.logout() 都一定会执行，避免连接泄漏。
        try:
            # enumerate 同时给出下标 i 和元素 symbol，i 从 0 开始。
            for i, symbol in enumerate(symbols):
                last_date = self._get_last_date(symbol)

                # 已是最新：跳过，但仍按每 500 只打印一次进度。
                if last_date and last_date >= today_str:
                    skipped += 1
                    if (i + 1) % 500 == 0:
                        logger.info(
                            f"已处理 {i + 1}/{len(symbols)}，"
                            f"成功 {success} 跳过 {skipped} 失败 {failed}"
                        )
                    continue

                # 定期重连，防止长连接超时
                since_reconnect += 1
                if since_reconnect >= reconnect_interval:
                    bs.logout()
                    time.sleep(1)  # 给服务端一点缓冲时间
                    if not _login():
                        logger.error("重连失败，终止回填")
                        return
                    since_reconnect = 0

                # 下面两行是原代码：先赋一个默认值，再在 if 里覆盖。
                # 第一行的值实际用不到（有 last_date 时会被第二行覆盖），
                # 属于可以清理的冗余代码。
                start = last_date or self.start_date
                if last_date:
                    start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")

                bs_code = self._to_baostock_code(symbol)

                # 带重试的查询
                rows = []
                query_ok = False
                # range(max_retries) → [0, 1, 2]，共 3 次尝试。
                for attempt in range(max_retries):
                    try:
                        rs = bs.query_history_k_data_plus(
                            bs_code,
                            "date,open,high,low,close,volume,amount",
                            start_date=start,
                            end_date=today_str,
                            frequency="d",
                            adjustflag="1",  # 后复权
                        )

                        # 主动把错误码转成异常，这样下面的 except 能统一处理重试。
                        if rs.error_code != "0":
                            raise RuntimeError(rs.error_msg)

                        rows = []
                        while rs.next():
                            rows.append(rs.get_row_data())
                        query_ok = True
                        break  # 成功就跳出重试循环

                    except Exception as exc:
                        if attempt < max_retries - 1:
                            # 指数退避：2 ** 1 = 2 秒，2 ** 2 = 4 秒（最后一次不等待）。
                            # 失败越久等得越久，避免频繁冲击服务端。
                            wait = 2 ** (attempt + 1)
                            logger.warning(
                                f"[{symbol}] 第{attempt + 1}次失败: {exc}，{wait}s 后重试"
                            )
                            time.sleep(wait)
                            # 重连 baostock：先登出，再重新登录。
                            bs.logout()
                            time.sleep(1)
                            _login()
                        else:
                            logger.warning(f"[{symbol}] {max_retries}次重试均失败，跳过")

                if not query_ok:
                    failed += 1
                    continue

                # 查询成功但没有数据（新股上市前、长期停牌等）。
                if not rows:
                    skipped += 1
                    continue

                # rs.fields 是列名列表，例如 ["date", "open", ...]。
                # 这里依赖循环变量 rs：只有 query_ok 为 True 时才会走到这里，
                # 此时 rs 一定是最后一次成功查询的游标对象。
                df = pd.DataFrame(rows, columns=rs.fields)

                # 与 sync_today_bulk 相同的清洗逻辑。
                for col in ["open", "high", "low", "close", "volume", "amount"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
                df = df.dropna(subset=["close"])
                df = df[df["volume"] > 0]

                if df.empty:
                    skipped += 1
                    continue

                # 补上 symbol 列；把 baostock 的 amount（成交额）重命名为 turnover。
                df["symbol"] = symbol
                df = df.rename(columns={"amount": "turnover"})
                # 按数据库表结构重新排列列顺序。
                df = df[["symbol", "date", "open", "high", "low", "close", "volume", "turnover"]]

                try:
                    with sqlite3.connect(self.db_path) as conn:
                        df.to_sql(
                            "stock_daily",
                            conn,
                            if_exists="append",
                            index=False,
                            method="multi",
                            chunksize=500,
                        )
                except sqlite3.IntegrityError:
                    # 唯一约束冲突：说明这些 (symbol, date) 已经存在，忽略即可。
                    # 这样重复跑 backfill 不会报错，实现"断点续传"。
                    pass

                success += 1

                if (i + 1) % 500 == 0:
                    logger.info(
                        f"已处理 {i + 1}/{len(symbols)}，"
                        f"成功 {success} 跳过 {skipped} 失败 {failed}"
                    )

        finally:
            bs.logout()

        logger.info(f"回填完成 — 成功: {success} | 跳过: {skipped} | 失败: {failed}")

    # ── 股票列表 ──

    def get_all_symbols(self) -> list[str]:
        """通过 baostock 获取全市场 A 股代码列表。

        Returns:
            纯数字代码列表，如 ["000001", "600000", ...]。
            baostock 登录失败或接口异常时返回空列表。
        """
        import baostock as bs

        lg = bs.login()
        if lg.error_code != "0":
            logger.error(f"baostock 登录失败: {lg.error_msg}")
            return []

        try:
            # code_name="" 和 code="" 都为空，表示查全部股票基础信息。
            rs = bs.query_stock_basic(code_name="", code="")
            symbols = []
            while rs.next():
                row = rs.get_row_data()
                code = row[0]           # "sh.600000" or "sz.000001"
                status = row[4]         # "1" = 上市
                stock_type = row[5]     # "1" = 股票
                # 只保留"正常上市"的"股票"，排除退市、指数、基金等。
                if status == "1" and stock_type == "1":
                    # "sh.600000".split(".") → ["sh", "600000"]，取 [1] 得到纯数字。
                    symbols.append(code.split(".")[1])
            logger.info(f"获取股票列表完成，共 {len(symbols)} 只")
            return symbols
        except Exception as e:
            logger.error(f"获取股票列表失败: {e}")
            return []
        finally:
            # 不管成功还是异常，都确保登出。
            bs.logout()

    def get_local_symbols(self) -> list[str]:
        """返回本地数据库中已有的股票代码（DISTINCT 去重）。

        策略都基于"本地已回填的股票"运行，而不是每次重新请求全市场列表，
        这样日常运行不需要额外的网络请求。
        """
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM stock_daily"
            ).fetchall()
        # 列表推导式（list comprehension）：
        #   [row[0] for row in rows]
        # 等价于：
        #   result = []
        #   for row in rows:
        #       result.append(row[0])
        return [row[0] for row in rows]
