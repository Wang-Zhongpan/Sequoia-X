"""Sequoia-X V2 主程序入口。

两种运行模式：
  python main.py               # 日常模式：8进程增量补数据 + 跑策略 + 飞书推送（2~3分钟）
  python main.py --backfill    # 回填模式：baostock 拉全市场历史K线（首次/补数据用，约12分钟）

【阅读提示】
这个文件是整条流水线的"总指挥"，建议按下面的顺序理解整个项目：
    main() → DataEngine（准备数据）→ Strategy.run()（选股）→ FeishuNotifier（推送）

【本文件涉及的 Python 知识点】
  1. argparse              —— 解析命令行参数（--backfill）
  2. try / except / finally —— 异常处理，保证出错时有清晰日志
  3. f-string              —— f"文本 {变量}" 形式的字符串插值
  4. 多态                   —— 不同策略类都实现 run()，循环里可以统一调用
  5. sys.exit(1)           —— 用非 0 退出码告诉操作系统"程序失败了"
  6. if __name__ == "__main__" —— 区分"直接运行本文件"和"被别的文件 import"
"""

import argparse  # 标准库：解析命令行参数，--backfill 就是它识别出来的
import sys  # 标准库：sys.exit() 用于设置进程退出码

from dotenv import load_dotenv

# load_dotenv() 会读取项目根目录的 .env 文件，把其中的 KEY=VALUE 写进 os.environ。
# 必须放在导入 sequoia_x 之前：Settings 初始化时会读取环境变量，晚加载就来不及了。
load_dotenv()

from datetime import date  # noqa: F401  # 目前本文件未使用，保留给后续扩展

import socket

# 给所有网络连接设置默认超时（秒）。baostock / requests 如果卡住，
# 没有超时会一直阻塞，导致定时任务永远不结束。
socket.setdefaulttimeout(10.0)

# 下面这些都是项目内部的模块。注意：每个策略都是一个独立的类，
# 它们都继承 BaseStrategy，所以可以放进同一个列表统一处理。
from sequoia_x.core.config import get_settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine
from sequoia_x.notify.feishu import FeishuNotifier
from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.high_tight_flag import HighTightFlagStrategy
from sequoia_x.strategy.limit_up_shakeout import LimitUpShakeoutStrategy
from sequoia_x.strategy.ma_volume import MaVolumeStrategy
from sequoia_x.strategy.turtle_trade import TurtleTradeStrategy
from sequoia_x.strategy.uptrend_limit_down import UptrendLimitDownStrategy
from sequoia_x.strategy.rps_breakout import RpsBreakoutStrategy
from sequoia_x.strategy.private_placement import PrivatePlacementStrategy


