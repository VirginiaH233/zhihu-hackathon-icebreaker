"""
盐之有理 · 后端服务（FastAPI）

职责：
  1. 代理 zhihu-cli（凭证只存在于本机，绝不进前端）
  2. 提供三个接口：取料+生成灵魂档案 / 对话 / 额度
  3. 维护会话（session_id → SoulAgent 实例）

启动：
    python server.py
    浏览器打开 http://127.0.0.1:8000
"""
import base64
import io
import json
import re
import subprocess
import time
import uuid
import hashlib
import http.cookiejar
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse, RedirectResponse, JSONResponse
from pydantic import BaseModel

import oauth
from agent import SoulAgent, build_self_agent, run_duel

ROOT = Path(__file__).parent
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

app = FastAPI(title="社恐破冰船")


@app.exception_handler(Exception)
async def _on_unhandled(request: Request, exc: Exception):
    """未捕获异常 → 记进事件流，再返回结构化错误。

    以前线上 500 只躺在 Railway 日志里，外部完全看不到，排查全靠猜（分享卡那次）。
    记进事件流后，/api/stats 的 recent_errors 就能直接看到最近出了什么错。
    """
    _event("error", path=str(request.url.path), err=f"{type(exc).__name__}: {exc}"[:200])
    return JSONResponse(status_code=500,
                        content={"ok": False, "error": "服务器出了点问题，再试一次"})


SESSIONS: dict[str, SoulAgent] = {}      # session_id -> Agent
SOUL_CACHE: dict[str, dict] = {}         # 昵称 -> {persona, library, name}（省额度：同名不重建）

# ============================================================
# 持久化：分身是资产，不是一次性调用
# ============================================================
DATA = ROOT / "data"
USERS_DIR = DATA / "users"     # 你的分身（持久，可编辑）
SOULS_DIR = DATA / "souls"     # TA 的灵魂档案（缓存，可复用、可预热）
HISTORY_DIR = DATA / "history" # 每个用户「聊过谁」（按 uid 归属，给个人抽屉的入口用）
EVENTS_DIR = DATA / "events"   # 使用事件流（按天 jsonl）—— 漏斗分析用

USERS_DIR.mkdir(parents=True, exist_ok=True)
SOULS_DIR.mkdir(parents=True, exist_ok=True)


def _safe_key(s: str) -> str:
    """标识符白名单，防路径穿越。"""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", s or ""):
        raise HTTPException(status_code=400, detail="非法的标识符")
    return s


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(path: Path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")

# ============================================================
# 工具函数
# ============================================================

def cli_raw(args: list, retries: int = 3) -> str:
    """调用 zhihu-cli，带限流重试。返回 stdout。"""
    last = ""
    for attempt in range(retries):
        r = subprocess.run([CLI] + args, capture_output=True, text=True,
                           encoding="utf-8", timeout=200)
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode == 0 and "rate_limit" not in out:
            return r.stdout
        last = out[:200]
        if "rate limit" in out or "rate_limit" in out:
            time.sleep(20 * (attempt + 1))
            continue
        break
    raise HTTPException(status_code=502, detail=f"CLI 调用失败: {last}")


def cli_answer(query: str, model: str = "zhida-thinking-1p5") -> str:
    """调直答，返回纯文本。"""
    raw = cli_raw(["answer", "--model", model, "--query", query])
    d = json.loads(raw)
    return d["choices"][0]["message"]["content"]


def clean(text: str) -> str:
    """去掉 HTML 标签、压缩空白。"""
    text = re.sub(r"<[^>]+>", " ", text or "")
    for a, b in (("&nbsp;", " "), ("&quot;", '"'), ("&amp;", "&"), ("&#34;", '"')):
        text = text.replace(a, b)
    return re.sub(r"\s+", " ", text).strip()


# ============================================================
# 核心流程
# ============================================================

# 不用 take_my_contents()（用 CLI 凭证读「凭证所属账号」的内容）
#
# 原因：CLI 的 Access Secret 固定属于一个账号，代表不了任何真实用户，
# 用它来充当「用户自己的创作」在语义上就不成立。
#
# 现在的做法：生成「你的分身」必须先走 OAuth 授权，
# 用 oauth.fetch_user_contents(user_access_token) 读**该用户自己**的创作。


# 每条「TA 的公开创作」喂给模型的正文字数上限。
# 实测：搜索返回的 ContentText 通常 300–1068 字，而 Summary 字段一直是空的。
# 旧代码只截 220 字 —— 一半以上素材被扔掉，模型会在档案里抱怨「正文被截断」。
BODY_LIMIT = 1200

# 取料方式一改就 +1：缓存里记了这个版本号，旧档案会被判为过期、自动重建。
# （不升版本号的话，改了取料也读不到效果 —— 旧的档案还在缓存里）
# v6（2026-09-14）：补「想法」的阈值从 <3 提到 <8 —— 取料结果变了，旧缓存（可能只
#                   抓到三五条）必须重建，否则修了也白修。
MATERIAL_VERSION = 6

# 产品对外地址 —— 分享卡上的二维码指向它（带 ?from=card，将来能看扫码来源）。
# 线上用环境变量覆盖；本地开发默认线上地址（本地二维码也扫得出、跳线上）。
SITE_URL = os.environ.get(
    "SITE_URL", "https://zhihu-hackathon-icebreaker-production.up.railway.app").rstrip("/")


def make_qr_datauri(url: str) -> str:
    """把链接画成二维码，返回 data:image/png;base64 —— 卡片导出时不依赖网络。"""
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M
        q = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=10, border=1)
        q.add_data(url)
        q.make(fit=True)
        img = q.make_image(fill_color="#22262b", back_color="white").convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        _ZHIHU_ERR["qr"] = f"{type(e).__name__}: {e}"[:120]
        return ""


def _search_items(query: str, count: int = 10, retries: int = 2) -> list:
    """搜一次知乎，返回 items 列表。

    ⚠️ 实测这个接口偶尔会返回 **空 Items**（不是报错）——直接透传会让用户看到
    「没搜到这个人」，而其实 TA 存在。所以空结果先重试一次再认。
    """
    for attempt in range(retries):
        raw = cli_raw(["search", "zhihu", "--query", query, "--count", str(count)])
        items = json.loads(raw).get("Data", {}).get("Items", []) or []
        if items:
            return items
        if attempt + 1 < retries:
            time.sleep(1.5)
    return []


# 「按名字搜内容」这个接口上限就是 10 条（传 30 直接 502，实测），
# 而且同一个名字换个查询词，命中的作者能差一大截 ——
# 实测：「王瑞恩」搜不到本人，「王瑞恩 文章」就能搜到。所以不够就换词再搜，合并去重。
# ⚠️ 只留 3 个：实测「王瑞恩 文章」第 2 个就命中，5 个换词会让最坏情况涨到 20 秒+。
# 时间预算比覆盖率重要 —— 搜不到的还能走「粘主页链接」。
SEARCH_VARIANTS = ["{} 回答", "{} 文章", "{} 想法"]


def _item_key(it: dict) -> str:
    return ((it.get("AuthorSignature") or "") + "|" + clean(it.get("Title", ""))[:60])


def _search_merged(name: str, enough) -> tuple:
    """按名字搜；这批「不够」就换几种词再搜，合并去重。

    enough(items) -> bool：这批够不够（找人是「有没有同名作者」，取料是「本人内容够几条」）。
    先用名字搜一次就够的话，**不会有任何额外开销**（大多数人走这条）。
    """
    items = _search_items(name) or []
    if enough(items):
        return items, [name]
    seen = {_item_key(it) for it in items}
    used = [name]
    for v in SEARCH_VARIANTS:
        q = v.format(name)
        try:
            more = _search_items(q) or []
        except Exception:
            continue
        used.append(q)
        for it in more:
            k = _item_key(it)
            if k and k not in seen:
                seen.add(k)
                items.append(it)
        if enough(items):
            break
    return items, used


def _item_is_author(it: dict, name: str, signature: str) -> bool:
    """这条内容是不是目标作者本人写的。"""
    it_sig = (it.get("AuthorSignature") or "").strip()
    it_name = (it.get("AuthorName") or "").strip()
    if signature:
        return it_sig == signature
    return it_name == name.strip()


