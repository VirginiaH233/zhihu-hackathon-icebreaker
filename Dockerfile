# 社恐破冰船 —— 独立部署镜像
# 云端跑得起来的关键：把 Windows 的 zhihu-cli 换成官方 Linux 版（同一套 API，行为一致）
FROM python:3.11-slim

# 基础工具（下载 CLI 用）
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# ---- 装官方 zhihu-cli（linux-amd64）----
ARG CLI_VERSION=0.6.0-beta.20260908125143
ARG CLI_SHA256=d21691ac3bebeac4fb29f6982da6b4e4dddf659b731cd8f65dea1c0242a7d0ba
RUN mkdir -p /opt/zhihu-cli \
 && curl -fsSL "https://developer-cdn.zhihu.com/zhihu-cli/releases/beta/cli/${CLI_VERSION}/zhihu-cli-${CLI_VERSION}-linux-amd64.tar.gz" -o /tmp/cli.tar.gz \
 && echo "${CLI_SHA256}  /tmp/cli.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/cli.tar.gz -C /opt/zhihu-cli \
 && chmod +x /opt/zhihu-cli/zhihu-cli \
 && rm /tmp/cli.tar.gz \
 && /opt/zhihu-cli/zhihu-cli version

# 让 server.py / agent.py 找到它（代码里读 ZHIHU_CLI 环境变量）
ENV ZHIHU_CLI=/opt/zhihu-cli/zhihu-cli

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# 凭证一律走环境变量（不落镜像、不进代码）：
#   ZHIHU_ACCESS_SECRET      开放平台 Access Secret（必需，读内容/搜索/直答）
#   ZHIHU_OAUTH_APP_ID       知乎账号登录（提报后领取）
#   ZHIHU_OAUTH_APP_KEY
#   ZHIHU_OAUTH_REDIRECT_URI 必须是公网 HTTPS 且与登记值完全一致
# 端口固定 8000 —— 平台（Railway 等）的 target port 也填 8000。
# 故意不用 ${PORT}：平台的 PORT 变量和它自己的 target port 容易不一致，
# 两边不一致就会导致 "Application failed to respond"。
ENV PORT=8000
EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