def main() -> None:
    """程序主流程。

    Returns:
        None。函数内部通过 sys.exit(1) 表示失败；正常结束时返回 None（退出码 0）。
    """
    # ── 第 0 步：解析命令行参数 ──
    # ArgumentParser 是 argparse 的核心类，description 会显示在 `python main.py -h` 里。
    parser = argparse.ArgumentParser(description="Sequoia-X V2 选股系统")
    # action="store_true" 的含义：
    #   命令行里写了 --backfill → args.backfill 为 True
    #   没写                      → args.backfill 为 False
    # 这种参数叫"开关参数"，不需要再跟一个具体值。
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="回填模式：通过 baostock 拉取全市场历史 K 线（约12分钟）",
    )
    # parse_args() 读取 sys.argv（即命令行），返回一个对象，属性名就是参数名。
    args = parser.parse_args()

    # ── 主流程统一异常出口 ──
    # 把整个流程包在 try 里：不管哪一步抛出异常，都会在 except 里记录一次完整堆栈，
    # 然后以退出码 1 结束，方便 cron / 系统监控发现"这次运行失败了"。
    try:
        # 1. 初始化配置
        # get_settings() 是单例工厂：第一次调用时读取 .env / 环境变量，
        # 之后直接返回同一个对象，避免重复解析配置。
        settings = get_settings()

        # 2. 初始化日志
        # __name__ 在当前文件被直接运行时等于 "__main__"，被 import 时等于 "main"。
        # 用模块名当 logger 名字，日志里就能看出是哪段代码打印的。
        logger = get_logger(__name__)
        logger.info("Sequoia-X V2 启动")

        # 3. 初始化数据引擎
        # DataEngine 构造时会在数据库不存在时自动建表、建索引。
        engine = DataEngine(settings)

        if args.backfill:
            # ── 回填模式：单线程保守拉历史 K 线，自动多轮重跑 ──
            logger.info("进入回填模式...")
            # get_all_symbols() 通过 baostock 拿当前全市场 A 股代码（约 5000+ 只）。
            all_symbols = engine.get_all_symbols()
            # backfill 内部做了重试、断点续传、定期重连，详见 data/engine.py。
            engine.backfill(all_symbols)
            logger.info("Sequoia-X V2 回填模式运行完成")
            # return 表示回填模式到此结束，不会继续执行下面的日常流程。
            return

        # ── 日常模式：单次 API 补今天 + 策略 + 推送 ──
        logger.info("开始拉取最新快照...")
        # sync_today_bulk() 用多进程从 baostock 拉"上次更新日期之后"的数据，返回写入条数。
        count = engine.sync_today_bulk()
        logger.info(f"快照同步完成，写入 {count} 只股票")

        # 4. 策略列表（新增策略在此追加即可）
        # 这里的关键是"多态"：
        #   - 列表分别装了 7 个不同类的实例；
        #   - 但它们都继承自 BaseStrategy，并且都实现了 run()；
        #   - 所以下面的 for 循环可以用同一个 strategy.run() 调用它们。
        # 这也是"开闭原则"的体现：新增策略 = 新增一个类 + 在这里加一行。
        strategies: list[BaseStrategy] = [
            MaVolumeStrategy(engine=engine, settings=settings),
            TurtleTradeStrategy(engine=engine, settings=settings),
            HighTightFlagStrategy(engine=engine, settings=settings),
            LimitUpShakeoutStrategy(engine=engine, settings=settings),
            UptrendLimitDownStrategy(engine=engine, settings=settings),
            RpsBreakoutStrategy(engine=engine, settings=settings),
            PrivatePlacementStrategy(engine=engine, settings=settings),
        ]

        # 推送器只需要一个实例，内部会根据 webhook_key 选择不同的飞书机器人。
        notifier = FeishuNotifier(settings)

        # 5. 遍历策略，有结果则推送至对应机器人
        for strategy in strategies:
            # type(obj).__name__ 返回对象所属类的名字，例如 "MaVolumeStrategy"。
            strategy_name = type(strategy).__name__
            logger.info(f"执行策略：{strategy_name}")

            # 多态调用：无论是哪个子类，接口都是 run()，返回值都是 list[str]。
            selected: list[str] = strategy.run()
            logger.info(f"{strategy_name} 选出 {len(selected)} 只股票")

            if selected:
                # 关键字参数（keyword argument）写法：参数名=值。
                # 这样即使参数顺序变化也不容易传错。
                # webhook_key 是类属性，子类里覆盖成了各自的名字，
                # 用来把不同策略的结果推到不同的飞书群。
                notifier.send(
                    symbols=selected,
                    strategy_name=strategy_name,
                    webhook_key=strategy.webhook_key,
                )
            else:
                logger.info(f"{strategy_name} 无选股结果，跳过推送")

    except Exception:
        # 注意这里没有写 `as exc`：我们不直接使用异常对象，
        # 而是让 logger.exception() 自动把完整堆栈写进日志（比 print 更便于排查）。
        try:
            _logger = get_logger(__name__)
            _logger.exception("主流程发生未捕获异常，程序终止")
        except Exception:
            # 极端兜底：如果连日志模块都坏了，就直接用标准库打印堆栈，
            # 保证异常信息不会彻底丢失。
            import traceback

            traceback.print_exc()
        # 退出码 1 表示"失败"。0 表示成功，这是 Unix 的通用约定，
        # cron / 监控 / CI 都靠它判断本次运行是否正常。
        sys.exit(1)

    logger.info("Sequoia-X V2 运行完成")


# 这两行是 Python 的标准写法，作用是：
#   - 直接执行 `python main.py` 时，__name__ == "__main__"，于是调用 main()；
#   - 当别的文件 `import main` 时，不会自动跑 main()，只提供函数给你调用。
# 另外，Windows 上 multiprocessing 使用 "spawn" 方式启动子进程，
# 子进程会重新 import 本模块，这个判断也能防止子进程无限递归地再启动 main()。
if __name__ == "__main__":
    main()