def search_candidates(nickname: str) -> list:
    """搜昵称 → 列出候选作者（头像 + 名字 + 签名，去重）。

    开放 API 没有「搜人」接口，只能搜内容；但每条内容带作者头像/名字/签名，
    据此把「可能是 TA」的作者全部列出来，让用户点选确认——而不是自动取第一个。

    ⭐ 例外：如果输入的是**主页链接或裸 token**（如 wenbo），
    就走知乎网页版公开接口直接把这个人读出来 —— 这条路必中，
    而且能拿到真名（token 只是主页标识，用户未必知道 TA 的中文名）。
    """
    token = extract_zhihu_token(nickname)
    if token:
        m = fetch_member(token)
        if m:
            return [{
                "name": m.get("name") or token,
                "signature": m.get("url_token") or token,
                "avatar": m.get("avatar_url") or "",
                "sample": (m.get("headline") or "（TA 没写一句话介绍）")[:36],
                "exact": True,
                "by_token": True,
                "stats": {"answer": m.get("answer_count") or 0,
                          "article": m.get("articles_count") or 0,
                          "follower": m.get("follower_count") or 0},
            }]
        # token 读不出来（可能用户其实在输名字）→ 继续走下面的内容搜索

    key = nickname.strip()
    items, _used = _search_merged(
        nickname, lambda its: any((x.get("AuthorName") or "").strip() == key for x in its))
    seen = {}
    for it in items:
        name = it.get("AuthorName") or ""
        sig = it.get("AuthorSignature") or ""
        if not name:
            continue
        ident = sig or name
        if ident not in seen:
            seen[ident] = {
                "name": name,
                "signature": sig,
                "avatar": it.get("AuthorAvatar") or "",
                "sample": clean(it.get("Title", ""))[:36],
                # 名字完全等于用户输入 → 大概率就是要找的人，前端可以标一下
                "exact": name.strip() == key,
            }
    cands = list(seen.values())
    # 完全同名最前，其次名字包含，其余按原顺序
    cands.sort(key=lambda c: 0 if c["name"].strip() == key else (1 if key in c["name"] else 2))
    return cands


def take_by_signature(name: str, signature: str) -> list:
    """按签名（主页 token）精确取这个人的公开内容。

    签名是知乎用户的主页唯一标识（如 liangbianyao），用它能精确区分同名作者。
    注意：签名是英文 token，直接拿去搜内容搜不到——要用「名字」搜，再按签名筛。
    """
    items, _used = _search_merged(
        name,
        lambda its: len([x for x in its if _item_is_author(x, name, signature)]) >= 6)
    pool = [it for it in items if _item_is_author(it, name, signature)]
    lib, seen = [], set()
    for it in pool:
        title = clean(it.get("Title", ""))
        if not title:
            continue
        dedup = it.get("ContentID") or title
        if dedup in seen:                      # 同一条内容可能被搜回多次
            continue
        seen.add(dedup)
        body = clean(it.get("ContentText") or it.get("Summary") or it.get("Excerpt") or "")
        lib.append({
            "title": title[:80],
            "summary": body[:BODY_LIMIT],
            "url": (it.get("Url") or "").strip(),      # 原文链接：依据可追溯到出处
            "type": it.get("ContentType") or "",
        })
    # 内容搜索只收录「有排名的」内容 —— 显示名是英文/拼音的作者（如 wenbo）
    # 基本搜不到自己写的回答，「文博」还会被匹配成一堆同音的无关账号。
    # 「想法」免鉴权可读，是唯一能拿到 TA 原话的兜底，所以素材薄时补进来。
    #
    # 阈值原来是 3 —— 太低：搜索只命中三五条时正好停手，而那正是最需要补料的答主。
    # 实测线上抓 chenqin 只拿到 3 条（1 回答 + 2 文章）就不补了，而 TA 的公开内容
    # 绝大多数是「想法」→ 档案里没有一条 TA 的原话，分身的味道就没了。
    # 提到 8：素材不够丰富的都补一轮想法（想法是免鉴权接口，代价只是一次网络请求）。
    if len(lib) < 8:
        have = {x["summary"][:60] for x in lib}
        for x in fetch_pins(signature):
            key = x["summary"][:60]
            if key not in have:
                have.add(key)
                lib.append(x)
    return lib


# 模型偶尔不干活，直接回一句自我介绍（实测：梁边妖那次返回「我是知乎直答…」，
# 结果被当成档案存了下来、还盖住了旧的好档案）。这类输出必须拦在存盘之前。
_BAD_PERSONA_MARKERS = (
    "知乎直答", "AI搜索产品", "AI 搜索产品", "我是知乎开发的",
    "我是一个人工智能", "我是一个 AI", "作为AI", "作为 AI",
)


def looks_broken(persona: str) -> bool:
    """档案体检：太短、或者像自我介绍，就算不合格。

    ⚠️ 别加「必须有小节标题」这类骨架判据 —— 「关注画像」（1b，素材薄时的降级产物）
    本来就没有 `#`（实测「路过的鞋」那份是「**一句话**：素材太少…」），会被误杀。
    只抓两种确定的坏：长度不对、内容是自我介绍。
    """
    t = (persona or "").strip()
    if len(t) < 120:
        return True
    return any(m in t for m in _BAD_PERSONA_MARKERS)


def forge_with_check(fn, name: str) -> str:
    """生成档案并体检；不合格就重试。三次都不行返回空串（上层降级），
    绝不把垃圾当档案存下来 —— 存下来比失败更糟，用户会以为产品坏了。

    ⚠️ 两种坏要分开对待：
    - 「我是知乎直答」这类自我介绍 = 素材内容触发的**确定性**安全兜底，重试无效，
      直接放弃（别白花额度）；
    - 太短 = 可能偶发，才值得重试。
    """
    for i in range(3):
        out = fn()
        if not looks_broken(out):
            return out
        if any(m in (out or "") for m in _BAD_PERSONA_MARKERS):
            return ""     # 确定性兜底，重试没用
        print(f"[warn] {name} 档案输出异常（第 {i+1} 次）：{(out or '')[:60]!r}", flush=True)
    return ""


def forge_soul(name: str, library: list) -> str:
    """生成灵魂档案：用 prompts/1-灵魂档案.md 生成 TA 的人格档案。"""
    p = (ROOT / "prompts" / "1-灵魂档案.md").read_text(encoding="utf-8")
    p = p.split("---", 2)[2].strip() if p.startswith("---") else p
    material = "\n".join(f"- {x['title']}（{x['summary']}）" for x in library)
    return cli_answer(f"{p}\n\n【答主的公开内容】{name}：\n{material}")


def forge_soul_chunked(name: str, library: list, chunk_size: int = 3) -> str:
    """整批生成翻车时的自救：分块找出触发直答安全兜底的「毒素材」，用健康子集重生成。

    实测（梁边妖 20 条）：单放某些内容（性别自嘲 / 具体人物恩怨 / 涉政）会让直答
    **确定性地**返回「我是知乎直答」的自我介绍，且重试无效（内容没变）。只能剔除。
    分块时每块只调 1 次（不重试，省额度），翻车的整块丢弃；健康子集 ≥3 条再整批生成。
    """
    healthy: list = []
    for i in range(0, len(library), chunk_size):
        chunk = library[i:i + chunk_size]
        out = forge_soul(name, chunk)
        if not looks_broken(out):
            healthy.extend(chunk)
        else:
            titles = "、".join((x.get("title") or "")[:14] for x in chunk)
            print(f"[warn] 剔除翻车素材块（第 {i}-{i + len(chunk) - 1} 条）：{titles}", flush=True)
    if len(healthy) < 3:
        return ""
    return forge_with_check(lambda: forge_soul(name, healthy), name)


# 素材薄到这个程度就画不出「人格」了 —— 改画「关注画像」（prompts/1b-关注画像.md），
# 只说他关注什么，不硬凑他是谁。实测：1 条 119 字的素材硬写人格，只会产出套话。
PROFILE_MAX_CHARS = 700


def _lib_chars(lib: list) -> int:
    return sum(len(x.get("title") or "") + len(x.get("summary") or "") for x in lib)


def is_thin(lib: list) -> bool:
    """素材够不够画人格。不够 → 走「关注画像 + 匹配卡」那条路（不做分身对谈）。"""
    return _lib_chars(lib) <= PROFILE_MAX_CHARS


def is_too_thin(lib: list) -> bool:
    """薄到读不出「关注什么」（只剩几条个人化的只言片语）。

    这种素材画出来的卡片会是「无重合 / 得不出方向」—— 诚实但像坏了。
    前端据此请用户补一句「我了解的 TA」。
    """
    return len(lib) <= 2 and _lib_chars(lib) <= 250


