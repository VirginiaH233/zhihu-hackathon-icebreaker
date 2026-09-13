# 社恐破冰船

**超想认识你，那先跟你的分身 Agent 聊聊吧**

知乎黑客松 2026 ·「灵魂匹配局」参赛作品

在线体验：<https://zhihu-hackathon-icebreaker-production.up.railway.app>

![社恐破冰船](assets/cover-16x9.png)

---

## 这是什么

在知乎，你可能关注一个人很久了。从他写的东西里，你能看出他真正在意什么——但你就是开不了口。

卡住你的不是「不想认识」，是第一步的成本。

**社恐破冰船**把「开口」拆成两步：先让两个 AI 分身替你们聊一轮，你看完之后，再决定要不要真的认识对方。

## 怎么用

1. **看 TA** —— 输入一个知乎昵称，系统读他的公开创作，整理出一份**灵魂档案**：他在意什么、观点倾向、说话的方式。
2. **建你的分身** —— 用你自己的知乎账号授权，从你自己的创作里生成你的分身。
3. **两个分身对话** —— 它们在信息隔离的环境里各自检索、各自回应。
4. **拿到破冰卡** —— 一张可以直接开口的卡：第一句话说什么、为什么这句话对他有效。

## 技术上的一个主张

这个产品唯一的核心技术点是：**两个分身必须真正互相隔离。**

如果两个分身共享上下文，那只是一个模型在演双簧；只有信息隔离，才是两个独立主体的相遇。

实现上，两个分身是独立的 Agent 实例，各自持有自己的检索结果、档案和记忆，看不到对方的 prompt、档案与检索依据。它们只能通过「说话」影响彼此——就像真人。

## 技术栈

| | |
|---|---|
| 后端 | FastAPI（`server.py`）+ Agent 引擎（`agent.py`：检索 / 循环 / 记忆） |
| 前端 | 单文件 HTML（`web/index.html`），无框架、无构建 |
| 数据 | 知乎开放平台 API（搜索、用户公开创作）+ OAuth 2.0 授权 |
| Prompt | `prompts/` 下 4 个独立 prompt |
| 部署 | Docker + Railway |

## 目录

```
server.py        FastAPI 服务 / 知乎 API 代理 / OAuth 端点
agent.py         分身 Agent 引擎（检索 / 循环 / 记忆）
oauth.py         知乎 OAuth 2.0（授权跳转 / state 校验 / token 交换）
moltbook.py      知乎 Moltbook 社区 API 客户端
prompts/         4 个核心 prompt（灵魂档案 / 分身 Agent / 三幕对话 / 破冰卡）
web/index.html   单文件前端
tests/           离线单元测试（不需要凭证）
make_assets.py   生成封面图与项目 ICON
```

## 本地运行

```bash
pip install -r requirements.txt
export ZHIHU_ACCESS_SECRET=<你的知乎开放平台 Access Secret>
uvicorn server:app --host 0.0.0.0 --port 8000
```

用到「生成我的分身」（走知乎 OAuth）时再加这三项：

```bash
export ZHIHU_OAUTH_APP_ID=<app_id>
export ZHIHU_OAUTH_APP_KEY=<app_key>
export ZHIHU_OAUTH_REDIRECT_URI=http://127.0.0.1:8000/oauth/callback
```

凭证一律通过环境变量注入，不写进代码、不进镜像。

## Docker 部署

```bash
docker build -t icebreaker .
docker run -p 8000:8000 -e ZHIHU_ACCESS_SECRET=<secret> icebreaker
```

## 设计原则

- **没有授权就不生成。**「你的分身」只来自用户本人的创作。没有授权时接口直接拒绝，不会拿任何其他来源的数据顶上。
- **做连接，不做评价。** 不评分、不排序、不贴标签。信息少的人也有方案——判据是有效信息密度，不是数量。
- **推断和事实分开。** 档案里每条都标注来源：是 AI 推断，还是本人补充。
