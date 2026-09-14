"""知乎 OAuth 接入（黑客松渠道）

流程（Authorization Code Flow）：
    ① build_authorize_url()  生成授权链接（带服务端保存的 state）
    ② 知乎回调 redirect_uri?authorization_code=xxx&state=xxx
    ③ check_state() → exchange_token() → fetch_user()
    ④ 用 access_token 读该用户的创作（需 Access Secret + X-OAuth-Token）

凭证：
    ZHIHU_OAUTH_APP_ID / ZHIHU_OAUTH_APP_KEY / ZHIHU_OAUTH_REDIRECT_URI
    黑客松渠道「创建黑客松项目后，系统会自动生成」——当前赛事页尚无入口，
    因此本模块支持未配置状态（is_configured() = False），前端据此提示。

实测要点（来自官方文档 + 知乎 2077 项目验证）：
    - 回调参数是 authorization_code，但换 token 的表单字段叫 code
    - 响应里 code: 20000 表示成功，不能把所有非零 code 当失败
    - access_token 有效期 1 小时，没有 refresh token
    - 授权回调是否回传 state 以黑客松渠道为准（新版文档称支持透传），
      因此校验时两种都要能处理：有 state 必须匹配，无 state 记为待确认
"""
import json
import secrets
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent

AUTHORIZE_URL = "https://openapi.zhihu.com/authorize"
TOKEN_URL = "https://openapi.zhihu.com/access_token"
USER_URL = "https://openapi.zhihu.com/user"
CONTENTS_URL = "https://developer.zhihu.com/api/v1/user/contents"

STATE_TTL = 600          # state 有效期 10 分钟

# 每条创作喂给模型的正文字数上限。旧值 220 会丢掉大半素材（身份档案质量直接受影响）。
SUMMARY_LIMIT = 1200


def _load_env() -> dict:
    """读环境变量，.env 文件作为本地开发的兜底。

    ⚠️ 顺序很重要：**环境变量优先**。云端（Railway/Render 等）没有 .env 文件，
    凭证全部通过平台的环境变量注入；本地开发才用 .env。
    """
    import os

    out = {}

    # 1) 本地 .env（存在才读）
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")

    # 2) 环境变量覆盖（云端走这条）
    for k in ("ZHIHU_OAUTH_APP_ID", "ZHIHU_OAUTH_APP_KEY",
              "ZHIHU_OAUTH_REDIRECT_URI", "ZHIHU_ACCESS_SECRET",
              "ZHIHU_MOLTBOOK_APP_KEY", "ZHIHU_MOLTBOOK_APP_SECRET",
              "ZHIHU_CLI"):
        v = os.environ.get(k)
        if v:
            out[k] = v.strip()

    return out


ENV = _load_env()
APP_ID = ENV.get("ZHIHU_OAUTH_APP_ID", "")
APP_KEY = ENV.get("ZHIHU_OAUTH_APP_KEY", "")
REDIRECT_URI = ENV.get("ZHIHU_OAUTH_REDIRECT_URI", "")
ACCESS_SECRET = ENV.get("ZHIHU_ACCESS_SECRET", "")


def is_configured() -> bool:
    return bool(APP_ID and APP_KEY and REDIRECT_URI)


# ---- 会话：demo 用进程内 Map；多实例部署需换共享存储 ----
# ⚠️ state 以前存在进程内存字典里（STATES = {}）—— 这在云端是**结构性问题**：
# ① 每次部署 Railway 会重启进程，重启后 state 全丢；
# ② 滚动部署时新旧实例并存，state 生成在 A、回调打到 B → 校验必失败；
# ③ 免费层空闲休眠同理。
# 而用户在手机上从「点登录」到「知乎授权完跳回来」要花几十秒，撞上的概率不低。
# 结果就是「手机上登录失败」—— 且错误提示只会说「state 缺失或已使用」，查不出真因。
# 改成**无状态签名**：state = "{时间戳}.{HMAC}", 校验只验签名 + 时效，不依赖任何服务端存储。
_STATE_SECRET = ""     # 首次用到时从 APP_KEY 派生

def _state_secret() -> str:
    global _STATE_SECRET
    if not _STATE_SECRET:
        import hashlib as _h
        _STATE_SECRET = _h.sha256(("state|" + (APP_KEY or APP_ID or "fallback")).encode()).hexdigest()
    return _STATE_SECRET

def _sign_state(ts: int) -> str:
    import hmac as _m, hashlib as _h
    return _m.new(_state_secret().encode(), str(ts).encode(), _h.sha256).hexdigest()[:32]
SESSIONS: dict = {}    # session_id -> {access_token, expires_at, profile}


