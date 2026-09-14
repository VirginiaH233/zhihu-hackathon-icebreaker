# -*- coding: utf-8 -*-
"""产出质量检查（静态）—— 不调模型、不花额度、秒级完成

────────────────────────────────────────────────────────────
为什么需要这套测试？

原来 4 套测试（oauth / persist / duel / e2e）的断言全是 `assert d.get("ok")`
—— 只能证明「流程跑通了」，不能证明「产出是对的」。

实测踩到过：测试全绿，但线上某答主的档案内容是「我是知乎直答」的自我介绍。
测试没报错，因为它的职责里根本没有「看一眼产出」这一项。

  「测试有效性」的定义不是「测试通过」，而是「测试能不能发现真问题」。

────────────────────────────────────────────────────────────
设计原则：只抓「100% 是坏的」模式（高精度，不误报）

  质量判断天然模糊（「像不像本人」没法断言），所以这里不碰模糊区，
  只抓**确定性坏模式** —— 一旦命中，绝无可能是正常产出。

  代价：抓不到「不精彩但没错」的产出。
  好处：**几乎不可能误报** —— 所以它可以进部署阻塞链，不会因为模型
        偶发波动挡住开发。（「能测到问题」和「不影响开发」靠这一条同时成立）

────────────────────────────────────────────────────────────
用法：
  python tests/test_quality.py            # 检查本地 data/souls/ 里的缓存档案
  python tests/test_quality.py --online   # 额外拉线上 /api/stats 的报错记录
"""
import json
import os
import sys
import glob
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOULS_DIR = os.path.join(ROOT, "data", "souls")
ONLINE = "https://zhihu-hackathon-icebreaker-production.up.railway.app"

# ── 确定性坏模式：命中即判坏，不做例外 ──────────────────────────
BAD_PATTERNS = [
    # 直答的安全兜底自我介绍（本项目实际踩过）
    "知乎直答",
    # 模型自曝身份
    "我是一个 AI", "我是个 AI", "我是一个AI", "我是个AI",
    "是一个 AI 助手", "是一个AI助手", "作为 AI 助手", "作为AI助手",
    "人工智能助手", "语言模型", "大语言模型",
    # 拒答
    "我无法回答", "抱歉，我不能", "对不起，我不能",
]

# 档案最短字数：低于此值说明生成基本失败（正常档案 400~1500 字）
MIN_PERSONA_CHARS = 100


def check_persona(name, persona):
    """返回问题列表（空 = 没问题）"""
    problems = []
    p = (persona or "").strip()

    if len(p) < MIN_PERSONA_CHARS:
        problems.append(f"档案过短（{len(p)} 字 < {MIN_PERSONA_CHARS}）—— 生成很可能失败")

    for pat in BAD_PATTERNS:
        if pat in p:
            idx = p.find(pat)
            snippet = p[max(0, idx - 20): idx + 30].replace("\n", " ")
            problems.append(f"含坏模式「{pat}」→ ...{snippet}...")
            break  # 一个档案报一个坏模式就够，不刷屏

    return problems


def main():
    print("=" * 74)
    print("产出质量检查（静态 · 不调模型 · 不花额度）")
    print("=" * 74)

    files = sorted(glob.glob(os.path.join(SOULS_DIR, "*.json")))
    if not files:
        print("\n⚠️  data/souls/ 里没有档案（可能被重置过）—— 无可检查")
        print("\n全部通过（0 个档案）")
        return 0

    print(f"\n检查 {len(files)} 个缓存档案：\n")
    bad = 0
    for f in files:
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception as e:
            print(f"  ❌ {os.path.basename(f)}：文件读不出来（{e}）")
            bad += 1
            continue

        name = d.get("name") or os.path.basename(f)
        problems = check_persona(name, d.get("persona"))

        if problems:
            bad += 1
            print(f"  ❌ {name}")
            for pr in problems:
                print(f"       {pr}")
            print(f"       uid={d.get('uid')}  mode={d.get('mode')}")
        else:
            n = len((d.get("persona") or "").strip())
            lib = len(d.get("library") or [])
            print(f"  ✓ {name}（{n} 字 · {lib} 条素材 · {d.get('mode')}）")

    # 可选：线上报错里有没有质量相关的
    if "--online" in sys.argv:
        print(f"\n[线上] 最近报错记录：")
        try:
            with urllib.request.urlopen(f"{ONLINE}/api/stats", timeout=20) as r:
                st = json.load(r)
            errs = st.get("recent_errors") or []
            if not errs:
                print("  ✓ 无报错记录")
            else:
                for e in errs[-5:]:
                    print(f"  ⚠️ {e.get('type')} @ {e.get('path')} — {str(e.get('msg'))[:60]}")
        except Exception as e:
            print(f"  ⚠️ 拉不到线上数据（{e}）")

    print("\n" + "=" * 74)
    if bad:
        print(f"❌ 发现 {bad} 个问题档案 —— 这些档案会直接喂给用户，必须修")
        return 1
    print(f"全部通过（{len(files)} 个档案，0 个问题）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
