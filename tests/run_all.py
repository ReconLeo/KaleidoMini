# -*- coding: utf-8 -*-
"""run_all —— 依次执行 tests/test_*.py，汇总结果

设计说明：刻意**不**用 subprocess（受沙箱管道限制），改为同进程 import + 调用 main()。
各测试模块互不共享可变全局，可安全串行。

运行： python tests/run_all.py
退出码：0 全部通过；1 有失败
"""

import importlib.util
import io
import os
import sys
import time
import traceback
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))


def discover():
    files = sorted(
        f for f in os.listdir(HERE)
        if f.startswith("test_") and f.endswith(".py")
    )
    return files


def load_module(path):
    name = "dsh_test_" + os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main():
    files = discover()
    if not files:
        print("未发现测试文件")
        return 1

    print("#" * 78)
    print(f"# Kaleido Mini 测试总运行：{len(files)} 个测试文件")
    print("#" * 78)

    outcomes = []
    started_all = time.time()

    for filename in files:
        path = os.path.join(HERE, filename)
        print()
        started = time.time()
        buffer = io.StringIO()
        try:
            module = load_module(path)
            runner = getattr(module, "main", None)
            if runner is None:
                outcomes.append((filename, "SKIP", 0.0, "没有 main()"))
                print(f"--- {filename}: 跳过（没有 main()）")
                continue
            with redirect_stdout(buffer):
                code = runner()
            elapsed = time.time() - started
            # 回显子测试输出
            sys.stdout.write(buffer.getvalue())
            outcomes.append((filename, "PASS" if code == 0 else "FAIL", elapsed, ""))
        except Exception:
            elapsed = time.time() - started
            sys.stdout.write(buffer.getvalue())
            print(f"--- {filename}: 异常")
            traceback.print_exc()
            outcomes.append((filename, "ERROR", elapsed, "异常"))

    total_elapsed = time.time() - started_all
    print()
    print("#" * 78)
    print("# 汇总")
    print("#" * 78)
    width = max(len(f) for f, _, _, _ in outcomes)
    bad = 0
    for filename, status, elapsed, note in outcomes:
        flag = {"PASS": "✅", "FAIL": "❌", "ERROR": "💥", "SKIP": "－"}.get(status, "?")
        print(f"{flag} {filename.ljust(width)}  {status:<5}  {elapsed:6.2f}s  {note}")
        if status in ("FAIL", "ERROR"):
            bad += 1

    print("-" * 78)
    print(f"共 {len(outcomes)} 个文件，失败 {bad} 个，总耗时 {total_elapsed:.2f}s")
    if bad:
        print("存在失败 ❌")
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