def forge_profile(name: str, library: list, extra: str = "") -> str:
    """画「关注地图」：他关注什么、到哪一层，并写明看不出什么。

    extra = 附加素材块（收藏夹名字 / 本人自述），**标签由调用方带进来** ——
    因为这条既给「TA」画，也给「我自己」画，标签不能写死成 TA。
    """
    p = (ROOT / "prompts" / "1b-关注画像.md").read_text(encoding="utf-8")
    p = p.split("---", 2)[2].strip() if p.startswith("---") else p
    material = "\n".join(f"- {x['title']}（{x['summary']}）" for x in library)
    more = f"\n\n{extra}" if extra else ""
    return cli_answer(
        f"{p}\n\n【素材】要画像的人：{name}\n{material}{more}\n\n"
        f"补充事实：能读到的素材只有 {len(library)} 条、约 {_lib_chars(library)} 字，"
        f"素材少这件事在画像里如实说明。")


_FAVLISTS_API = "https://www.zhihu.com/api/v4/members/{token}/favlists?limit=10"


def fetch_favlist_titles(token: str) -> str:
    """TA 的公开收藏夹名字（免鉴权可读；夹子里的内容读不到）。

    素材太薄时它是有用的补充信号 —— 「第一卷：有价值及有意思的资料」这种夹子名
    本身就说明他在意什么。
    """
    if not token:
        return ""
    try:
        d = json.loads(_zhihu_get(_FAVLISTS_API.format(
            token=urllib.parse.quote(token))).decode("utf-8"))
    except Exception:
        return ""
    titles = [str(f.get("title") or "").strip() for f in (d.get("data") or [])]
    return "、".join([t for t in titles if t][:10])


# ============================================================
# 接口
# ============================================================

class LoadReq(BaseModel):
    name: str            # 作者名（用户点选确认后）
    signature: str = ""  # 作者签名（主页唯一 token，同名靠它区分）
    user_id: str = ""    # 埋点归属（匿名 uid，只说「还是这个人」，不含任何身份信息）


class ChatReq(BaseModel):
    session_id: str
    message: str
    topic: str = ""                  # 第四幕：这场聊天是由哪个话题起的头（可为空）
    user_id: str = ""                # 记「聊过谁 / 最近聊什么」用


@app.post("/api/candidates")
def api_candidates(req: LoadReq):
    """搜昵称 → 候选作者列表（头像 + 名字 + 签名）。让用户点选，避免找错人。"""
    if not req.name.strip():
        return {"ok": False, "error": "输入一个知乎昵称"}
    cands = search_candidates(req.name)
    _event("search", uid=(req.user_id or "")[:24], q=req.name.strip(), n=len(cands))
    if not cands:
        return {"ok": False, "error": f"没搜到「{req.name}」相关的内容，换更准确的昵称试试。"}
    return {"ok": True, "candidates": cands}


@app.post("/api/load")
def api_load(req: LoadReq):
    """用户点选确认后：按签名取料 + 生成灵魂档案 → 建立分身会话。

    灵魂档案缓存：内存（SOUL_CACHE）+ 磁盘（data/souls/）。
    同一个答主，全站只建一次——生成完落盘，重启也不丢，之后所有人直接读。
    """
    name = req.name.strip()
    signature = req.signature.strip()
    if not name and not signature:
        return {"ok": False, "error": "参数缺失"}
    # 容错：直接丢主页链接/主页后缀进来也认（前端正常会带 signature，
    # 但「只粘了链接就提交」是最容易发生的输入方式，别让它白白失败）。
    if not signature:
        _tk = extract_zhihu_token(name)
        if _tk:
            _m = fetch_member(_tk) or {}
            signature = _m.get("url_token") or _tk
            name = _m.get("name") or name

    key = signature or name
    fname = hashlib.md5(key.encode("utf-8")).hexdigest()[:16] + ".json"

    cached = SOUL_CACHE.get(key) or _load_json(SOULS_DIR / fname, None)
    # 取料方式升级过 → 旧档案的素材是残缺的，判为过期重建（靠 material_version 识别）
    if cached and cached.get("material_version") != MATERIAL_VERSION:
        cached = None
    # 档案本身坏了（模型偶发跑偏）→ 同样判为过期，重新生成
    if cached and looks_broken(cached.get("persona")):
        print(f"[warn] 缓存里的档案不合格，重建：{key}", flush=True)
        cached = None
    from_cache = bool(cached)
    if cached:
        author, library, persona = cached["name"], cached["library"], cached["persona"]
        # 老缓存没有 mode 字段：按素材量补算
        mode = cached.get("mode") or ("profile" if is_thin(library) else "soul")
    else:
        library = take_by_signature(name, signature)
        if not library:
            # 取料为空是两种完全不同的情况，别混为一谈：
            # ① 搜索接口偶发限流（实测确有）→ 等 2 秒重来一次，多半就好
            # ② 真的没料 → 下面按事实把原因说清楚
            time.sleep(2)
            library = take_by_signature(name, signature)
        if not library:
            # 用主页接口看看这个人到底有多少创作 —— 好把「为什么没有料」说清楚，
            # 而不是笼统说一句「没找到」（用户会以为是名字打错了）。
            m = fetch_member(signature) if signature else {}
            if m:
                n_ans = m.get("answer_count") or 0
                n_art = m.get("articles_count") or 0
                return {"ok": False, "error": (
                    f"找到了「{m.get('name') or name}」——TA 有 {n_ans} 篇回答、"
                    f"{n_art} 篇文章，但知乎的公开搜索接口读不到 TA 的正文"
                    f"（只有被搜索收录的内容才拿得到）。没有素材就没法画像，"
                    f"换一个更活跃、内容能被搜到的答主试试？"),
                    "debug": dict(_ZHIHU_ERR)}
            return {"ok": False, "error": (
                f"这次没读到「{name}」的公开内容 —— 可能是搜索接口限流"
                f"（等几秒再点一次生成），也可能是 TA 的内容没被知乎搜索收录。"),
                "debug": dict(_ZHIHU_ERR)}
        author = name
        # 素材薄 → 不硬凑人格，改画「关注地图」（下游走匹配卡，不做分身对谈）
        mode = "profile" if is_thin(library) else "soul"
        if mode == "profile":
            _fav = fetch_favlist_titles(signature)
            _extra = (f"【TA 的公开收藏夹名字（只看得到名字，看不到内容）】\n{_fav}"
                      if _fav else "")
            persona = forge_with_check(lambda: forge_profile(author, library, _extra), author)
        else:
            # 三层兜底：整批 → 分块剔毒素材 → 退关注画像（更简单，更少触发安全兜底）
            persona = forge_with_check(lambda: forge_soul(author, library), author)
            if not persona:
                persona = forge_soul_chunked(author, library)
            if not persona:
                _fav = fetch_favlist_titles(signature)
                _extra = (f"【TA 的公开收藏夹名字（只看得到名字，看不到内容）】\n{_fav}"
                          if _fav else "")
                persona = forge_with_check(lambda: forge_profile(author, library, _extra), author)
                if persona:
                    mode = "profile"   # 降级了，下游改走匹配卡（不做分身对谈）
        if not persona:
            return {"ok": False, "error": (
                f"这次没整理出「{author}」的档案（生成时出了点岔子）。再点一次通常就好了。"),
                "debug": dict(_ZHIHU_ERR)}
        cached = {"name": author, "library": library, "persona": persona, "mode": mode,
                  "material_version": MATERIAL_VERSION,
                  "cached_at": int(time.time())}
        SOUL_CACHE[key] = cached
        _save_json(SOULS_DIR / fname, cached)          # 落盘：重启也不丢

    _event("soul", uid=(req.user_id or "")[:24], name=author, mode=mode,
           cached=from_cache, n=len(library))
    sid = uuid.uuid4().hex
    SESSIONS[sid] = SoulAgent(name=author, persona=persona, library=library, verbose=False)
    return {
        "ok": True,
        "session_id": sid,
        "name": author,
        "persona": persona,
        "library": library,
        "count": len(library),
        "cached": from_cache,
        "mode": mode,          # soul=人格档案；profile=关注地图（素材薄，下游走匹配卡）
        "chars": _lib_chars(library),
        "too_thin": is_too_thin(library),   # 薄到读不出关注点 → 前端请用户补一句
    }


@app.post("/api/chat")
def api_chat(req: ChatReq):
    """和分身对话（真 Agent：检索 → 回应 → 记忆）"""
    agent = SESSIONS.get(req.session_id)
    if not agent:
        return {"ok": False, "error": "会话已失效，请重新开始"}
    if not req.message.strip():
        return {"ok": False, "error": "说点什么吧"}

    reply = agent.chat(req.message, topic=req.topic)
    _record_history(req.user_id, agent.name, topic=req.topic)   # 让「我聊过的人」当天就有内容
    # ⚠️ 模型输出是两段（【依据】…/【回应】…）—— 必须拆开再给前端，
    # 否则界面上会原样显示「【依据】[12] 【回应】…」，很难看。
    from agent import _split_speech
    ev, sp = _split_speech(reply)
    hits = [{"id": i, "title": d["title"], "summary": d["summary"], "sim": s}
            for i, d, s in agent.last_hits]
    return {"ok": True, "reply": sp or reply, "evidence": ev,
            "hits": hits, "round": len(agent.history)}


