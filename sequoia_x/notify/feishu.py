"""飞书通知模块：将选股结果通过 Webhook 推送至飞书群。

【什么是 Webhook？】
  简单说就是一个"固定的 URL"：往这个地址 POST 一段 JSON，飞书群里就会收到消息。
  每个策略可以配置自己的 Webhook，从而把不同策略的结果推到不同的群里。

【本文件涉及的 Python 知识点】
  1. class / __init__ / self：面向对象的基本写法。
  2. @staticmethod：不需要访问 self 的方法，相当于"挂在类下面的普通函数"。
  3. dict / list 嵌套：飞书卡片消息本质是一个多层嵌套的字典。
  4. f-string、join、字符串格式化。
  5. requests：发送 HTTP 请求的第三方库（比标准库 urllib 更易用）。
  6. try / except + 特定异常类型：只捕获网络请求相关异常，不吞掉程序 bug。
"""

import json
from datetime import date

import requests

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)


class FeishuNotifier:
    """飞书 Webhook 推送器。

    根据策略的 webhook_key 路由到对应的飞书机器人。
    若 webhook_key 未在 Settings.strategy_webhooks 中配置，
    则 fallback 到 Settings.feishu_webhook_url。
    """

    def __init__(self, settings: Settings) -> None:
        """
        初始化 FeishuNotifier。

        Args:
            settings: Settings 实例，提供 Webhook URL 配置。
        """
        # 只保存配置对象，真正的 URL 在 send() 时按策略动态查询。
        self.settings = settings

    @staticmethod
    def _to_xueqiu_code(code: str) -> str:
        """将纯数字代码转为雪球格式：6开头→SH，4/8开头→BJ，其余→SZ。

        雪球链接形如 https://xueqiu.com/S/SH600000，需要带市场前缀。

        注意：这里的 9 开头会落到最后的 SZ（因为 9 不满足前面两个分支），
        而 data/engine.py 的 _to_baostock_code 把 9 当作 sh。
        两处映射规则不完全一致，属于 review 中提到的小问题。
        """
        if code.startswith("6"):
            return f"SH{code}"
        elif code.startswith(("4", "8")):
            return f"BJ{code}"
        return f"SZ{code}"

    @staticmethod
    def _get_stock_names(symbols: list[str]) -> dict[str, str]:
        """通过 baostock 批量查询股票名称，返回 {code: name} 映射。

        说明：虽然函数名里有"批量"，但实现是逐只查询，
        每只股票一次网络请求。候选股票多时这里会比较慢，
        后续可以改成一次 query_stock_basic() 拉全表再过滤。

        Args:
            symbols: 股票代码列表，如 ["000001", "600000"]。

        Returns:
            形如 {"000001": "平安银行", "600000": "浦发银行"} 的字典；
            查不到的代码不会出现在字典里。
        """
        import baostock as bs

        # 这里没有检查 login 是否成功；失败时后续查询拿不到数据，
        # mapping 会是空字典，卡片里就显示代码本身（见 _build_card 的 names.get）。
        bs.login()
        mapping = {}
        for code in symbols:
            prefix = "sh" if code.startswith(("6", "9")) else "sz"
            rs = bs.query_stock_basic(code=f"{prefix}.{code}")
            while rs.next():
                row = rs.get_row_data()
                mapping[code] = row[1]  # 第2个字段是股票名称
        bs.logout()
        return mapping

    def _build_card(self, symbols: list[str], strategy_name: str) -> dict:
        """把选股结果拼成飞书"消息卡片"所需的 JSON 结构。

        飞书卡片是一个嵌套字典：
          msg_type = "interactive" 表示交互式卡片；
          card.header  → 标题栏；
          card.elements → 正文内容列表（div 文本、hr 分割线等）。

        Args:
            symbols: 股票代码列表。
            strategy_name: 策略名称，显示在卡片标题里。

        Returns:
            可直接 json.dumps 后 POST 的字典。
        """
        today = date.today().strftime("%Y-%m-%d")
        names = self._get_stock_names(symbols)

        links: list[str] = []
        for code in symbols:
            xq_code = self._to_xueqiu_code(code)
            # dict.get(code, xq_code)：查到名字用名字，查不到就退回显示代码。
            name = names.get(code, xq_code)
            # 飞书卡片支持 Markdown 链接语法：[显示文字](链接地址)
            links.append(f"[{name}](https://xueqiu.com/S/{xq_code})")

        # " ".join(列表) 用空格把列表元素拼成一个字符串。
        # 条件表达式 `A if 条件 else B`：links 非空时用 A，否则用 B。
        symbol_text = " ".join(links) if links else "（无选股结果）"

        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"📈 Sequoia-X 选股播报 | {strategy_name}",
                    },
                    "template": "blue",  # 卡片主题色
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",  # 使用飞书 Markdown 渲染
                            "content": (
                                f"**日期：** {today}\n"
                                f"**策略：** {strategy_name}\n"
                                f"**选股数量：** {len(symbols)}"
                            ),
                        },
                    },
                    {"tag": "hr"},  # 分割线
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**选股列表：**\n{symbol_text}",
                        },
                    },
                ],
            },
        }

    def send(
        self,
        symbols: list[str],
        strategy_name: str,
        webhook_key: str = "default",
    ) -> None:
        """
        将选股结果格式化为飞书卡片消息并 POST 至对应 Webhook。

        根据 webhook_key 从 Settings 中查找专属 URL；
        若未配置，则 fallback 到 feishu_webhook_url。

        Args:
            symbols: 选股结果代码列表。
            strategy_name: 策略名称，用于卡片标题。
            webhook_key: 策略标识，用于路由到对应飞书机器人。
                有默认值 "default"，所以调用时可以不传这个参数。

        Raises:
            不抛出异常，HTTP 失败时记录 ERROR 日志。

        【设计取舍】
          推送失败不会中断主流程：一只股票选出来了但推送失败，
          只影响"通知"，不应该让整个程序崩溃。
        """
        # get_webhook_url 内部会做 fallback，详见 core/config.py。
        url = self.settings.get_webhook_url(webhook_key)
        payload = self._build_card(symbols, strategy_name)

        try:
            # requests.post(地址, data=请求体, headers=请求头, timeout=超时秒数)
            # json.dumps 把 Python 字典序列化成 JSON 字符串。
            # timeout=10 很重要：没有超时的话，网络异常时可能永久卡住。
            resp = requests.post(
                url,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            # 解析飞书真正的返回体
            # Response.json() 把响应体按 JSON 解析成字典。
            resp_json = resp.json()

            # 飞书真正的成功标志是内部的 code == 0
            # 注意：HTTP 200 只代表"请求送达了"，不代表"机器人接受并发出去了"，
            # 所以还要检查响应 JSON 里的 code 字段。
            if resp.status_code != 200 or resp_json.get("code") != 0:
                logger.error(
                    f"飞书推送失败 [{webhook_key}] "
                    f"HTTP状态={resp.status_code} 飞书响应={resp.text}"
                )
            else:
                logger.info(f"飞书推送成功 [{webhook_key}]，共 {len(symbols)} 只股票")

        except requests.RequestException as exc:
            # 只捕获 requests 相关的异常（连接失败、超时、JSON 解析失败等）。
            # 故意不写 `except Exception`：程序自身的 bug（如拼写错误）应该暴露出来，
            # 而不是被这里静默吞掉。
            logger.error(f"飞书推送请求异常 [{webhook_key}]：{exc}")
