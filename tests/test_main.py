"""主程序入口属性测试。

【测试目标】
  验证"主流程出现任何异常时，程序以非 0 退出码结束"。
  这保证 cron / 监控系统能发现运行失败，而不是静默假装成功。

【测试技巧】
  不真的要跑整个选股流程（那需要数据库和网络），
  而是 patch 掉 main 模块里的 get_settings，让它在被调用时直接抛异常，
  然后断言 main() 会把它转成 sys.exit(1)。
"""

import sys
from unittest.mock import patch

import pytest
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

# 预先导入 main 模块，避免在 @given 循环中重复导入
# 另外，main.py 里有 `if __name__ == "__main__"` 保护，
# 所以 import main 不会自动执行 main()。
import main as main_module


# Feature: sequoia-x-v2, Property 13: 主程序异常以非零退出码终止
@given(error_msg=st.text(min_size=1, max_size=100))
@h_settings(max_examples=30, deadline=None)
def test_main_exits_nonzero_on_exception(error_msg: str) -> None:
    """属性 13：main() 中任意未捕获异常应导致 sys.exit(1)。"""
    # patch main 模块中直接引用的 get_settings
    # 注意 patch.object(模块, "属性名", ...) 和 patch("路径.属性") 的区别：
    #   这里必须 patch main_module.get_settings，因为 main.py 里是
    #   `from ... import get_settings` 导入的，它引用的是 main 模块自己的名字。
    # side_effect=异常实例 → 每次调用这个 mock 都会抛出该异常。
    with patch.object(main_module, "get_settings", side_effect=RuntimeError(error_msg)):
        # sys.exit() 本质上是抛出 SystemExit 异常；
        # pytest.raises(SystemExit) 用来接住它，避免真的结束测试进程。
        with pytest.raises(SystemExit) as exc_info:
            main_module.main()

        # .code 是传给 sys.exit 的值；0 表示成功，非 0 表示失败。
        assert exc_info.value.code != 0