# ==================== 我聊过谁（个人抽屉的入口）====================

def _event(kind: str, **fields) -> None:
    """记一条使用事件，append 到当天的 jsonl。埋点回答的是「多少人走到哪一步」。

    ⚠️ 隐私原则：只记「发生了什么」，**不记用户说了什么、写了什么**。
    唯一例外是**搜索词** —— 它是公开的答主名，而且「谁搜不到」对优化找人
    路径是最关键的信息。埋点失败绝不能影响主流程，所以整个包在 try 里。
    """
    try:
        EVENTS_DIR.mkdir(parents=True, exist_ok=True)
        rec = {"ts": int(time.time()), "kind": kind, **fields}
        # 按**业务时区**（UTC+8）分天：容器跑在 UTC，直接 strftime 会把晚上 8 点后的
        # 事件算进「明天」，跨天漏斗就全错位了（信度问题）。
        # ⚠️ 必须用 gmtime（按 UTC 解释时间戳），不能用 localtime ——
        # localtime 会套上「运行环境自己的时区」：容器是 UTC 时是对的，
        # 但本地开发机本身就是 UTC+8，会再 +8h，跨天就错位了（真踩过）。
        day = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 8 * 3600))
        with (EVENTS_DIR / f"{day}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _record_history(uid: str, name: str, signature: str = "", topic: str = "",
                    comment: bool = False) -> None:
    """记一条「我和谁聊过」。没有 uid 就不记 —— 没有身份，这条记录没处归属。

    这里是**用户自己的使用记录**，不是 TA 的资料：只存名字/签名/时间/最后聊的话题。
    """
    if not uid or not name:
        return
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = HISTORY_DIR / f"{_safe_key(uid)}.json"
    rec = _load_json(path, None) or {"items": []}
    items = rec.get("items") or []
    now = int(time.time())
    hit = next((x for x in items if x.get("name") == name), None)
    if hit:
        hit["ts"] = now
        if signature:
            hit["signature"] = signature
        if topic:
            hit["last_topic"] = topic
        hit["visits"] = int(hit.get("visits") or 1) + 1
        if comment:
            hit["comments"] = int(hit.get("comments") or 0) + 1
    else:
        items.insert(0, {"name": name, "signature": signature, "ts": now,
                         "last_topic": topic, "visits": 1,
                         "comments": 1 if comment else 0})
    items = sorted(items, key=lambda x: x.get("ts") or 0, reverse=True)[:30]   # 留最近 30 个
    path.write_text(json.dumps({"user_id": uid, "items": items}, ensure_ascii=False, indent=1),
                    encoding="utf-8")


@app.get("/api/history")
def api_history(user_id: str):
    """我聊过谁 —— 个人抽屉里那个入口读的就是它。"""
    if not user_id:
        return {"ok": True, "items": []}
    rec = _load_json(HISTORY_DIR / f"{_safe_key(user_id)}.json", None)
    return {"ok": True, "items": (rec or {}).get("items") or []}


@app.get("/api/stats")
def api_stats():
    """使用情况统计（只返回聚合数字 + 公开答主名，不含任何用户内容）。

    用途：产品上线后「根据使用记录优化」的依据 —— 看有多少人真的用过、
    哪些答主被聊得最多（该预热谁）、有多少人走到「生成评论」这一步。
    """
    def _count(d) -> int:
        try:
            return sum(1 for _ in d.glob("*.json"))
        except Exception:
            return 0

    hot: dict = {}        # 答主 -> 被聊次数（跨所有用户聚合）
    talked_users = 0      # 有过聊天记录的用户数
    with_comment = 0      # 走到「生成评论」这一步的用户数
    try:
        for f in HISTORY_DIR.glob("*.json"):
            rec = _load_json(f, None) or {}
            items = rec.get("items") or []
            if not items:
                continue
            talked_users += 1
            if any((x.get("comments") or 0) > 0 for x in items):
                with_comment += 1
            for it in items:
                k = (it.get("name") or "").strip()
                if k:
                    hot[k] = hot.get(k, 0) + 1
    except Exception:
        pass

    # TA 档案按模式分（soul=画了人格；profile=素材薄，只画了关注地图）
    modes: dict = {}
    try:
        for f in SOULS_DIR.glob("*.json"):
            m = (_load_json(f, None) or {}).get("mode") or "soul"
            modes[m] = modes.get(m, 0) + 1
    except Exception:
        pass

    # 漏斗：从事件流算 —— 访问 → 搜人 → 读 TA → 建分身 → 拿评论
    # 漏斗：copy_comment 放在 comment 之后 —— 「生成」到「真的复制走」是最后也最关键的落差
    funnel = {"visit": 0, "search": 0, "soul": 0, "me": 0,
              "comment": 0, "copy_comment": 0, "sharecard": 0}
    failed: dict = {}     # 搜不到的词 -> 次数（最值钱的信号）
    errors: list = []     # 线上未捕获异常
    try:
        for f in sorted(EVENTS_DIR.glob("*.jsonl")):
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                k = e.get("kind")
                if k in funnel:
                    funnel[k] += 1
                if k == "error":
                    errors.append(e)
                if k == "search" and not e.get("n"):
                    q = (e.get("q") or "").strip()
                    if q:
                        failed[q] = failed.get(q, 0) + 1
    except Exception:
        pass

    return {
        "ok": True,
        "users": _count(USERS_DIR),          # 生成过分身的人数
        "souls": _count(SOULS_DIR),          # 被读过的答主数
        "soul_modes": modes,                 # 人格 / 关注地图 各多少
        "talked_users": talked_users,        # 真的聊过的人
        "with_comment": with_comment,        # 聊完还拿了评论的人
        "hot": sorted(hot.items(), key=lambda x: -x[1])[:12],   # 热门答主
        # 北极星（由产品负责人拍板）：用户「复制走」的评论数 —— 它才是「打算真的发出去」
        "north_star": {
            "metric": "copy_comment",
            "value": funnel.get("copy_comment", 0),
            "why": "生成只说明读了，复制才说明要用 —— 产品承诺是「帮你开口」，所以成功定义在复制",
        },
        "funnel": funnel,                    # 漏斗各步绝对量（看流失在哪一步）
        "failed_searches": sorted(failed.items(), key=lambda x: -x[1])[:10],  # 搜不到的人
        "recent_errors": errors[-10:],       # 最近的线上报错（含路径和异常类型）
    }


# ==================== 第四幕开局：TA 的分身先开口 + 双模块话题 ====================

class OpenReq(BaseModel):
    session_id: str
    user_id: str = ""                # 有存档时才知道「你们的交集」，否则模块2 留空
    signature: str = ""              # 作者签名：记「聊过谁」时要用（同名靠它区分）


def _parse_opening(raw: str) -> dict:
    """解析 prompts/4c 的输出（【开场】/【模块1】/【模块2】）。

    容错优先：模型偶尔会漏段、多写一句解释。能救就救；救不回来就返回空列表 ——
    前端有「我自己写」这条永远存在的出口，不会把用户卡死。
    """
    text = (raw or "").replace(chr(13), "")
    out = {"opening": "", "m1": [], "m2": []}

    m = re.search(r"【开场】\s*(.+?)(?=\s*【模块|$)", text, re.S)
    if m:
        out["opening"] = re.sub(r"\s+", " ", m.group(1)).strip().strip("`")

    for key, tag in (("m1", "【模块1】"), ("m2", "【模块2】")):
        seg = re.search(re.escape(tag) + r"\s*(.*?)(?=\s*【模块|\s*$)", text, re.S)
        if not seg:
            continue
        for line in seg.group(1).splitlines():
            line = line.strip().strip("`").strip()
            if "|" not in line:
                continue
            parts = [x.strip() for x in line.split("|")]
            if len(parts) < 2:
                continue
            label = parts[1]
            # 过滤表头与空占位
            if not label or label in ("（无）", "无", "话题文字", "话题"):
                continue
            refs = [int(x) for x in re.findall(r"\d+", parts[2])] if len(parts) >= 3 else []
            out[key].append({
                "text": label,
                "refs": refs[:4],
                "recommend": len(parts) >= 4 and "推荐" in parts[3],
            })
    return out


