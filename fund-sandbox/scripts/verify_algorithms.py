# -*- coding: utf-8 -*-
"""核心算法正确性验证。

筛选规则算错 → 选出的基金就是错的 → 直接违背项目目的。
所以用**已知答案的手工序列**验证，而不是"跑通就算对"。

用法：runtime\\python\\python.exe scripts\\verify_algorithms.py
（顺带一个教训：首版这个脚本自己有 3 处错 —— 期望值算错、check() 不支持 None 期望 ——
  导致误报 5 项"算法失败"。**测试本身也会错，失败时先怀疑测试**。）

筛选规则算错 → 选出的基金就是错的 → 直接违背项目目的。
所以用**已知答案的手工序列**验证，而不是"跑通就算对"。
"""
import os
import sys

# 相对路径：本脚本在 fund-sandbox/scripts/，上一级即 fund-sandbox 根，可正确 import backend
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.engine import screen_rules as sr  # noqa: E402

FAIL = []


def check(name, got, want, tol=0.01):
    if want is None:
        ok = got is None
    else:
        ok = got is not None and abs(got - want) <= tol
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: got={got} want={want}")
    if not ok:
        FAIL.append(name)


print("=== max_drawdown（最大回撤：峰值→谷底）===")
# 长度需 ≥ MIN_SERIES(60)，用重复模式构造但保留关键形状
# 形状：涨到 2.0 → 跌到 1.0（-50%）→ 回到 2.0
seq = [1.0, 2.0] + [2.0] * 30 + [1.0] + [1.0] * 27 + [2.0] * 5
check("峰值2.0→谷底1.0 应为 -50%", sr.max_drawdown(seq), -50.0)
# 单调上涨 → 无回撤
up = [1.0 + i * 0.01 for i in range(70)]
check("单调上涨 应为 0", sr.max_drawdown(up), 0.0)
# 单调下跌 从 2.0 到 1.0 → -50%
down = [2.0 - i * (1.0 / 69) for i in range(70)]
check("单调下跌 2.0→1.0 应为 -50%", sr.max_drawdown(down), -50.0, tol=0.05)
# 回撤后新高：先跌 50% 再创新高 → 最大回撤仍是 -50%
seq2 = [2.0] * 30 + [1.0] * 20 + [4.0] * 20
check("跌50%后创新高 最大回撤仍为 -50%", sr.max_drawdown(seq2), -50.0)

print()
print("=== percentile（当前值在序列中的百分位）===")
# 严格递增序列，当前=最大值 → 100%
inc = [float(i) for i in range(1, 71)]
check("严格递增、当前最大 应为 100%", sr.percentile(inc), 100.0)
# 严格递减，当前=最小值 → 1/70 ≈ 1.43%
dec = [float(i) for i in range(70, 0, -1)]
check("严格递减、当前最小 应为 1.43%", sr.percentile(dec), 100.0 / 70, tol=0.05)
# 中位：前 35 个小于当前，后 34 个大于当前 → (35+1)/70 = 51.43%
mid = [float(i) for i in range(1, 36)] + [float(i) for i in range(200, 234)] + [100.0]
check("居中 应为 51.43%", sr.percentile(mid), 36.0 / 70 * 100, tol=0.05)

print()
print("=== ret_1y（近 1 年收益率）===")
check("1.0 → 1.1 应为 +10%", sr.ret_1y([1.0] * 69 + [1.1]), 10.0, tol=0.01)
check("1.0 → 0.9 应为 -10%", sr.ret_1y([1.0] * 69 + [0.9]), -10.0, tol=0.01)
check("首值非法(0) 应返回 None", sr.ret_1y([0.0] + [1.0] * 69), None, tol=0)

print()
print("=== 数据不足的保护（MIN_SERIES=60）===")
check("30 条序列 分位应为 None", sr.percentile([1.0] * 30), None, tol=0)
check("30 条序列 回撤应为 None", sr.max_drawdown([1.0] * 30), None, tol=0)

print()
if FAIL:
    print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
    sys.exit(1)
print("✅ 全部算法验证通过")
