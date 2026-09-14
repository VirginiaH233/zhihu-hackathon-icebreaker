"""端到端实测：load → A2A → 破冰卡（走 HTTP，和前端完全同一条路）

跑法：python tests/test_e2e.py [昵称]
"""
import glob
import json
import os
import sys
import time
import urllib.request

# 复用静态检查器的「确定性坏模式」清单 —— 一处定义、两处使用
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_quality import BAD_PATTERNS, MIN_PERSONA_CHARS  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def quality_check(text, where):
    """产出质量断言：只抓 100% 是坏的模式（高精度 → 不会因模型波动误报）

    这就是「测试有效性」的补丁：原来的断言只有 `ok`（流程通不通），
    这条补的是「产出对不对」。实测踩过：流程全绿，但产出是「我是知乎直答」。
    """
    bad = [p for p in BAD_PATTERNS if p in (text or "")]
    if bad:
        raise AssertionError(
            f"{where} 的产出含坏模式 {bad} —— 流程能跑通、但内容不可用\n"
            f"      原文：{(text or '')[:160]}")


def check_ta_persona(ta_name):
    """检查 TA 的缓存档案（毒素材会让档案变成模型的自我介绍）"""
    for f in glob.glob(os.path.join(ROOT, "data", "souls", "*.json")):
        try:
            j = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        if j.get("name") != ta_name:
            continue
        per = (j.get("persona") or "").strip()
        bad = [q for q in BAD_PATTERNS if q in per]
        if bad:
            raise AssertionError(
                f"{ta_name} 的档案含坏模式 {bad} —— 这份档案会直接喂给用户\n"
                f"      人脸：{per[:160]}")
        if len(per) < MIN_PERSONA_CHARS:
            raise AssertionError(
                f"{ta_name} 的档案只有 {len(per)} 字（< {MIN_PERSONA_CHARS}）—— 生成很可能失败")
        return f"{len(per)} 字"
    return "（缓存里没找到）"

BASE = "http://127.0.0.1:8000"
NICK = sys.argv[1] if len(sys.argv) > 1 else "梁边妖"
# 「我」的身份：产品规则要求先有分身才能开始对话（不再有「跳过、用一句自我介绍代班」）
TEST_UID = "e2e_tester"
MY_PERSONA = ("## 我的关注\n- 产品与人的沉迷机制\n- 小众运动 / 把兴趣变成专业\n"
              "## 表达\n- 爱用反问句，先给判断再给理由")

INTRO = """我是一个在中国做产品的人，业余喜欢研究各种小众运动，也爱琢磨人为什么会沉迷一件事。
我最近在想：为什么很多人觉得「玩」是不务正业，明明玩才是人恢复精力的方式。
我想认识一个把「玩」当正事做的人，想知道你是怎么把兴趣变成专业的。"""


# ⚠️ 绕过系统代理：本机代理对 localhost 请求会瞬时 502（部署时真踩过，导致误判「测试没过」）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post(path, data, timeout=900):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    return _OPENER.open(req, timeout=timeout)


def main():
    t0 = time.time()
    print("=" * 74)
    print(f"端到端实测 · TA = {NICK}")
    print("=" * 74)

    # ---------- 1. candidates → load ----------
    print(f"\n[1] POST /api/candidates  ({NICK})")
    d = json.loads(post("/api/candidates", {"name": NICK}).read().decode("utf-8"))
    assert d.get("ok"), f"candidates 失败: {d}"
    cands = d["candidates"]
    print(f"    ✅ 候选 {len(cands)} 个（同名靠签名区分）：")
    for c in cands[:5]:
        print(f"        · {c['name']:12} 签名「{c['signature']}」")
    pick = cands[0]
    print(f"\n[2] POST /api/load  ({pick['name']} / {pick['signature']})")
    d = json.loads(post("/api/load", {"name": pick["name"],
                                      "signature": pick["signature"],
                                      "user_id": TEST_UID}).read().decode("utf-8"))
    assert d.get("ok"), f"load 失败: {d}"
    sid, ta_name, cnt = d["session_id"], d["name"], d["count"]
    print(f"    ✅ ok · 作者={ta_name} · 资料 {cnt} 条 · 缓存={d.get('cached')} · 耗时 {time.time()-t0:.0f}s")

    # ── 产出质量：TA 的档案（毒素材会把它变成模型的自我介绍）──────────
    q = check_ta_persona(ta_name)
    print(f"    ✅ 档案体检通过（{q}）")

    # ---------- 2.5 先有「我的分身」 ----------
    print("\n[2.5] POST /api/me  (先造出我的分身)")
    d = json.loads(post("/api/me", {"user_id": TEST_UID, "name": "老周",
                                    "persona": MY_PERSONA}).read().decode("utf-8"))
    assert d.get("ok"), f"建我的分身失败: {d}"
    print(f"    ✅ 我的分身已就位（source={d.get('source')}）")

    # ---------- 3. duel (SSE) ----------
    print(f"\n[3] POST /api/duel  (SSE 流式)")
    r = post("/api/duel", {"session_id": sid, "intro": INTRO,
                           "my_name": "老周", "user_id": TEST_UID, "rounds": 3})
    # ⚠️ 必须累积**字节**再按事件边界解码：逐块 decode 会把跨块的中文（多字节）直接丢掉
    turns, buf, t_duel = [], b"", time.time()
    while True:
        chunk = r.read(4096)
        if not chunk:
            break
        buf += chunk
        while b"\n\n" in buf:
            raw, buf = buf.split(b"\n\n", 1)
            line = raw.decode("utf-8")
            if not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            if ev["type"] == "turn":
                quality_check(ev.get("text"), f"第{len(turns)+1}轮 · {ev.get('name')}的分身")
                turns.append(ev)
                who = f"●{ev['name']}的分身" if ev["key"] == "a" else f"★{ev['name']}的分身"
                print(f"    [{time.time()-t_duel:5.1f}s] {who}")
                print(f"          {ev['text'][:170]}")
                if ev.get("evidence"):
                    print(f"          ↳依据 {ev['evidence'][:80]}")
                if ev.get("hits"):
                    print(f"          ↳命中 {', '.join(h['title'][:16] for h in ev['hits'])}")
            elif ev["type"] == "error":
                print(f"    ❌ {ev['error']}")
    print(f"    ✅ 共 {len(turns)} 轮 · 耗时 {time.time()-t_duel:.0f}s")

    # ---------- 4. icebreak ----------
    print(f"\n[4] POST /api/icebreak")
    d = json.loads(post("/api/icebreak", {
        "session_id": sid, "intro": INTRO, "dialogue": turns
    }).read().decode("utf-8"))
    assert d.get("ok"), f"破冰卡失败: {d}"
    quality_check(d.get("card"), "破冰卡")
    print("    ✅ 破冰卡（已过质量体检）：")
    print("    " + "\n    ".join(d["card"].split("\n")))

    print("\n" + "=" * 74)
    print(f"全链路通过 · 总耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
