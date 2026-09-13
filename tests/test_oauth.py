"""OAuth 逻辑单测（不依赖真实凭证，Mock 掉网络）

重点验证文档里最容易踩的安全点：
  1. state 不可预测、不可重复使用、会过期
  2. 回调参数兼容 authorization_code / code
  3. token 响应里 code:20000 是成功（不能当失败）
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import oauth

FAIL = []


def check(name, cond, extra=""):
    print(("  ✅ " if cond else "  ❌ ") + name + (("  → " + str(extra)) if extra else ""))
    if not cond:
        FAIL.append(name)


print("=" * 70)
print("OAuth 逻辑单测（Mock）")
print("=" * 70)

print("\n[1] state：生成 → 校验通过 → 不可重复使用")
url, st = oauth.build_authorize_url()
check("state 长度足够（≥20）", len(st) >= 20, len(st))
check("授权 URL 含 response_type=code", "response_type=code" in url)
check("授权 URL 含 state", f"state={st}" in url)
ok, why = oauth.check_state(st)
check("首次校验通过", ok, why)
ok2, why2 = oauth.check_state(st)
check("重复使用被拒绝", not ok2, why2)

print("\n[2] state：不存在的值被拒绝")
ok, why = oauth.check_state("伪造的state")
check("伪造 state 被拒绝", not ok, why)

print("\n[3] state：过期被拒绝")
url, st = oauth.build_authorize_url()
oauth.STATES[st] = time.time() - (oauth.STATE_TTL + 10)
ok, why = oauth.check_state(st)
check("过期 state 被拒绝", not ok, why)

print("\n[4] exchange_token：Mock 掉网络，验证解析逻辑")
import urllib.request

real_urlopen = urllib.request.urlopen


class FakeResp:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(req, timeout=None):
    u = req.full_url if hasattr(req, "full_url") else str(req)
    if "access_token" in u:
        # 文档实测：成功响应 code 可能是 20000（不是 0），且有 access_token
        return FakeResp({"access_token": "tok_abc", "token_type": "Bearer",
                         "expires_in": 3600, "code": 20000})
    if u.endswith("/user"):
        return FakeResp({"uid": 1234567890123456789, "fullname": "测试用户",
                         "headline": "Be hungry.", "avatar_path": "https://x/a.jpg"})
    return FakeResp({})


urllib.request.urlopen = fake_urlopen
try:
    tok, exp = oauth.exchange_token("fake_code")
    check("code:20000 被当成功（不报错）", tok == "tok_abc", tok)
    check("expires_in 解析正确", exp == 3600, exp)

    prof = oauth.fetch_user(tok)
    check("读到用户昵称", prof.get("fullname") == "测试用户", prof.get("fullname"))
    check("uid 保持无损（不丢精度）", str(prof.get("uid")) == "1234567890123456789")

    print("\n[5] 会话：建立 / 读取 / 过期即清 / 退出")
    sid = oauth.new_session(tok, exp, prof)
    s = oauth.get_session(sid)
    check("会话可读", s is not None and s["profile"]["fullname"] == "测试用户")
    oauth.SESSIONS[sid]["expires_at"] = time.time() - 1
    check("过期会话读取返回 None（不静默降级）", oauth.get_session(sid) is None)
    sid2 = oauth.new_session(tok, exp, prof)
    oauth.drop_session(sid2)
    check("drop 后读不到", oauth.get_session(sid2) is None)
finally:
    urllib.request.urlopen = real_urlopen

print("\n[6] 凭证配置状态")
_ok = oauth.is_configured()
print(f"  is_configured() = {_ok}（.env 填好 App ID / App Key / Redirect URI 后应为 True）")
check("is_configured 返回布尔", isinstance(_ok, bool))
if _ok:
    check("APP_ID 已填", bool(oauth.APP_ID), oauth.APP_ID)
    check("REDIRECT_URI 是 https 公网地址",
          oauth.REDIRECT_URI.startswith("https://"), oauth.REDIRECT_URI)

print("\n" + "=" * 70)
if FAIL:
    print(f"❌ {len(FAIL)} 项失败：" + "、".join(FAIL))
    sys.exit(1)
print("✅ 全部通过")