@app.post("/api/opening")
def api_opening(req: OpenReq):
    """第四幕开局：TA 的分身先开口（说「我最近在琢磨什么」）+ 两组可选话题。

    - 模块1「他最在行的」：只能来自 TA 的资料库（背后挂 2～4 篇原文）
    - 模块2「你们可能都感兴趣的」：**必须用户先有自己的档案** —— 没有就返回 locked，
      前端显示锁态卡片让用户主动按（不静默生成，见 v5 决策）
    """
    ta = SESSIONS.get(req.session_id)
    if not ta:
        raise HTTPException(status_code=404, detail="会话已失效，请重新开始")

    my_lib, my_state = None, "用户还没生成自己的档案 → 模块2 留空（locked）"
    if req.user_id:
        rec = _load_json(USERS_DIR / f"{_safe_key(req.user_id)}.json", None)
        if rec and rec.get("library"):
            my_lib = rec["library"]
            my_state = f"用户（{rec.get('name') or '我'}）的档案已生成，可以找交集了"
        elif rec and rec.get("persona"):
            my_state = "用户有档案但没有内容索引 → 模块2 留空（locked）"

    p = (ROOT / "prompts" / "4c-开场与话题.md").read_text(encoding="utf-8")
    p = p.split("---", 2)[2].strip() if p.startswith("---") else p

    from agent import build_opening_prompt
    raw = cli_answer(build_opening_prompt(ta, p, my_lib, my_state))
    got = _parse_opening(raw)

    # 记一笔「我和 TA 聊过」—— 个人抽屉的入口靠它
    _record_history(req.user_id, ta.name, req.signature)

    return {
        "ok": True,
        "ta_name": ta.name,
        "opening": got["opening"] or f"我是{ta.name}的分身。你最近在琢磨什么？",
        "modules": [
            {"key": "his", "label": "他最在行的", "locked": False, "topics": got["m1"]},
            {"key": "both", "label": "你们可能都感兴趣的", "locked": my_lib is None,
             "topics": got["m2"]},
        ],
        # 解析失败时把原文带回去（只给开发看，界面不展示）：
        "parse_failed": not got["m1"] and not got["m2"],
        "raw": raw[:2000] if not got["m1"] and not got["m2"] else "",
    }


class DuelReq(BaseModel):
    session_id: str
    intro: str                       # 你的三句话（兜底，没有存档时用）
    my_name: str = "我"              # 你的昵称（兜底）
    user_id: str = ""                # 有存档时，从这里加载你的分身（含编辑过的档案）
    topic: str = ""
    rounds: int = 2                  # 默认 2 轮 = 4 条（实测够看出「聊不聊得来」，省额度）


class IceReq(BaseModel):
    session_id: str
    intro: str
    dialogue: list


def sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


@app.post("/api/duel")
def api_duel(req: DuelReq):
    """A2A：你的分身 × TA 的分身。SSE 流式——前端能逐条看到它们说话。"""
    ta = SESSIONS.get(req.session_id)
    if not ta:
        raise HTTPException(status_code=404, detail="会话已失效，请重新开始")
    # 「你」必须来自存档：用户用知乎授权建的分身（persona/library 来自他本人的创作）。
    # 不再有「跳过生成、用一句自我介绍代班」那条路 —— 那样两个没内容的角色只会客套。
    rec0 = _load_json(USERS_DIR / f"{_safe_key(req.user_id)}.json", None) if req.user_id else None
    has_archive = bool(rec0 and (rec0.get("persona") or rec0.get("library")))
    # 没有存档就没有「你」——不假装能聊（正常路径下前端不会走到这：
    # 必须先建成分身，幕 3 才会出现开始按钮）
    if not has_archive:
        raise HTTPException(status_code=400, detail="还没建你的分身，先生成一个再开始")

    def gen():
        try:
            me = build_self_agent(req.intro, name=req.my_name)   # 兜底
            if req.user_id:
                rec = _load_json(USERS_DIR / f"{_safe_key(req.user_id)}.json", None)
                if rec:
                    me = build_self_agent(rec.get("intro") or req.intro,
                                          name=rec.get("name") or req.my_name)
                    me.persona = rec.get("persona") or me.persona   # 用存档（含编辑）
                    if rec.get("library"):                          # 有真实资料库就用它
                        me.library = rec["library"]
            yield sse({"type": "self_persona", "persona": me.persona})
            ta.key, me.key = "a", "b"
            dialogue = []
            for r in range(max(1, min(req.rounds, 4))):
                for agent, peer in ((ta, me), (me, ta)):
                    opener = (r == 0 and agent is ta)
                    try:
                        from agent import speak_to
                        out = speak_to(agent, peer.key, peer.name, dialogue, req.topic,
                                       is_opener=opener, as_self=getattr(agent, "is_self", False))
                    except Exception as e:
                        out = {"依据": "", "发言": f"（对话中断：{e}）"}
                    turn = {
                        "key": agent.key, "name": agent.name, "text": out["发言"],
                        "evidence": out["依据"],
                        "hits": [{"id": i, "title": d["title"], "sim": s} for i, d, s in agent.last_hits],
                    }
                    dialogue.append(turn)
                    yield sse({"type": "turn", **turn})
            ta.duel_log = dialogue
            yield sse({"type": "done", "count": len(dialogue)})
        except Exception as e:
            yield sse({"type": "error", "error": str(e)})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ==================== 第四幕产出：针对 TA 某篇内容的评论 ====================

class CommentReq(BaseModel):
    session_id: str
    user_id: str = ""                # 记「聊过谁」用
    topic: str = ""
    dialogue: list = []              # 这场聊天（[{name, text}]）—— 只用来判断「他想问什么」
    n: int = 3


def _parse_comments(raw: str) -> list:
    """解析 prompts/5 的输出：序号|形态|正文|编号"""
    out = []
    for line in (raw or "").replace("\r", "").splitlines():
        line = line.strip().strip("`").strip()
        if "|" not in line:
            continue
        parts = [x.strip() for x in line.split("|")]
        if len(parts) < 3:
            continue
        text = parts[2]
        if not text or text in ("评论正文", "正文"):
            continue
        kind = parts[1].lower()
        ref = None
        if len(parts) >= 4:
            m = re.search(r"\d+", parts[3])
            if m:
                ref = int(m.group(0))
        out.append({"kind": kind if kind in ("a", "c") else "a", "text": text, "ref": ref})
    return out


@app.post("/api/comment")
def api_comment(req: CommentReq):
    """生成「针对 TA 某篇内容、可直接发出去」的评论。

    依据地基：**只用 TA 公开发布的内容**（标题 + 摘要）。
    对话记录只用来判断这个人想问什么 —— 每条评论都挂着它的出处，用户能自己核。
    """
    ta = SESSIONS.get(req.session_id)
    if not ta:
        raise HTTPException(status_code=404, detail="会话已失效，请重新开始")

    # 候选内容：话题 + 最近几句对话 → 检索出最相关的几篇（必须有原文链接）
    q = " ".join([req.topic] + [str(d.get("text", "")) for d in (req.dialogue or [])[-4:]]).strip()
    hits = ta._retrieve(q) if q else []
    cands, seen = [], set()
    for idx, doc, _ in hits:
        if idx in seen or not doc.get("url"):
            continue
        seen.add(idx)
        cands.append((idx, doc))
        if len(cands) >= 6:
            break

    # 检索没命中就退回到时间线最前面的几篇（有链接的）
    if not cands:
        for i, doc in enumerate(ta.library, 1):
            if doc.get("url"):
                cands.append((i, doc))
            if len(cands) >= 6:
                break

    if not cands:
        return {"ok": False, "error": "TA 的内容里没找到能挂原文链接的（评论要能核到出处才有意义）"}

    lib_text = "\n".join(f"[{i}] 《{d['title']}》\n    摘要：{d.get('summary', '')}" for i, d in cands)
    convo = "\n".join(f"{t.get('name', '')}：{t.get('text', '')}" for t in (req.dialogue or [])[-8:])

    p = (ROOT / "prompts" / "5-评论.md").read_text(encoding="utf-8")
    p = p.split("---", 2)[2].strip() if p.startswith("---") else p
    raw = cli_answer(
        f"{p}\n\n【TA 的名字】{ta.name}\n\n【候选内容】\n{lib_text}\n\n"
        f"【这个用户刚聊过的话题】{req.topic or '（没指定）'}\n\n"
        f"【刚刚这场对话（只用来判断他想问什么，绝不能当 TA 说过的话）】\n{convo or '（没聊几句）'}"
    )

    _record_history(req.user_id, ta.name, topic=req.topic, comment=True)

    items = _parse_comments(raw)
    by_idx = {i: d for i, d in cands}
    comments = []
    for it in items[: max(1, min(req.n, 4))]:
        doc = by_idx.get(it["ref"]) or cands[0][1]
        comments.append({
            "kind": it["kind"],
            "text": it["text"],
            "title": doc["title"],
            "url": doc.get("url", ""),
            "summary": (doc.get("summary") or "")[:160],
        })

    _event("comment", name=ta.name, n=len(comments))
    return {"ok": True, "ta_name": ta.name,
            "comments": comments,
            "parse_failed": not comments,
            "raw": raw[:1500] if not comments else ""}


