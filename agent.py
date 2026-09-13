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
    def _respond(self, user_msg: str, hits: list) -> str:
        if hits:
            lib_text = "\n".join(f"[{idx}] 《{d['title']}》：{d['summary']}" for idx, d, _ in hits)
            allowed = "、".join(f"[{idx}]" for idx, _, _ in hits)
        else:
            lib_text = "（检索无结果——资料库里没有相关内容）"
            allowed = "（无）"
        hist_text = "\n".join(f"用户：{u}\n分身：{a}" for u, a in self.history[-4:]) or "（这一轮是对话的开始）"
        prompt = f"""你是「{self.name}」的分身——基于 TA 公开内容构建的思维镜像，**不是 AI 助手**。

【TA 的人格】
{self.persona}

【检索到的资料】（内部参考，用户看不到）
{lib_text}

【对话历史】
{hist_text}

【用户的话】
{user_msg}

规则（违反任何一条都算失败）：
1. **只能引用上面给出的编号**：{allowed}。**严禁编造不存在的编号。**
2. 先从「检索到的资料」里找依据再回应，标【依据】[编号]。
3. **严禁编造资料里没有的具体细节**——数字、实验结论、案例、人名，资料里没有就**不准写**。
   不确定就说「这个细节我不确定」。
4. 若检索无结果，**必须诚实说「TA 好像没写过这个」**，可以说「我只能猜 TA 大概会…」。
5. 禁一切 AI 腔（禁「作为一个AI」「我很乐意」「希望对你有帮助」；禁列 1234 点）。
6. 用 TA 的口吻，长度 1–3 句，保持 TA 的立场、不迎合。
7. 不编造 TA 的具体经历；不假装知道 TA 的私生活。

输出格式（两段）：
【依据】…
【回应】…"""
        return self._llm(prompt)

    # ========== 完整的 Agent 循环 ==========
    def chat(self, user_msg: str) -> str:
        query = self._build_query(user_msg)              # 组装检索查询
        hits = self._retrieve(query)                     # ① 检索（代码真的执行）
        answer = self._respond(user_msg, hits)           # ② 回应
        self.history.append((user_msg, answer))          # ③ 记忆
        self.last_hits = hits
        if self.verbose:
            shown = [f"[{i}]{d['title'][:18]}({s})" for i, d, s in hits] or ["（无命中）"]
            print(f"  ① Retrieve → {shown}")
        return answer


# ==================== A2A：两个分身相遇 ====================
def _split_speech(raw: str) -> tuple:
    """把模型输出拆成 (依据, 发言)

    健壮性：模型偶尔会吐异常格式（只输出 `[2]` 这种编号、或把两段顺序写反），
    这里做一次校验——发言里如果只有编号/标点，视为无效。
    """
    raw = (raw or "").strip()
    ev, sp = "", raw
    m = re.search(r"【发言】\s*", raw)
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


SPEAK_RULES = """规则（违反任何一条都算失败）：
1. 你在**和一个人聊天**，不是回答问题、也不是汇报资料。可以追问、反驳、接梗、举例、说自己的看法、承认不知道。
2. **事实必须真**：TA 的观点、经历、结论、数字、案例、人名，只能来自上面给你的内容；有就写【依据】[编号]，**只能引用这些编号**：{allowed}。**严禁编造不存在的编号，也严禁编造内容里没有的具体细节。**
3. **说话方式不必有出处**：你的反应、感受、态度、追问、比方、常识都不需要依据，放开说。**你不需要每句话都有出处。**
4. **不许说「这个我没写过」「不确定我写过没有」这类自指的话**。碰到不熟悉的，就像人一样说：「这个我不太懂，你咋看？」「这块我不太熟，你说说。」
5. **每条发言至少包含一个「反应」**：对对方那句话的态度、感受，或一个追问。不能只输出自己的观点。
6. 长度 1–3 句，**说人话**。禁一切 AI 腔（禁「作为一个AI」「希望对你有帮助」），禁列 1234 点，禁总结句（「总的来说」「综上所述」）。
7. 保持 TA 的立场和口吻，**不要迎合对方**。该不同意就不同意，该追问就追问。
8. 禁客套（「很高兴认识你」「感谢分享」）。直接进入内容。
9. **【依据】里只写编号，或者留空。** 不要解释「为什么这轮没有依据」—— 那种解释是噪音。

输出格式（两段）：
【依据】…
【发言】…"""


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

    prompt = f"""{me}

【你（TA）的人格】
{self.persona}

【你（TA）写过的内容】（内部参考，对方看不到）
{lib_text}

【正在和你对话的人】
对方是「{peer_name}」{'' if not as_self else '——一个真实的人和 TA 的分身'}。
**你只看得到对方说的话，看不到对方的资料、经历、底细。**

【对话现场】
{scene}

【当前任务】
{task}

{SPEAK_RULES.format(allowed=allowed)}"""

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
