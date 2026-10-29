"""配置管理模块：通过 pydantic-settings 从环境变量或 .env 文件加载系统配置。

【本文件涉及的 Python 知识点】
  1. 类型注解 + 类属性：BaseSettings 把类属性当作"配置字段"，并自动做类型转换和校验。
  2. 必填 vs 默认值：字段不给默认值 → 缺失时抛 ValidationError。
  3. 环境变量映射：字段名 feishu_webhook_url 会自动对应 FEISHU_WEBHOOK_URL。
  4. @classmethod：第一个参数是类本身（cls），不是实例。
  5. os.environ：一个"类字典"对象，保存当前进程的所有环境变量。
  6. 字符串切片 / startswith / lower / upper：这里大量用来解析环境变量名。
  7. 单例模式：模块级变量 _settings + get_settings()，保证全局只有一个配置对象。
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置对象。

    继承 BaseSettings 后，下面这些"带类型注解的类属性"会自动变成配置字段：
      - 有默认值的字段：环境变量 / .env 里没有时使用默认值；
      - 没有默认值的字段（feishu_webhook_url）：缺失时直接报 ValidationError。
    环境变量名默认是"字段名大写"，所以 feishu_webhook_url ← FEISHU_WEBHOOK_URL。
    """

    # 本地 SQLite 数据库路径。父目录不存在时 DataEngine 会自动创建。
    db_path: str = "data/sequoia_v2.db"

    # 首次回填数据的起始日期（格式 YYYY-MM-DD）。
    start_date: str = "2024-01-01"

    # 飞书群机器人 Webhook 地址，作为所有策略的兜底推送地址。
    # 这里故意不写默认值：没有配置 Webhook 程序就没有意义，应尽早报错。
    feishu_webhook_url: str  # 必填字段，缺失时抛出 ValidationError

    # 各策略专属的 Webhook 映射，例如 {"ma_volume": "https://..."}。
    # 说明：在普通 Python 里，可变对象（list/dict）做默认值是危险写法；
    # 但 pydantic 会为每个实例做一份拷贝，所以这里写 {} 是安全的。
    strategy_webhooks: dict[str, str] = {}

    # model_config 是 pydantic-settings 的配置项（不是业务配置）。
    model_config = SettingsConfigDict(
        env_file=".env",  # 自动读取项目根目录的 .env 文件
        env_file_encoding="utf-8",
        case_sensitive=False,  # 环境变量名不区分大小写
        extra="ignore",  # <--- 加上这一行！让 Pydantic 放行未定义的变量
        # 例如 .env 里写了 STRATEGY_WEBHOOK_XXX，它不是 Settings 的字段，
        # 如果不 ignore，pydantic 会报"多余的字段"错误。
    )

    @classmethod
    def settings_customise_sources(cls, settings_cls, **kwargs):  # type: ignore[override]
        """扩展配置源，支持从环境变量中扫描 STRATEGY_WEBHOOK_ 前缀的键。

        【理解提示】
        这是 pydantic-settings 提供的"钩子函数"，本该用来返回自定义的配置来源
        （环境变量、.env 文件、初始化参数等）。但这段代码虽然扫描了环境变量，
        最后却 `return sources`（原样返回父类结果），所以扫描结果在这里并没有生效。
        真正把 STRATEGY_WEBHOOK_XXX 合并进 strategy_webhooks 的，是下面的
        model_post_init()。阅读时可以把本函数当作历史遗留 / 待清理代码。

        Args:
            settings_cls: pydantic 传进来的类对象（等价于 cls）。
            **kwargs: pydantic 传入的各个配置源，例如 init_settings、env_settings。

        Returns:
            tuple: 配置源元组，顺序决定优先级。
        """
        from pydantic_settings import EnvSettingsSource  # noqa: F401  # 未使用，可删除
        import os

        # super() 调用父类的同名方法，拿到默认的配置源元组。
        sources = super().settings_customise_sources(settings_cls, **kwargs)

        # 扫描环境变量，将 STRATEGY_WEBHOOK_<KEY> 收集到 strategy_webhooks
        prefix = "STRATEGY_WEBHOOK_"
        webhooks: dict[str, str] = {}
        for key, value in os.environ.items():
            # key 是环境变量名，value 是它的字符串值。
            # key.upper() 是为了兼容小写写法的环境变量。
            if key.upper().startswith(prefix):
                # 字符串切片：key[len(prefix):] 表示"从第 N 个字符切到末尾"，
                # 即去掉 STRATEGY_WEBHOOK_ 前缀，只留策略名。
                strategy_key = key[len(prefix):].lower()
                webhooks[strategy_key] = value

        # 注入到初始化数据中（通过 init_kwargs source）
        if webhooks:
            # 下面三行都是"写了但没人用"的代码：
            #   original_init 取了却没用；
            #   _STRATEGY_WEBHOOKS_PARSED 这个环境变量没有任何地方读取；
            #   _parsed_strategy_webhooks 这个类属性也没有任何地方读取。
            # 保留注释是为了说明：阅读他人代码时，要留意这类"看起来在做事"的代码。
            original_init = kwargs.get("init_settings")  # noqa: F841
            # 直接在 env 层注入，通过 model_post_init 处理
            os.environ.setdefault("_STRATEGY_WEBHOOKS_PARSED", "1")
            # 存储解析结果供 model_validator 使用
            cls._parsed_strategy_webhooks = webhooks

        return sources

    def model_post_init(self, __context: object) -> None:
        """初始化后合并 STRATEGY_WEBHOOK_ 前缀的环境变量到 strategy_webhooks。

        这是 pydantic 提供的生命周期钩子：对象完成字段校验后自动调用。
        这里再次扫描 os.environ，把形如
            STRATEGY_WEBHOOK_MA_VOLUME=https://...
        的变量合并成
            {"ma_volume": "https://..."}

        注意：扫描的是 os.environ（进程环境变量），不是 .env 文件本身。
        main.py 先调用了 load_dotenv()，所以 .env 的内容此时已经在 os.environ 里；
        如果绕过 main.py 直接实例化 Settings()，只写在 .env 里的策略 webhook 不会生效。

        Args:
            __context: pydantic 内部使用的上下文参数，这里不需要用到。
        """
        import os

        prefix = "STRATEGY_WEBHOOK_"
        # dict(self.strategy_webhooks) 复制一份，避免直接修改原字典。
        webhooks: dict[str, str] = dict(self.strategy_webhooks)
        for key, value in os.environ.items():
            if key.upper().startswith(prefix):
                strategy_key = key[len(prefix):].lower()
                webhooks[strategy_key] = value

        # 使用 object.__setattr__ 绕过 pydantic 的不可变保护
        # 普通写法 self.strategy_webhooks = ... 在 pydantic 模型里可能被拦截，
        # 这里直接调用"对象最底层"的属性设置方法。
        object.__setattr__(self, "strategy_webhooks", webhooks)

    def get_webhook_url(self, webhook_key: str) -> str:
        """
        根据 webhook_key 返回对应的 Webhook URL。

        优先从 strategy_webhooks 查找，找不到则 fallback 到 feishu_webhook_url。

        Args:
            webhook_key: 策略标识，如 'ma_volume'、'breakout'。

        Returns:
            对应的 Webhook URL 字符串。

        示例：
            settings.strategy_webhooks = {"ma_volume": "https://A"}
            settings.feishu_webhook_url = "https://DEFAULT"
            settings.get_webhook_url("ma_volume")  # → "https://A"
            settings.get_webhook_url("turtle")     # → "https://DEFAULT"
        """
        # dict.get(key, default) 的第二个参数是"找不到时返回的默认值"。
        # 这里先 .lower() 统一大小写，避免配置成 MA_VOLUME 时匹配失败。
        return self.strategy_webhooks.get(webhook_key.lower(), self.feishu_webhook_url)


# 模块级变量：保存全局唯一的配置实例。
# 类型写成 `Settings | None` 表示"要么是 Settings，要么是 None"（Python 3.10+ 语法）。
_settings: Settings | None = None


def get_settings() -> Settings:
    """返回全局 Settings 单例。

    首次调用时从环境变量或 .env 文件加载配置。
    若必填字段（feishu_webhook_url）缺失，抛出 pydantic_core.ValidationError。

    【知识点：单例模式】
      - 第一次调用：_settings 为 None → 创建 Settings() 并缓存；
      - 第二次调用：直接返回缓存对象，不再重复读 .env。
      - `global _settings` 告诉 Python：下面要修改的是模块级变量，
        而不是在函数里新建一个同名局部变量。

    Returns:
        Settings: 全局唯一的配置实例。

    Raises:
        pydantic_core.ValidationError: 当必填字段缺失或字段类型不匹配时抛出。
    """
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