class ShareReq(BaseModel):
    user_id: str


def _parse_sharecard(raw: str) -> dict:
    """解析 prompts/6-分享卡.md 的输出（【金句】/【标签】/【维度】）。"""
    text = (raw or "").replace(chr(13), "")
    out = {"quote": "", "tags": [], "axes": [], "values": []}

    m = re.search(r"【金句】\s*(.+?)(?=\s*【|\Z)", text, re.S)
    if m:
        out["quote"] = re.sub(r"\s+", " ", m.group(1)).strip().strip("`「」“”")

    m = re.search(r"【标签】\s*(.+?)(?=\s*【|\Z)", text, re.S)
    if m:
        out["tags"] = [t.strip() for t in m.group(1).strip().split("|") if t.strip()][:6]

    m = re.search(r"【维度】\s*(.+?)(?=\s*【|\Z)", text, re.S)
    if m:
        for seg in m.group(1).strip().split("|"):
            if "," not in seg:
                continue
            name, _, val = seg.rpartition(",")
            try:
                v = float(val.strip())
            except ValueError:
                continue
            name = name.strip().strip("「」")
            if name:
                out["axes"].append(name)
                out["values"].append(max(0.25, min(1.0, v)))
    return out


@app.post("/api/sharecard")
def api_sharecard(req: ShareReq):
    """把我的档案变成一张能发出去的卡（金句 + 标签 + 雷达图维度）。

    **只在用户点「生成分享卡」时才调**（不点不花钱）。没有档案就用 ME 的兜底。
    """
    # 先挡空/非法 user_id —— 否则 _safe_key 会抛，FastAPI 回 400（前端拿到的是 HTTP 错误
    # 而不是 {"ok": false}，不好处理）。前端在没建档时也会带空 uid 试一次。
    if not (req.user_id or "").strip():
        return {"ok": False, "error": "还没有你的档案 —— 先生成一次你的分身"}
    try:
        uid = _safe_key(req.user_id)
    except Exception:
        return {"ok": False, "error": "还没有你的档案 —— 先生成一次你的分身"}

    rec = _load_json(USERS_DIR / f"{uid}.json", None)
    persona = (rec or {}).get("persona") or ""
    if not persona.strip():
        return {"ok": False, "error": "还没有你的档案 —— 先生成一次你的分身"}

    name = (rec.get("name") or "我").strip() or "我"
    try:
        p = (ROOT / "prompts" / "6-分享卡.md").read_text(encoding="utf-8")
        p = p.split("---", 2)[2].strip() if p.startswith("---") else p
        raw = cli_answer(f"{p}\n\n【TA 的名字】{name}\n\n【TA 的档案】\n{persona}")
    except Exception as e:
        # 别让异常漏成 500（前端只能看到「网络出了点问题」）—— 把原因说清楚
        _ZHIHU_ERR["sharecard"] = f"{type(e).__name__}: {e}"[:200]
        return {"ok": False, "error": (
            f"卡片生成没成功（{type(e).__name__}）。等几秒再点一次试试；"
            f"如果一直这样，多半是模型调用被限流了。"), "debug": dict(_ZHIHU_ERR)}
    got = _parse_sharecard(raw)

    # 解析救不回来时，用档案兜底（至少给一张卡，不把用户卡死）
    if not got["quote"] or len(got["axes"]) < 3:
        return {"ok": False, "error": "卡片生成失败，请再点一次", "raw": raw[:600]}

    # 二维码指向产品首页（带 from=card，将来能看有多少人是扫码来的）
    qr = make_qr_datauri(SITE_URL + "/?from=card")
    _event("sharecard")
    return {"ok": True, "name": name,
            "quote": got["quote"], "tags": got["tags"][:5],
            "axes": got["axes"][:5], "values": got["values"][:5],
            "qr": qr, "site": SITE_URL}


@app.post("/api/icebreak")
def api_icebreak(req: IceReq):
    """破冰卡：看完整场对话后，给用户一张能直接用的卡。"""
    ta = SESSIONS.get(req.session_id)
    if not ta:
        return {"ok": False, "error": "会话已失效，请重新开始"}
    if not req.dialogue:
        return {"ok": False, "error": "还没有对话记录"}

    p = (ROOT / "prompts" / "4-破冰卡.md").read_text(encoding="utf-8")
    p = p.split("---", 2)[2].strip() if p.startswith("---") else p
    convo = "\n".join(f"「{t['name']}」：{t['text']}" for t in req.dialogue)
    card = cli_answer(
        f"{p}\n\n【TA 的名字】{ta.name}\n\n【TA 的灵魂档案】\n{ta.persona}\n\n"
        f"【用户的自我介绍】\n{req.intro}\n\n【两个分身的对话记录】\n{convo}"
    )
    return {"ok": True, "card": card, "ta_name": ta.name}



class MatchReq(BaseModel):
    session_id: str
    user_id: str = ""
    ta_note: str = ""      # 素材太薄时，用户自己对 TA 的了解（一句话/几句）


@app.post("/api/match")
def api_match(req: MatchReq):
    """兴趣匹配卡：TA 的素材太薄时**不做双分身对谈**（没有语言样本，对谈只会客套），
    直接比两边「关注地图」的重合，给一张卡 + 破冰话术。"""
    ta = SESSIONS.get(req.session_id)
    if not ta:
        return {"ok": False, "error": "会话已失效，请重新开始"}
    rec = _load_json(USERS_DIR / f"{_safe_key(req.user_id)}.json", None) if req.user_id else None
    if not rec or not (rec.get("persona") or rec.get("library")):
        return {"ok": False, "error": "还没建你的分身，先生成一个再开始"}

    p = (ROOT / "prompts" / "4b-匹配卡.md").read_text(encoding="utf-8")
    p = p.split("---", 2)[2].strip() if p.startswith("---") else p
    # TA 那侧：素材（可能薄到没用）+ 用户自己补的一句「我了解的 TA」
    ta_side = ta.persona
    note = (req.ta_note or "").strip()
    if note:
        ta_side += (f"\n\n【用户自己对 TA 的了解（用户写的，不是 TA 的公开内容）】\n{note}\n"
                    f"注意：这一部分来自用户转述，不是 TA 公开内容的推论 —— 用它的时候要标明来源，"
                    f"别当成「素材里读出来的」。")
    me_name = (rec.get("name") or "我").strip() or "我"
    card = cli_answer(
        f"{p}\n\n【我的关注地图】\n{rec.get('persona') or ''}\n\n"
        f"【TA 的关注地图】\n{ta_side}\n\n"
        f"补充事实：TA 能读到的公开创作只有 {len(ta.library)} 条"
        f"（约 {_lib_chars(ta.library)} 字），卡片上要如实标出这件事。")
    # ⚠️ 标题由前端渲染（带两个名字）——模型不用再写一遍，避免卡上出现两个标题
    return {"ok": True, "card": card, "ta_name": ta.name, "me_name": me_name,
            "ta_count": len(ta.library)}


class MeReq(BaseModel):
    user_id: str
    name: str = "我"
    intro: str = ""            # 首次/重建时给（三句话）
    persona: str = ""          # 编辑时给（直接存用户改好的档案文字）


@app.get("/api/me")
def api_me(user_id: str):
    """读我的分身。没有则 exists=False。"""
    uid = _safe_key(user_id)
    rec = _load_json(USERS_DIR / f"{uid}.json", None)
    if not rec:
        return {"ok": False, "exists": False}
    return {"ok": True, "exists": True, **rec}


