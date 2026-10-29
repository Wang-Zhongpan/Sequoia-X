"""日志模块：基于 rich 库提供带颜色的结构化终端日志输出。

【为什么要用 logging，而不是 print？】
  - print 只能输出纯文本，没有级别（INFO/WARNING/ERROR）、没有时间戳；
  - logging 可以按级别过滤、格式化、输出到文件，还能自动打印异常堆栈。

【本文件涉及的 Python 知识点】
  1. logging 的层级结构：logging.getLogger(name) 按名字管理 logger，
     同名调用返回的是同一个对象。
  2. Handler：真正负责"把日志写到哪里"（终端、文件、网络）。
  3. Formatter：决定每条日志长什么样。
  4. propagate=False：阻止日志向父 logger 冒泡，避免重复打印。
  5. 幂等性：同一个 logger 重复调用 get_logger() 不会重复添加 handler。
"""

import logging

from rich.logging import RichHandler

# 日志正文格式：%(name)s 是 logger 名字，%(message)s 是日志内容。
# 时间戳由 RichHandler 自己渲染（见下面的 log_time_format）。
_FORMAT = "%(name)s - %(message)s"


def get_logger(name: str) -> logging.Logger:
    """
    工厂函数，返回配置了 RichHandler 的 Logger 实例。

    支持 DEBUG/INFO/WARNING/ERROR 四级日志，由 rich 自动以不同颜色渲染。
    每条日志包含时间戳、模块名和日志级别。
    同名 logger 不重复添加 handler（幂等性）。

    Args:
        name: logger 名称，通常传入 __name__。

    Returns:
        logging.Logger: 配置好的 Logger 实例。
    """
    # logging.getLogger 内部维护一个字典：同一个 name 永远返回同一个 Logger 对象。
    # 如果 name 不存在，会自动创建一个新的 Logger。
    logger = logging.getLogger(name)

    # 如果这个 logger 已经有 handler，说明之前配置过，直接返回，避免重复添加。
    # 否则日志会被打印两次、三次（取决于调用了几次）。
    if logger.handlers:
        return logger

    # RichHandler 是 rich 提供的 handler：会用颜色、缩进、语法高亮渲染日志。
    handler = RichHandler(
        rich_tracebacks=True,  # 异常堆栈也用 rich 渲染，可读性更好
        show_path=False,  # 不显示源码文件路径，保持输出简洁
        log_time_format="[%Y-%m-%d %H:%M:%S]",  # 时间格式：年-月-日 时:分:秒
    )
    # Formatter 只负责"正文"的格式；时间戳由 RichHandler 单独渲染。
    handler.setFormatter(logging.Formatter(_FORMAT))

    logger.addHandler(handler)

    # 设置最低输出级别为 DEBUG，意味着 DEBUG / INFO / WARNING / ERROR 都会输出。
    # 如果只想看 WARNING 以上，改成 logging.WARNING 即可。
    logger.setLevel(logging.DEBUG)

    # 默认情况下，子 logger 的日志会向父 logger（root）冒泡，可能被重复输出。
    # 这里设为 False，表示"我自己的日志由我自己处理，不再向上传递"。
    logger.propagate = False

    return logger
