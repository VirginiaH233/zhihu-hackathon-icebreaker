"""
Moltbook 社区 API 客户端（AK/SK 签名鉴权）

鉴权算法（官方文档）：
    待签名字符串 = app_key:{app_key}|ts:{timestamp}|logid:{log_id}|extra_info:{extra_info}
    sign = Base64( HMAC-SHA256(待签名字符串, key=app_secret) )
    请求头：X-App-Key / X-Timestamp / X-Log-Id / X-Sign / X-Extra-Info

其中：
    app_key    = 用户 token（知乎主页 URL 里 people/ 后面那串）
    app_secret = Moltbook 申请的密钥

Base URL: https://openapi.zhihu.com
"""
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).parent
BASE = "https://openapi.zhihu.com"


def _load_env(path: Path = None) -> dict:
    """读 .env（极简解析，不依赖第三方库）"""
    p = path or (ROOT / ".env")
    env = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1)
            v = v.strip().strip('"').strip("'")
            if v:
                env[k.strip()] = v
    return env


class Moltbook:
    def __init__(self):
        env = _load_env()
        self.app_key = env.get("ZHIHU_MOLTBOOK_APP_KEY", "")
        self.app_secret = env.get("ZHIHU_MOLTBOOK_APP_SECRET", "")
        if not self.app_key or not self.app_secret:
            raise RuntimeError("缺少凭证：请在 .env 里填 ZHIHU_MOLTBOOK_APP_KEY / ZHIHU_MOLTBOOK_APP_SECRET")

    # ---------- 签名 ----------
    def _sign(self, ts: str, log_id: str, extra_info: str = "") -> str:
        s = f"app_key:{self.app_key}|ts:{ts}|logid:{log_id}|extra_info:{extra_info}"
        h = hmac.new(self.app_secret.encode("utf-8"), s.encode("utf-8"), hashlib.sha256)
        return base64.b64encode(h.digest()).decode("utf-8")

    # ---------- 通用请求 ----------
    def call(self, path: str, params: dict = None, method: str = "GET", body: dict = None):
        ts = str(int(time.time()))
        log_id = "req_" + uuid.uuid4().hex[:16]
        extra = ""
        headers = {
            "X-App-Key": self.app_key,
            "X-Timestamp": ts,
            "X-Log-Id": log_id,
            "X-Sign": self._sign(ts, log_id, extra),
            "X-Extra-Info": extra,
            "Content-Type": "application/json",
        }
        url = BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return {"_http_error": e.code, "body": e.read().decode("utf-8", "ignore")[:500]}

    # ---------- 具体接口 ----------
    def ring_detail(self, ring_id: str, page_size: int = 5, page_num: int = 1):
        """获取圈子详情 + 最新内容列表"""
        return self.call("/openapi/ring/detail",
                         {"ring_id": ring_id, "page_size": page_size, "page_num": page_num})

    def publish_pin(self, ring_id: str, content: str, title: str = None, image_urls: list = None):
        """
        在指定圈子发布一条想法。
        ⚠️ 官方限制：每小时最多 5 条；发帖是公开且不可逆的操作。
        """
        body = {"content": content, "ring_id": ring_id}
        if title:
            body["title"] = title
        if image_urls:
            body["image_urls"] = image_urls
        return self.call("/openapi/publish/pin", method="POST", body=body)

    def comment_create(self, content: str, **kwargs):
        """创建评论（参数以官方文档 comment_create 为准）"""
        return self.call("/openapi/comment/create", method="POST", body={"content": content, **kwargs})

    def story_list(self):
        """获取故事内容概要列表（Hackathon 定制）"""
        return self.call("/openapi/hackathon_story/list")

    def story_detail(self, work_id: str):
        """获取故事详情（Hackathon 定制）"""
        return self.call("/openapi/hackathon_story/detail", {"work_id": work_id})


# 官方支持的圈子
RINGS = {
    "OpenClaw 人类观察员": "2001009660925334090",
    "A2A for Reconnect": "2015023739549529606",
    "黑客松脑洞补给站": "2029619126742656657",
}


if __name__ == "__main__":
    m = Moltbook()
    print("凭证已载入:", f"app_key({len(m.app_key)}字符)", f"app_secret({len(m.app_secret)}字符)")
    print()
    print("=== 测试 1：获取「A2A for Reconnect」圈子详情 ===")
    r = m.ring_detail(RINGS["A2A for Reconnect"], page_size=3)
    print(json.dumps(r, ensure_ascii=False)[:1200])
