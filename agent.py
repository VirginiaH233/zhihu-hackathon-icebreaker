"""
分身 Agent —— 真 Agent 实现（检索 → 回应 + 记忆）

架构（v3，按实测约束收敛）:
    用户说话
      ↓
    ① Retrieve  代码在资料库里检索（真实执行，不是全量投喂）
      ↓
    ② Respond   模型基于「命中的这几条」回应，带 [ID] 依据
      ↓
    ③ Memory    存入会话历史，供下一轮检索使用

为什么砍掉「LLM Plan」这一步（v2 有）:
    实测发现直答**限流很严**（连续调用直接 rate_limit_exceeded，退避 6/12/18s 仍失败）。
    每轮 2 次调用会把体验拖垮。而「用代码检索」同样满足「真 Agent」的定义——
    工具是真的（代码执行检索）、循环是真的（检索→回应）、记忆是真的（history），
    只是查询词不用 LLM 生成，改用「用户的话 + 对话历史」，省掉一半调用。

v3 修复:
    - 检索加**阈值过滤**：所有条目低于阈值 → 返回空（保证「没命中时能诚实说不知道」）
    - 查询带上最近对话历史（多轮累积信息，弥补检索词质量）

用法:
    python agent.py            # 跑内置多轮对话演示
"""
import json
import re
import subprocess
import time
from pathlib import Path

import os
import shutil


def _find_cli() -> str:
    """定位 zhihu-cli：环境变量 → PATH → Windows 用户级安装目录（不写死用户名）"""
    if os.environ.get("ZHIHU_CLI"):
        return os.environ["ZHIHU_CLI"]
    for name in ("zhihu-cli", "zhihu-cli.exe"):
        exe = shutil.which(name)
        if exe:
            return exe
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cand = os.path.join(local, "ZhihuCLI", "current", "zhihu-cli.exe")
        if os.path.exists(cand):
            return cand
    return "zhihu-cli"


CLI = _find_cli()
ROOT = Path(__file__).parent
SIM_THRESHOLD = 0.03          # 相似度阈值：最高分低于此值 → 视为「没命中」


# ---------- 分身 prompt 模板：从文件读，改文件即改行为 ----------
# 规则与两个模板原来硬编码在本文件里 → 改 prompts/2-分身Agent.md 不生效，
# 也就没法对 prompt 做对比测试。现在文件是唯一真相。
_SOUL_TPL_PATH = ROOT / "prompts" / "2-分身Agent.md"
_soul_cache = None