@app.post("/api/me")
def api_me_save(req: MeReq, request: Request):
    """存/更新我的分身。

    - 给了 persona → 用户手动改过的，直接存文字（不烧 LLM，也不需要登录）
    - 没给 persona → **必须先用知乎账号登录并授权**，用用户自己的 token
      读他自己的创作。分身的来源只能是用户本人——没有授权就不生成。
    """
    uid = _safe_key(req.user_id)
    path = USERS_DIR / f"{uid}.json"
    old = _load_json(path, {})

    if req.persona.strip():
        rec = {
            "user_id": uid,
            "name": (req.name.strip() or old.get("name") or "我"),
            "intro": req.intro.strip() or old.get("intro", ""),
            "persona": req.persona.strip(),
            "source": "edited",
            "mode": old.get("mode") or "soul",
            "library": old.get("library", []),
            "count": old.get("count", 0),
            "edited": True,
            "created": old.get("created") or int(time.time()),
            "updated": int(time.time()),
        }
    else:
        # ⚠️ 生成你的分身 = 必须拿出你自己的授权
        sid = request.cookies.get(COOKIE)
        sess = oauth.get_session(sid) if sid else None
        if not sess:
            return {
                "ok": False,
                "need_login": True,
                "error": "生成你的分身，得先让知乎账号登录并授权——这样分身才来自你自己写过的内容，而不是别人的。",
            }

        try:
            my_lib = oauth.fetch_user_contents(sess["access_token"], limit=20)
        except Exception as e:
            return {"ok": False, "error": "读你的知乎创作时出错：" + str(e)[:160]}

        if not my_lib:
            return {
                "ok": False,
                "error": "你的知乎账号里还没读到公开创作 —— 没有素材就画不出画像。"
                         "可以先在知乎写几条，或过一会儿再试。",
            }

        # 素材薄 → 不硬凑人格，改画「关注地图」（后面走匹配卡，不做分身对谈）
        mode = "profile" if is_thin(my_lib) else "soul"
        if mode == "profile":
            _prof = sess.get("profile") or {}
            _fav = fetch_favlist_titles(_prof.get("url_token") or _prof.get("urlToken") or "")
            _blocks = []
            # 素材薄时，本人亲手写的那句介绍是最可信的一条素材 —— 别丢
            if req.intro.strip():
                _blocks.append(f"【本人自述（本人写的，比公开素材可信）】\n{req.intro.strip()}")
            if _fav:
                _blocks.append(f"【本人公开收藏夹名字（只看得到名字，看不到内容）】\n{_fav}")
            persona = forge_profile(req.name.strip() or old.get("name") or "我", my_lib,
                                    "\n\n".join(_blocks))
        else:
            material = "\n".join(f"- {x['title']}（{x['summary']}）" for x in my_lib)
            p = (ROOT / "prompts" / "1-灵魂档案.md").read_text(encoding="utf-8")
            p = p.split("---", 2)[2].strip() if p.startswith("---") else p
            extra = f"\n\n【本人补充】\n{req.intro.strip()}" if req.intro.strip() else ""
            persona = cli_answer(f"{p}\n\n【这个人在知乎的公开创作】\n{material}{extra}")
        rec = {
            "user_id": uid,
            "name": (req.name.strip() or old.get("name") or "我"),
            "intro": req.intro.strip(),
            "persona": persona,
            "source": "oauth",
            "mode": mode,
            "count": len(my_lib),
            "library": my_lib,
            "zhihu": sess.get("profile") or {},
            "edited": False,
            "created": old.get("created") or int(time.time()),
            "updated": int(time.time()),
        }

    _save_json(path, rec)
    _event("me", mode=rec.get("mode"), n=rec.get("count"))
    return {"ok": True, **rec}


# ============================================================
# OAuth（知乎账号登录）
# ============================================================
COOKIE = "salt_sid"


class EventReq(BaseModel):
    kind: str                 # 事件名（如 copy_comment / pick）
    user_id: str = ""
    name: str = ""            # 相关答主（可选）
    n: int = -1


@app.post("/api/event")
def api_event(req: EventReq):
    """前端上报「后端看不到的用户动作」。

    最要紧的是 **copy_comment（用户复制了评论）** —— 它才是北极星的正确测量：
    comment 只说明「生成了」，copy 才说明「打算真的发出去」。产品承诺是「帮你开口」，
    生成没发 = 没成功，所以「生成数」会高估价值（效度问题）。
    """
    _event((req.kind or "unknown")[:24], uid=(req.user_id or "")[:24],
           name=(req.name or "")[:40], n=req.n)
    return {"ok": True}


@app.get("/api/oauth/status")
def api_oauth_status(user_id: str = ""):
    """前端据此决定显示「用知乎登录」还是「凭证未配置」的说明。"""
    # 带 uid：否则只能算 PV（页面加载次数），算不出 UV（多少**人**来过）
    _event("visit", uid=(user_id or "")[:24])
    return {"ok": True, "configured": oauth.is_configured()}


@app.get("/api/oauth/url")
def api_oauth_url():
    if not oauth.is_configured():
        return {"ok": False, "error": "OAuth 凭证未配置——需「创建黑客松项目」后由赛事页面生成 App ID / App Key"}
    url, state = oauth.build_authorize_url()
    return {"ok": True, "url": url, "state": state}


@app.get("/oauth/callback")
def oauth_callback(authorization_code: str = "", code: str = "", state: str = ""):
    """知乎授权回调。参数是 authorization_code（兼容 code）。"""
    auth_code = authorization_code or code
    if not auth_code:
        return RedirectResponse("/?oauth=err&msg=" + urllib.parse.quote("缺少授权码"))
    ok, reason = oauth.check_state(state)
    if not ok:
        return RedirectResponse("/?oauth=err&msg=" + urllib.parse.quote(reason))
    try:
        token, expires_in = oauth.exchange_token(auth_code)
        profile = oauth.fetch_user(token)
    except Exception as e:
        return RedirectResponse("/?oauth=err&msg=" + urllib.parse.quote(str(e)[:120]))
    sid = oauth.new_session(token, expires_in, profile)
    resp = RedirectResponse("/?oauth=ok")
    resp.set_cookie(COOKIE, sid, httponly=True, samesite="lax", max_age=expires_in)
    return resp


@app.get("/api/oauth/me")
def api_oauth_me(request: Request):
    """读当前登录用户（token 只在服务端，浏览器只持 HttpOnly Cookie）。"""
    sid = request.cookies.get(COOKIE)
    s = oauth.get_session(sid) if sid else None
    if not s:
        return {"ok": False, "logged_in": False}
    return {"ok": True, "logged_in": True, "profile": s["profile"],
            "profile_keys": s.get("profile_keys", []),
            "expires_in": int(s["expires_at"] - time.time())}


@app.post("/api/oauth/logout")
def api_oauth_logout(request: Request, response: Response):
    sid = request.cookies.get(COOKIE)
    if sid:
        oauth.drop_session(sid)
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/quota")
def api_quota():
    """查额度（前端展示剩余次数）"""
    try:
        d = json.loads(cli_raw(["quota"]))
        for x in d.get("Data", []):
            if x.get("APIID") == "zhida_openai":
                return {"ok": True, "used": x["TotalUsed"], "total": x["TotalQuota"],
                        "remaining": x["RemainingQuota"]}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": False, "error": "未找到直答额度"}


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/favicon.svg")
def favicon():
    return FileResponse(ROOT / "web" / "favicon.svg", media_type="image/svg+xml")


@app.get("/favicon.ico")
def favicon_ico():
    return FileResponse(ROOT / "web" / "favicon.svg", media_type="image/svg+xml")


# ---- 知乎网页版公开接口：按主页 token 直接读人（免鉴权）----
# 开放平台没有「搜人」接口，内容搜索也只收录有排名的内容。
# 但 /api/v4/members/<token> 不需要任何凭证就能读到公开资料，
# 所以「粘贴主页链接/ token」这条路是通的 —— 先找到人，再按真名搜内容。
_MEMBER_API = ("https://www.zhihu.com/api/v4/members/{token}"
               "?include=name,headline,url_token,avatar_url,"
               "answer_count,articles_count,pins_count,follower_count")
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
# 知乎是国内站：走代理会失败，这里显式绕开。
# 另外挂一个 cookie 罐 —— 信息流类接口（pins）在机房 IP 上不带游客 cookie 会 403。
_COOKIE_JAR = http.cookiejar.CookieJar()
_NO_PROXY = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPCookieProcessor(_COOKIE_JAR))
_COOKIE_AT = 0.0


def _warm_cookies(force: bool = False) -> None:
    """先访问一次首页，把 d_c0 之类的游客 cookie 收下来。

    实测：`/members/<token>`（单对象）不带 cookie 也能过，
    但 `/members/<token>/pins`（信息流）在机房 IP 上会 403 —— 补上游客 cookie 再试。
    """
    global _COOKIE_AT
    if not force and _COOKIE_AT and time.time() - _COOKIE_AT < 3600:
        return
    try:
        req = urllib.request.Request("https://www.zhihu.com/",
                                     headers={"User-Agent": _UA, "Accept": "text/html"})
        with _NO_PROXY.open(req, timeout=20) as r:
            r.read(2048)
        _COOKIE_AT = time.time()
    except Exception as e:
        _ZHIHU_ERR["cookie"] = f"{type(e).__name__}: {e}"[:120]