def build_authorize_url() -> tuple:
    """生成授权 URL + state（state 存服务端，短时效）"""
    ts = int(time.time())
    state = f"{ts}.{_sign_state(ts)}"      # 无状态：回调时不依赖服务端还记得它
    q = urllib.parse.urlencode({
        "redirect_uri": REDIRECT_URI,
        "app_id": APP_ID,
        "response_type": "code",
        "state": state,
    })
    return f"{AUTHORIZE_URL}?{q}", state


def check_state(state: str) -> tuple:
    """校验 state（无状态：验签名 + 时效）。返回 (ok, reason)"""
    import hmac as _m
    if not state or "." not in state:
        return False, "没带 state（可能是从知乎 App 内打开的，换浏览器再试）"
    ts_s, _, sig = state.partition(".")
    try:
        ts = int(ts_s)
    except ValueError:
        return False, "state 格式不对"
    if not _m.compare_digest(_sign_state(ts), sig):
        return False, "state 校验失败（换浏览器再试一次）"
    age = time.time() - ts
    if age > STATE_TTL:
        return False, f"state 已过期（在授权页停留了 {int(age/60)} 分钟，回来重试一次即可）"
    return True, ""


def exchange_token(code: str) -> tuple:
    """authorization_code → access_token。返回 (token, expires_in)"""
    body = urllib.parse.urlencode({
        "app_id": APP_ID,
        "app_key": APP_KEY,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
        "code": code,                      # ⚠️ 回调给的是 authorization_code，这里字段名是 code
    }).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode("utf-8"))
    token = d.get("access_token")          # ⚠️ 优先看 token 是否存在（code:20000 也是成功）
    if not token:
        raise RuntimeError("换取 token 失败：" + json.dumps(d, ensure_ascii=False)[:200])
    return token, int(d.get("expires_in") or 3600)


def fetch_user(token: str) -> dict:
    """读授权用户基础信息（昵称/头像/简介）"""
    req = urllib.request.Request(USER_URL, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode("utf-8"))
    if not (d.get("fullname") or d.get("uid")):
        raise RuntimeError("读用户信息失败：" + json.dumps(d, ensure_ascii=False)[:200])
    return d


def fetch_user_contents(token: str, limit: int = 20) -> list:
    """读授权用户的创作列表（需 Access Secret + X-OAuth-Token）"""
    if not ACCESS_SECRET:
        raise RuntimeError("缺 ZHIHU_ACCESS_SECRET，无法读授权用户的创作")
    q = urllib.parse.urlencode({"ContentType": "all", "Limit": min(limit, 50)})
    req = urllib.request.Request(
        f"{CONTENTS_URL}?{q}",
        headers={
            "Authorization": f"Bearer {ACCESS_SECRET}",
            "X-OAuth-Token": token,
            "X-Request-Timestamp": str(int(time.time())),
            "Content-Type": "application/json",
        })
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode("utf-8"))
    items = (d.get("Data") or {}).get("Items") or []
    out = []
    for it in items:
        title = (it.get("Title") or "").strip()
        summ = (it.get("Summary") or "").strip()
        if title or summ:
            out.append({"title": (title or summ)[:80], "summary": summ[:SUMMARY_LIMIT]})
    return out


def pick_avatar(p: dict) -> str:
    """从 /user 响应里取头像 URL。

    ⚠️ `/user` 的头像字段名官方文档没写死（用户数据 API 是 `AvatarUrl`，
    这里可能叫 avatar_path / avatar / avatar_url…）。所以按候选名逐个试，
    再兜底扫一遍「值像图片链接」的字段 —— 避免因为字段名猜错就不出头像。
    """
    for k in ("avatar_path", "avatar", "avatar_url", "avatarUrl", "AvatarUrl",
              "avatarPath", "picture", "avatar_large", "avatarUrlTemplate"):
        v = p.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
    for k, v in p.items():
        if isinstance(v, str) and v.startswith("http") and "avatar" in k.lower():
            return v
    return ""


def new_session(token: str, expires_in: int, profile: dict) -> str:
    sid = secrets.token_urlsafe(24)
    SESSIONS[sid] = {
        "access_token": token,
        "expires_at": time.time() + expires_in,
        "uid": str(profile.get("uid") or ""),
        "profile": {**{k: profile.get(k) for k in
                       ("fullname", "headline", "description", "hash_id", "url")},
                    "avatar": pick_avatar(profile)},
        # 只记字段名，方便排查（不含值，不是敏感信息）
        "profile_keys": sorted(profile.keys()),
    }
    return sid


def get_session(sid: str):
    s = SESSIONS.get(sid)
    if not s:
        return None
    if s["expires_at"] < time.time():
        SESSIONS.pop(sid, None)     # ⚠️ 过期即清，不静默降级成 Access Secret 身份
        return None
    return s


def drop_session(sid: str):
    SESSIONS.pop(sid, None)