def _soul_sections() -> dict:
    """读 prompts/2-分身Agent.md，按 `=== 段名 ===` 切段（rules / a2a / chat）。"""
    global _soul_cache
    if _soul_cache is None:
        text = _SOUL_TPL_PATH.read_text(encoding="utf-8")
        if text.startswith("---"):                      # 去掉 frontmatter
            text = text.split("---", 2)[2]
        parts = re.split(r"^===\s*(\w+)\s*===\s*$", text, flags=re.M)
        _soul_cache = {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
    return _soul_cache


def reload_soul_prompt() -> None:
    """清缓存 —— 改完模板文件后调用（测试脚本用）。"""
    global _soul_cache
    _soul_cache = None


def _fill(tpl: str, **kw) -> str:
    """把 {{占位符}} 换成真实内容。"""
    for k, v in kw.items():
        tpl = tpl.replace("{{" + k + "}}", str(v))
    return tpl


class SoulAgent:
    """一个「有工具、有循环、有记忆」的分身 Agent"""

    def __init__(self, name: str, persona: str, library: list, verbose: bool = True):
        self.name = name
        self.persona = persona              # 人格层
        self.library = library              # 资料库：[{'title','summary'}, ...]
        self.history = []                   # 记忆层
        self.verbose = verbose
        self.last_hits = []                 # 本轮命中（供调试/前端展示）
        self.key = ""                       # A2A 身份（"a"/"b"）
        self.is_self = False                # True = 「你自己」的分身（人是活的，不是知乎答主）

    # ========== 工具 1：调用 LLM（带限流重试） ==========
    def _llm(self, prompt: str, model: str = "zhida-thinking-1p5", retries: int = 3) -> str:
        last_err = ""
        for attempt in range(retries):
            r = subprocess.run(
                [CLI, "answer", "--model", model, "--query", prompt],
                capture_output=True, text=True, encoding="utf-8", timeout=200,
            )
            out = (r.stdout or "") + (r.stderr or "")
            if r.returncode == 0:
                try:
                    return json.loads(r.stdout)["choices"][0]["message"]["content"]
                except Exception:
                    pass
            last_err = out[:200]
            if "rate limit" in out or "rate_limit" in out:
                wait = 20 * (attempt + 1)          # 退避加长（实测 6/12/18s 不够）
                if self.verbose:
                    print(f"    ⏳ 触发限流，等 {wait}s 重试（{attempt+1}/{retries}）")
                time.sleep(wait)
                continue
            break
        raise RuntimeError(f"CLI 调用失败: {last_err}")

    # ========== 工具 2：检索（代码真的执行检索） ==========
    def _retrieve(self, query: str, top_k: int = 3) -> list:
        """纯代码检索：字符 2-gram 重叠度。带阈值过滤——没命中就返回空。"""
        q = query.lower()
        qg = set(q[i:i + 2] for i in range(len(q) - 1))
        if not qg:
            return []
        scored = []
        for i, doc in enumerate(self.library):
            text = (doc.get("title", "") + " " + doc.get("summary", "")).lower()
            dg = set(text[j:j + 2] for j in range(len(text) - 1))
            sim = len(qg & dg) / len(qg)
            scored.append((sim, i, doc))
        scored.sort(key=lambda x: (-x[0], x[1]))
        # 阈值过滤：最高分不够 → 视为没命中
        if not scored or scored[0][0] < SIM_THRESHOLD:
            return []
        return [(i + 1, doc, round(sim, 3)) for sim, i, doc in scored[:top_k] if sim >= SIM_THRESHOLD]

    def _build_query(self, user_msg: str) -> str:
        """检索查询 = 本轮用户的话 + 最近两轮对话（多轮累积信息）"""
        recent = " ".join(u for u, _ in self.history[-2:])
        return f"{recent} {user_msg}"

    # ========== 循环 ②：Respond（基于检索结果回应） ==========
    def _respond(self, user_msg: str, hits: list, topic: str = "") -> str:
        if hits:
            lib_text = "\n".join(f"[{idx}] 《{d['title']}》：{d['summary']}" for idx, d, _ in hits)
            allowed = "、".join(f"[{idx}]" for idx, _, _ in hits)
        else:
            lib_text = "（检索无结果——资料库里没有相关内容）"
            allowed = "（无）"
        hist_text = "\n".join(f"用户：{u}\n分身：{a}" for u, a in self.history[-4:]) or "（这一轮是对话的开始）"
        topic_block = (f"\n【你们在聊的话题】{topic}\n"
                       f"（话题只是由头，不是要交的作业 —— 聊着聊着跑偏了也没关系）\n") if topic else ""
        _sec = _soul_sections()
        _rules = _fill(_sec["rules"], **{"允许的编号": allowed})
        prompt = _fill(_sec["chat"], **{
            "身份": f"你是「{self.name}」的分身——基于 TA 公开内容构建的思维镜像，**不是 AI 助手**。",
            "人格": self.persona,
            "资料库": lib_text,
            "话题块": topic_block,
            "历史": hist_text,
            "用户的话": user_msg,
            "规则": _rules,
        })
        return self._llm(prompt)

    # ========== 完整的 Agent 循环 ==========
    def chat(self, user_msg: str, topic: str = "") -> str:
        query = self._build_query(user_msg)              # 组装检索查询
        hits = self._retrieve(query)                     # ① 检索（代码真的执行）
        answer = self._respond(user_msg, hits, topic)    # ② 回应
        self.history.append((user_msg, answer))          # ③ 记忆
        self.last_hits = hits
        if self.verbose:
            shown = [f"[{i}]{d['title'][:18]}({s})" for i, d, s in hits] or ["（无命中）"]
            print(f"  ① Retrieve → {shown}")
        return answer


# ==================== 第四幕：开局（TA 的分身先开口 + 双模块话题）====================
def build_opening_prompt(agent: "SoulAgent", prompt_template: str, my_lib: list | None,
                         my_state: str) -> str:
    """拼出「开场白 + 两组话题」的 prompt（见 prompts/4c-开场与话题.md）。

    - agent     = TA 的分身（提供人格档案与资料库）
    - my_lib    = 我的资料库（没生成过就是 None → 模块2 留空）
    - my_state  = 给我的档案状态的一句话说明（让模型知道该不该生成模块2）
    只负责拼 prompt（LLM 调用由 server 走 cli_answer，与破冰卡/匹配卡同一条路）。
    """
    ta_lib = "\n".join(f"[{i}] 《{d['title']}》：{d['summary']}"
                       for i, d in enumerate(agent.library, 1)) or "（TA 没有可用的公开内容）"
    mine = ("\n".join(f"[{i}] 《{d['title']}》：{d['summary']}"
                      for i, d in enumerate(my_lib, 1)) if my_lib else "（还没有：用户尚未生成自己的档案）")
    body = (prompt_template
            .replace("{TA 的名字}", agent.name)
            .replace("{TA 的灵魂档案}", agent.persona)
            .replace("{TA 的资料库}", ta_lib)
            .replace("{我的资料库}", mine)
            .replace("{我的档案状态}", my_state))
    hdr = "以下是这次的真实输入，严格据此生成：\n\n"
    return hdr + body


# ==================== A2A：两个分身相遇 ====================
def _split_speech(raw: str) -> tuple:
    """把模型输出拆成 (依据, 发言)

    健壮性：模型偶尔会吐异常格式（只输出 `[2]` 这种编号、或把两段顺序写反），
    这里做一次校验——发言里如果只有编号/标点，视为无效。
    """
    raw = (raw or "").strip()
    ev, sp = "", raw
    # ⚠️ 两个段名都要认：A2A 用【发言】，「人 × TA 的分身」用【回应】
    m = re.search(r"【(?:发言|回应)】\s*", raw)
    if m:
        sp = raw[m.end():].strip()
        ev = re.sub(r"^【依据】\s*", "", raw[:m.start()].strip()).strip()
    # 无效发言（纯编号/符号）→ 尝试从后半段再捞一次
    if re.fullmatch(r"[\[\]\d\s、,，。\.\-—…：:；;]*", sp or ""):
        tail = re.split(r"【依据】", sp)[-1] if sp else ""
        cand = tail.strip()
        if len(cand) > 6:
            sp = cand
    return ev, sp


# 分身规则与两个模板已移到 `prompts/2-分身Agent.md`（改文件即改行为）。
# 以前这套规则硬编码在这里 —— 结果是「改 prompt 文件对产品没有任何影响」，
# 也就没法给 prompt 做对比测试。别再搬回来。


def speak_to(self, peer_key: str, peer_name: str, dialogue: list,
             topic: str = "", is_opener: bool = False, as_self: bool = False) -> dict:
    """A2A 核心：以分身身份对「另一个分身」发言。

    信息隔离（这是「真 A2A」的关键）：
      只读得到对方**说过的话**，读不到对方的资料库、人格、底细。
    """
    peer_last = ""
    for t in reversed(dialogue):
        if t["key"] != self.key:
            peer_last = t["text"]
            break

    # 检索自己的资料库（对方的话 = 检索线索）
    query = f"{topic} {peer_last}" if peer_last else f"{topic} {self.name}"
    hits = self._retrieve(query)
    self.last_hits = hits
    if hits:
        lib_text = "\n".join(f"[{i}] 《{d['title']}》：{d['summary']}" for i, d, _ in hits)
        allowed = "、".join(f"[{i}]" for i, _, _ in hits)
    else:
        lib_text = "（检索无结果——你的资料里没有相关的）"
        allowed = "（无）"

    scene = "\n".join(f"「{t['name']}」：{t['text']}" for t in dialogue) or "（还没有人开口）"

    if as_self:
        me = (f"你就是「{self.name}」本人的分身——用 TA 平时说话的方式参与这场对话，"
              f"代表的是 TA 真实的想法和关心的东西。")
        task = ("这是对话的开始，**由你主动开口**。说一句你真正想说的、或你真正想知道的。"
                "直接进入你关心的事，不要问候客套。")
    else:
        me = f"你是「{self.name}」的知乎人格分身——基于 TA 在知乎的公开内容构建的思维镜像。"
        task = (f"对方（「{peer_name}」）刚说了：\n「{peer_last}」\n"
                f"先对 TA 这句话有个反应（同意、不同意、追问、接过话头），再说你自己的看法。")

    if is_opener and not as_self:
        task = "这场对话由你主动开口。说一句你真正关心、或想问对方的事。直接进入内容，不要问候客套。"
    if is_opener and as_self:
        task = ("这场对话由你主动开口。你在知乎看到了「%s」这个人，想认识 TA。"
                "先说一句你真正想说的或想知道的——**不要自我介绍开场**，直接进入内容。" % peer_name)

    _sec = _soul_sections()
    _rules = _fill(_sec["rules"], **{"允许的编号": allowed})
    _peer = (f"对方是「{peer_name}」"
             f"{'' if not as_self else '——一个真实的人和 TA 的分身'}。\n"
             "**你只看得到对方说的话，看不到对方的资料、经历、底细。**")
    prompt = _fill(_sec["a2a"], **{
        "身份": me,
        "人格": self.persona,
        "资料库": lib_text,
        "对方": _peer,
        "现场": scene,
        "任务": task,
        "规则": _rules,
    })

    # 生成 + 校验：模型偶发会吐异常格式（只输出编号），检出就重试一次
    ev, sp = "", ""
    for attempt in range(2):
        ev, sp = _split_speech(self._llm(prompt))
        if len(sp) >= 8 and not re.fullmatch(r"[\[\]\d\s、,，。\.\-—…：:；;]*", sp):
            break
        if self.verbose:
            print(f"    ⚠️ 第 {attempt+1} 次输出无效（{sp[:20]!r}），重试")
        if attempt == 0:
            time.sleep(2)
    return {"依据": ev, "发言": sp or "（这次没说话）"}


def run_duel(a: "SoulAgent", b: "SoulAgent", topic: str = "", rounds: int = 3) -> list:
    """A2A：两个分身交替对话。A 先开口，共 rounds 轮（每轮 A、B 各说一次）。"""
    a.key, b.key = "a", "b"
    dialogue = []
    for r in range(rounds):
        for agent, peer in ((a, b), (b, a)):
            opener = (r == 0 and agent is a)
            try:
                out = speak_to(agent, peer.key, peer.name, dialogue, topic,
                               is_opener=opener, as_self=getattr(agent, "is_self", False))
            except Exception as e:
                out = {"依据": "", "发言": f"（对话中断：{e}）"}
            turn = {
                "key": agent.key, "name": agent.name,
                "text": out["发言"], "evidence": out["依据"],
                "hits": [{"id": i, "title": d["title"], "sim": s} for i, d, s in agent.last_hits],
            }
            dialogue.append(turn)
    return dialogue


def build_self_agent(intro: str, llm=None, name: str = "我", forge: bool = False) -> "SoulAgent":
    """从用户的三句话建「你的分身」。

    数据量自适应：3 句话也能聊——生成的不是「更多事实」，是「这个人关心什么」。

    forge=False（默认）：直接拿用户的原话当人格。**更真诚**（那是 TA 自己说的话，
        不是 AI 总结的），而且省一次 LLM 调用（直答额度只有 100/天）。
    forge=True：用 LLM 生成结构化档案，质量略高但烧额度。

    name 用用户的昵称（别用「你」——那会和 prompt 里的第二人称打架）。
    """
    if forge and llm:
        p = (ROOT / "prompts" / "1-灵魂档案.md").read_text(encoding="utf-8")
        p = p.split("---", 2)[2].strip() if p.startswith("---") else p
        persona = llm(f"{p}\n\n【这个人自己说的话】\n{intro}\n\n"
                      "注意：这是用户自己写的三句话，信息量很少。**信息密度低就如实标注**，"
                      "不要硬编造这个人没体现过的特质。")
    else:
        persona = ("（以下是本人自己写下的原话。信息密度低——不要替 TA 编造没写过的经历、"
                   "身份或观点，就照这些话所透露的样子说话。）\n" + intro)

    lib = []
    for line in re.split(r"[\n。；;]", intro):
        line = line.strip()
        if len(line) >= 4:
            lib.append({"title": line[:40], "summary": line[:200]})
    agent = SoulAgent(name=(name.strip() or "我"), persona=persona,
                      library=lib or [{"title": intro[:40], "summary": intro[:200]}],
                      verbose=False)
    agent.is_self = True
    return agent


# ==================== 演示 ====================
def demo():
    sample = json.loads((ROOT / "tests" / "samples" / "trancy.json").read_text(encoding="utf-8"))
    agent = SoulAgent(
        name=sample["A"]["name"],
        persona=(
            "核心关切：推理能力、Agent 自主性架构、一手论文权威性、LLM 与知识图谱结合。\n"
            "表达风格：结构化谱系式叙述、行动导向的劝学口吻。\n"
            "价值排序：一手信源 > 可解释性 > 系统性理解 > 实践验证。\n"
            "一句话侧写：别人看模型的热闹，她拆机制的骨架——一切结论回到一手论文。"
        ),
        library=[{"title": x["title"], "summary": x["summary"]} for x in sample["A"]["renders"]],
    )

    print("=" * 74)
    print(f"分身 Agent v3 演示 · {agent.name}（资料库 {len(agent.library)} 条 | 阈值 {SIM_THRESHOLD}）")
    print("=" * 74)

    turns = [
        "你好，我是大学生，想入门 AI，看了你的 CoT 总览很有收获。你觉得我该怎么入门？",
        "复现论文太花时间了，看视频讲解不也一样吗？感觉你就是想让我受苦。",
        "我最近很纠结：该考研还是直接工作？",
    ]
    for i, msg in enumerate(turns, 1):
        print(f"\n【第 {i} 轮】用户：{msg}")
        print("-" * 74)
        try:
            print(agent.chat(msg))
        except Exception as e:
            print(f"  ❌ {e}")
            time.sleep(20)
    print("\n" + "=" * 74)
    print(f"记忆层：本次会话共 {len(agent.history)} 轮")


if __name__ == "__main__":
    demo()
