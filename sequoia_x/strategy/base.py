"""策略基类模块：定义所有选股策略的抽象接口。

【为什么需要基类？】
  如果不定义 BaseStrategy，main.py 只能这样写：
      if isinstance(s, MaVolumeStrategy): ...
      elif isinstance(s, TurtleTradeStrategy): ...
  每加一个策略就要改一次 main.py。
  有了基类 + 统一接口 run()，main.py 只需要 for 循环调用 run()，
  新策略只要继承 BaseStrategy 即可自动接入——这就是"多态"。

【本文件涉及的 Python 知识点】
  1. abc.ABC / @abstractmethod：抽象基类，强制子类必须实现 run()。
  2. 类属性（webhook_key）：定义在类里、被所有实例共享的属性，子类可以覆盖。
  3. 类型注解：list[str] 表示"字符串列表"；-> list[str] 表示返回值类型。
  4. self：实例方法的第一个参数，代表"当前这个对象自己"。
"""

from abc import ABC, abstractmethod

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine


class BaseStrategy(ABC):
    """选股策略抽象基类。

    所有具体策略必须继承此类并实现 run() 方法。

    Attributes:
        webhook_key: 策略对应的飞书 webhook 标识，用于路由到不同机器人。
            默认为 'default'，将使用 Settings.feishu_webhook_url。
            子类可覆盖此属性以路由到专属机器人，例如 'ma_volume'。
    """

    # 类属性：写在 class 里、但在 __init__ 外面。
    # 所有实例共享这个值；子类可以写 `webhook_key = "turtle"` 覆盖它，
    # 之后通过 实例.webhook_key 访问到的就是子类的值。
    webhook_key: str = "default"

    def __init__(self, engine: DataEngine, settings: Settings) -> None:
        """
        初始化策略。

        子类如果没有写自己的 __init__，会自动继承这个构造函数；
        如果子类写了 __init__，通常需要通过 super().__init__(...) 先调用父类逻辑。

        Args:
            engine: DataEngine 实例，用于读取行情数据。
            settings: Settings 实例，用于读取配置。
        """
        # self.xxx = ... 表示给当前实例挂一个属性，后面 self.engine 就能取到。
        # 策略本身不负责创建 DataEngine，而是由 main.py 创建后"注入"进来。
        # 这种写法叫"依赖注入"：策略只依赖接口，不关心数据从哪来，方便测试时替换。
        self.engine = engine
        self.settings = settings

    @abstractmethod
    def run(self) -> list[str]:
        """
        执行选股逻辑，返回选中的股票代码列表。

        这是一个"抽象方法"：
          - 本类只声明接口，不提供实现（函数体只有 `...`）；
          - 继承 BaseStrategy 的子类必须实现 run()，否则实例化时会报 TypeError；
          - 这样 main.py 就能放心地对任意策略调用 run()。

        Returns:
            满足策略条件的股票代码列表，如 ['000001', '600519']。
            无选股结果时返回空列表。
        """
        # `...`（Ellipsis）在抽象方法里只是占位，表示"这里本应有代码"。
        # 它等价于 pass，但语义上更明确：这是有意留空的接口。
        ...
