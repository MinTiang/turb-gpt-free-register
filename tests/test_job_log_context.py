# -*- coding: utf-8 -*-
"""任务日志线程过滤回归:浏览器驱动的隔离子线程日志必须写入本任务文件。

背景(2026-09-18):cloak/patchright 为规避 Playwright Sync 循环的线程绑定,
在 "父线程名+驱动名" 的子线程中执行注册。_JobLogContext 的 FileHandler
过滤器若只认精确线程名,过程日志会全部丢失(WebUI 页面只剩首尾两行)。
"""
import logging
import threading
import unittest

from core.registration_service import _JobLogContext


def _record(thread_name: str) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="过程日志", args=None, exc_info=None,
    )
    # LogRecord 构造时自动取当前线程名;测试需显式覆盖为目标线程名
    record.threadName = thread_name
    return record


class JobLogContextFilterTests(unittest.TestCase):
    def _filter_in_thread(self, name):
        box = {}

        def _enter():
            ctx = _JobLogContext("NUL")
            ctx.__enter__()
            box["filter"] = ctx.handler.filters[0]
            ctx.__exit__(None, None, None)

        t = threading.Thread(target=_enter, name=name)
        t.start()
        t.join()
        return box["filter"]

    def test_worker_and_child_threads_pass_filter(self):
        f = self._filter_in_thread("reg-worker-1_0")
        # worker 线程自身的日志
        self.assertTrue(f(_record("reg-worker-1_0")))
        # 浏览器驱动隔离子线程(cloak/patchright)
        self.assertTrue(f(_record("reg-worker-1_0+cloak")))
        self.assertTrue(f(_record("reg-worker-1_0+patchright")))

    def test_other_worker_log_blocked(self):
        f = self._filter_in_thread("reg-worker-1_0")
        # 其他任务的线程不得串写
        self.assertFalse(f(_record("reg-worker-2_0")))
        self.assertFalse(f(_record("reg-worker-2_0+cloak")))
        # 前缀相似但不带 + 分隔的线程也不得通过
        self.assertFalse(f(_record("reg-worker-1_0x")))


if __name__ == "__main__":
    unittest.main()
