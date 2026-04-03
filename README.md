# GitCode PR 审查草稿生成器

输入 GitCode PR 链接 → 自动同步本地仓（clone/fetch）→ 拉取 PR 与文件 diff → 产出审查草稿文件。  
**不会自动回帖，不会改动仓库代码。**

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

> 如需隔离安装环境，可先创建虚拟环境：`python -m venv .venv`，再激活（Windows：`.\.venv\Scripts\Activate.ps1`，macOS/Linux：`source .venv/bin/activate`），然后再执行 `pip install`。

### 2. 配置环境变量

复制 `.env.example` 为 `.env`，至少填写：

```
GITCODE_TOKEN=你的GitCode个人访问令牌
```

完整配置项说明见 `.env.example`。

---

## 在 AI 工具中直接使用

### Cursor

在聊天框输入（两种写法均可）：

```
/review https://gitcode.com/Owner/repo/pulls/123
review https://gitcode.com/Owner/repo/pulls/123
```

Cursor 会自动执行脚本并直接输出审查意见，无需手动操作。

### Claude Code

```
/review https://gitcode.com/Owner/repo/pulls/123
```

### 其他 AI 工具

手动运行脚本后，将 `output/review_task_*.md` 的内容粘贴给任意 AI 助手即可。

---

## 手动运行脚本

#### 方式 A：直接给 PR URL

```bash
# Windows
py review_draft.py --pr "https://gitcode.com/Ascend/msserviceprofiler/pulls/123"

# macOS / Linux
python3 review_draft.py --pr "https://gitcode.com/Ascend/msserviceprofiler/pulls/123"
```

#### 方式 B：给 PR 编号

```bash
python3 review_draft.py --pr 123 --owner Ascend --repo msserviceprofiler
```

运行后在 `output/` 目录生成：

```
review_task_Ascend_msserviceprofiler_pr123_YYYYMMDD_HHMMSS.md
```

把该文件内容提供给任意 AI Agent，即可得到结构化审查意见。

---

## 可选：API 模式 / 本地模型模式

不依赖 AI 工具，直接调用模型输出审查结论，支持两种子模式：

### `--backend api`（云端大模型）

适合 GPT-4o、Claude 等上下文窗口足够大的云端模型，整个 PR diff 单次发送：

```bash
python3 review_draft.py --pr 123 --owner Ascend --repo msserviceprofiler --backend api
```

需在 `.env` 中配置：`LLM_API_BASE` / `LLM_API_KEY` / `LLM_MODEL`

### `--backend local`（本地小模型，支持并发）

适合在 Linux 服务器上部署的 Qwen、LLaMA 等小模型（通过 vLLM / Ollama 等暴露 OpenAI 兼容接口）。  
自动将 diff **按文件拆成小批次**，通过线程池并发发送，最后汇总结果：

```bash
python3 review_draft.py --pr 123 --owner Ascend --repo msserviceprofiler --backend local
```

需在 `.env` 中配置：

```
LLM_API_BASE=http://your-server:8000/v1   # vLLM / Ollama 地址
LLM_API_KEY=                              # 本地模型可留空
LLM_MODEL=qwen2.5-7b-instruct            # 与服务端模型名一致

LOCAL_BATCH_CHARS=6000    # 每批 diff 字符上限（根据模型 context window 调整）
LOCAL_MAX_WORKERS=4       # 并发请求数（不超过服务端并发上限）
LOCAL_REQUEST_TIMEOUT=180 # 单次请求超时（秒）
```

输出文件为 `review_<owner>_<repo>_pr<N>_<timestamp>.md`。

---

## 增量复审

开启 `INCREMENTAL_REVIEW=true`（默认）后：

- 缓存上次同一 PR 的 diff 指纹与每文件哈希至 `.review_cache/`
- 再次审查时只处理变更文件，跳过未变更文件
- 对强制推送（内容未变）场景同样有效：指纹未变则输出 `[SKIP]` 并退出

---

## 主要环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `GITCODE_TOKEN` | — | GitCode PAT（必填） |
| `MAX_PATCH_CHARS` | `40000` | diff 总字符预算 |
| `MAX_FILES_IN_DIFF` | `25` | 最多展开文件数 |
| `SECOND_PASS_FOCUS_FILES` | `6` | 二阶段重点展开文件数 |
| `ENABLE_LINE_CANDIDATES` | `false` | 是否输出行级评论候选文件 |
| `INCREMENTAL_REVIEW` | `true` | 是否启用增量复审 |

完整列表见 `.env.example`。

---

## 对接的 GitCode API

- `GET /repos/{owner}/{repo}/pulls/{number}` — PR 元数据
- `GET /repos/{owner}/{repo}/pulls/{number}/files` — 文件变更列表（自动分页）

参考文档：<https://docs.gitcode.com/v1-docs/docs/openapi/repos/pulls/>
