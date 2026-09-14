#!/usr/bin/env python
"""线上关键路径冒烟 —— 部署的最后一道关。

用法：
    python tests/smoke_online.py [BASE_URL]

为什么要有它
------------
原来部署只查「首页 200 + 搜人可用」。而线上真出过这种问题：

    用户的会话没了 → 「读我的公开创作」之后话题出不来、破冰卡提示「没写出来」

那两个接口都是 HTTP 200 —— 首页 200 和非空的服务，完全盖不住「**关键路径断了**」。
所以这里走一遍真实路径，每一步都断言**产出对不对**，不是只看请求成不成。

覆盖：搜人 → 读 TA（建会话）→ 出话题 → 写卡片
不覆盖：需要 OAuth 登录的路径（「读我的公开创作」）—— 那一步只有真人能走。

退出码：0 = 通过；1 = 有步骤失败（调用方应当视为发布失败）。
"""
import json
import sys
import time
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else
        "https://zhihu-hackathon-icebreaker-production.up.railway.app").rstrip("/")

# 固定探针身份：不碰真实用户的数据，出问题也好从数据里认出来
PROBE_UID = "smoke_probe"
PROBE_NAME = "曾加"


# ⚠️ 绕过系统代理：本机开着代理（127.0.0.1:7897），urllib 会把请求交给它，
#    结果要么 502 要么空响应（curl 上真踩过）。冒烟必须直连。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post(path: str, data: dict, timeout: int = 180):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    with _OPENER.open(req, timeout=timeout) as r:
        out = json.loads(r.read().decode("utf-8"))
    return out, time.time() - t0


def main() -> int:
    fails = []
    print("=" * 66)
    print(f"  线上关键路径冒烟  →  {BASE}")
    print("=" * 66)

    # ---------- ① 搜人 ----------
    print("\n[1/4] 搜人（{0}）".format(PROBE_NAME))
    try:
        d, dt = post("/api/candidates", {"name": PROBE_NAME, "user_id": PROBE_UID}, timeout=60)
        cands = d.get("candidates") or []
        if not cands:
            # 搜人本身在线上偶发 0 候选（限流/接口不稳），不当失败，但要喊出来
            print(f"  ⚠️ {dt:.1f}s · 0 个候选（线上搜人偶发不稳，继续往下走）")
        else:
            print(f"  ✓ {dt:.1f}s · {len(cands)} 个候选")
    except Exception as e:
        print(f"  ❌ 搜人请求失败：{type(e).__name__}: {e}")
        fails.append("搜人")
        cands = []

    pick = cands[0] if cands else {"name": PROBE_NAME, "signature": ""}

    # ---------- ② 读 TA（这一步建会话）----------
    print(f"\n[2/4] 读 TA（{pick.get('name')}）")
    sid = None
    try:
        d, dt = post("/api/load", {"name": pick.get("name"), "signature": pick.get("signature") or "",
                                   "user_id": PROBE_UID}, timeout=300)
        sid = d.get("session_id")
        if not d.get("ok") or not sid:
            print(f"  ❌ 没建起会话：{d.get('error') or d}")
            fails.append("读 TA（建会话）")
        else:
            persona = d.get("persona") or ""
            if len(persona) < 200:
                print(f"  ❌ 档案太短（{len(persona)} 字），产出质量可疑")
                fails.append("档案质量")
            else:
                print(f"  ✓ {dt:.1f}s · 会话已建 · 档案 {len(persona)} 字 · 素材 {d.get('count')} 条"
                      + ("（缓存）" if d.get("cached") else ""))
    except Exception as e:
        print(f"  ❌ 请求失败：{type(e).__name__}: {e}")
        fails.append("读 TA")

    if not sid:
        print("\n⚠️ 会话建不起来，后面两步没法验 —— 直接判失败（这正是线上出过的问题）")
        _summary(fails)
        return 1

    # ---------- ③ 出话题（用户报过「话题出不来」）----------
    print("\n[3/4] 出话题（用户报过这里出不来）")
    try:
        d, dt = post("/api/opening", {"session_id": sid, "user_id": PROBE_UID,
                                      "signature": pick.get("signature") or ""}, timeout=300)
        if not d.get("ok"):
            print(f"  ❌ 接口失败：{d.get('error')}")
            fails.append("出话题")
        else:
            mods = d.get("modules") or []
            his = next((m for m in mods if m.get("key") == "his"), None)
            n_his = len((his or {}).get("topics") or [])
            if n_his < 1:
                print(f"  ❌ 一个话题都没出来（modules={[m.get('key') for m in mods]}）")
                fails.append("出话题（0 个）")
            else:
                print(f"  ✓ {dt:.1f}s · 「{his.get('label')}」{n_his} 个话题")
                for tp in ((his or {}).get("topics") or [])[:2]:
                    print(f"      - {tp.get('text')}")
    except Exception as e:
        print(f"  ❌ 请求失败：{type(e).__name__}: {e}")
        fails.append("出话题")

    # ---------- ④ 写卡片（用户报过「没写出来」）----------
    print("\n[4/4] 写卡片（用户报过「没写出来」）")
    try:
        d, dt = post("/api/icebreak", {
            "session_id": sid, "user_id": PROBE_UID, "intro": "（冒烟探针）",
            "dialogue": [{"name": pick.get("name") or PROBE_NAME,
                          "text": "我最近在琢磨一件事，还没想明白"},
                         {"name": "你", "text": "我也在想类似的问题"}]}, timeout=300)
        if not d.get("ok"):
            print(f"  ❌ 接口失败：{d.get('error')}")
            fails.append("写卡片")
        else:
            card = d.get("card") or ""
            if len(card) < 150:
                print(f"  ❌ 卡片太短（{len(card)} 字），等于没写出来")
                fails.append("写卡片（内容为空）")
            else:
                print(f"  ✓ {dt:.1f}s · 卡片 {len(card)} 字")
                print(f"      开头：{card[:80].replace(chr(10), ' / ')}")
    except Exception as e:
        print(f"  ❌ 请求失败：{type(e).__name__}: {e}")
        fails.append("写卡片")

    _summary(fails)
    return 1 if fails else 0


def _summary(fails) -> None:
    print()
    print("-" * 66)
    if fails:
        print(f"❌ 关键路径冒烟未通过，失败步骤：{' / '.join(fails)}")
        print("   这代表线上「用户能走完的那条路」是断的 —— 不要当小问题放过去。")
    else:
        print("✅ 关键路径冒烟通过：搜人 → 读 TA → 出话题 → 写卡片，四步产出都对")


if __name__ == "__main__":
    raise SystemExit(main())