_PINS_API = "https://www.zhihu.com/api/v4/members/{token}/pins?limit={limit}"
# 外部取数服务：机房 IP 被知乎 WAF 拦时，借它的 IP 去取（免费、无需 key）
_RELAY = "https://r.jina.ai/"
_RELAY_MARK = "Markdown Content:"
_RELAY_UA = "icebreaker/1.0"      # 必须是「非浏览器」UA，见 _zhihu_via_relay 注释


def _pins_page(token: str, limit: int, offset: int = 0) -> list:
    url = _PINS_API.format(token=urllib.parse.quote(token), limit=limit)
    if offset:
        url += f"&offset={offset}"
    return json.loads(_zhihu_get(url, timeout=25).decode("utf-8")).get("data") or []


def fetch_pins(token: str, limit: int = 20) -> list:
    """读这个人的「想法」——公开接口免鉴权。

    这是搜不到正文时唯一能拿到的 TA 原话：知乎的 回答/文章 接口一律要鉴权，
    但「想法」不用。实测一位 39 回答 + 75 文章的作者，想法能读到 20 条、7000+ 字，
    足够画像（比只有 1 条搜到的内容强得多）。

    ⚠️ 机房 IP 实测：同一个 pins 接口，`limit=20` 会被 403，`limit=3` 却是 200 ——
    像是 WAF 按请求特征拦。所以从大到小试，并在 debug 里记下哪个形状通了。
    """
    if not token:
        return []
    hit = _PINS_CACHE.get(token)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        pins, used = hit[1], hit[2]
    else:
        pins, used, last_err = [], 0, None
        for lim in (20, 10, 5, 3):
            try:
                pins = _pins_page(token, lim)
                used = lim
                break
            except Exception as e:
                last_err = f"limit={lim} → {type(e).__name__}: {e}"[:90]
        if used:
            _ZHIHU_ERR["pins_shape"] = f"通的是 limit={used}（{len(pins)} 条）"
            # 小页取不全就再翻一页（同一个能通的 limit）
            if pins and limit > len(pins):
                try:
                    pins += _pins_page(token, used, len(pins))
                except Exception:
                    pass
        else:
            _ZHIHU_ERR["pins"] = last_err or "unknown"
        _PINS_CACHE[token] = (time.time(), pins, used)

    out = []
    for pin in pins:
        # 正文是 segment 数组，只取 text 片段
        text = "".join(seg.get("content") or "" for seg in (pin.get("content") or [])
                       if isinstance(seg, dict) and seg.get("type") == "text")
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()
        # 质量闸门：话题关注按钮、转发标题、打卡留念这类只有十几二十字的碎片
        # 是噪音 —— 拿它们画像只会产出套话。宁可报「信息不足」也不要用。
        if len(text) < 60:
            continue
        ts = pin.get("created") or pin.get("updated") or 0
        when = time.strftime("%Y-%m", time.localtime(ts)) if ts else ""
        pid = pin.get("id") or ""
        head = re.sub(r"\s+", " ", text).strip()
        out.append({
            # 想法没有标题。以前拼成「想法（2026-09）」，两个后果：
            #   ① 用户点开「依据」只看到一个日期，不知道这条是什么内容；
            #   ② 模型也无从判断主题 —— 模块2「找你们共同感兴趣的话题」全靠标题比对。
            # 改拿正文开头当标题。日期不进标题：素材本身按时间倒序，顺序已经说明新旧。
            "title": head[:30] + ("…" if len(head) > 30 else ""),
            "summary": text[:BODY_LIMIT],
            "url": f"https://www.zhihu.com/pin/{pid}" if pid else "",
            "type": "Pin",
        })
    return out


def extract_zhihu_token(raw: str) -> str:
    """从输入里认出知乎主页 token。识别不出返回 ""。

    支持三种写法：
        https://www.zhihu.com/people/wenbo
        www.zhihu.com/people/wenbo/answers
        wenbo                       （裸 token）
    中文名不会被当成 token。
    """
    s = (raw or "").strip()
    m = re.search(r"zhihu\.com/people/([A-Za-z0-9_\-]+)", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_\-]{2,40}", s):
        return s
    return ""


def _zhihu_via_relay(url: str, timeout: int = 20):
    """借外部取数服务（r.jina.ai，免费无需 key）换个 IP 取同一个 URL。

    为什么需要它：机房 IP（Railway）直连知乎网页版接口会被 WAF 拦成 403，
    重试、补 cookie 都没用 —— 只有换 IP 才过。返回清洗后的 JSON 字节，拿不到返回 None。
    """
    try:
        # ⚠️ 这里**不能**用浏览器 UA：r.jina.ai 对浏览器 UA 一律 403（反爬），
        # 非浏览器 UA（或空 UA）才给 200。实测过。
        req = urllib.request.Request(_RELAY + url, headers={
            "User-Agent": _RELAY_UA, "Accept": "text/plain"})
        with _NO_PROXY.open(req, timeout=timeout + 15) as r:
            text = r.read().decode("utf-8", "replace")
    except Exception as e:
        _ZHIHU_ERR["relay"] = f"{type(e).__name__}: {e}"[:120]
        return None
    i = text.find(_RELAY_MARK)
    if i >= 0:
        text = text[i + len(_RELAY_MARK):]
    j = text.find("{")
    if j < 0:
        _ZHIHU_ERR["relay"] = "返回里没有 JSON"
        return None
    try:
        obj = json.JSONDecoder().raw_decode(text[j:])[0]
    except Exception as e:
        _ZHIHU_ERR["relay"] = f"解析失败: {e}"[:120]
        return None
    _ZHIHU_ERR["relay"] = "ok"
    return json.dumps(obj).encode("utf-8")


def _zhihu_get(url: str, tries: int = 2, timeout: int = 20) -> bytes:
    """直连知乎网页版接口（免鉴权），不通就走取数服务。

    ⚠️ 三个实测教训，别改回去：
    ① 机房 IP 会被**偶发/按接口 403**，所以要有退避重试；
    ② 别加 Referer / Origin / X-Requested-With —— 实测加了**必 403**，裸 UA 才通；
    ③ 403 光重试是救不回来的（pins 信息流尤其），必须走 `_zhihu_via_relay` 换 IP。
    """
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": _UA, "Accept": "application/json"})
            with _NO_PROXY.open(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 403:
                _warm_cookies(force=True)      # 403 常见于缺游客 cookie：补一次再试
        except Exception as e:
            last = e
        if i + 1 < tries:
            time.sleep(1.2 * (i + 1))
    relayed = _zhihu_via_relay(url, timeout)
    if relayed is not None:
        return relayed
    raise last if last else RuntimeError("zhihu request failed")


# 按 token 缓存资料/想法：一次「搜索 → 点开 → 生成」会重复问同一个人，
# 缓存能把对知乎的请求量压掉一半以上，直接减少被限流的概率。
_MEMBER_CACHE: dict = {}
_PINS_CACHE: dict = {}
_CACHE_TTL = 900          # 15 分钟
# 失败原因留痕：失败响应里会带 debug 字段，好在线上定位（前端不显示）
_ZHIHU_ERR: dict = {}


def fetch_member(token: str) -> dict:
    """按主页 token 读公开资料。拿不到返回 {}。"""
    if not token:
        return {}
    hit = _MEMBER_CACHE.get(token)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    try:
        d = json.loads(_zhihu_get(_MEMBER_API.format(
            token=urllib.parse.quote(token))).decode("utf-8"))
        out = d if isinstance(d, dict) and d.get("name") else {}
    except Exception as e:
        _ZHIHU_ERR["member"] = f"{type(e).__name__}: {e}"[:160]
        out = {}
    if out:
        _MEMBER_CACHE[token] = (time.time(), out)
    return out


if __name__ == "__main__":
    # ⚠️ 必须放在文件最后：uvicorn.run 会阻塞，
    # 写在它后面的模块级定义永远不会执行（会 NameError → 500）。
    import uvicorn
    print("=" * 60)
    print("  社恐破冰船 · 超想认识你，那先跟你的分身 Agent 聊聊吧")
    print("  打开 http://127.0.0.1:8000")
    print("=" * 60)
    uvicorn.run(app, host="127.0.0.1", port=8000)
