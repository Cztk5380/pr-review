# GitCode PR 审查草稿生成器

输入 GitCode PR 链接 → 自动同步本地仓（clone/fetch）→ 拉取 PR 与文件 diff → 产出审查草稿文件。  
**不会自动回帖，不会改动仓库代码。**

支持两种后端：

- `agent`（默认）：生成"给任意 AI Agent 的审查任务草稿"，适用于 Cursor、Claude Code 等
- `api`：调用 OpenAI 兼容接口，直接生成审查结论草稿

---

## 快速开始

### 1. 安装依赖

```bash
python -m venv .venv

# Windows
.\.venv\Scripts\Activate.ps1
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. 配置环境变量

复制 `.env.example` 为 `.env`，至少填写：

```
GITCODE_TOKEN=你的GitCode个人访问令牌
```

`api` 模式还需额外填写 `LLM_API_BASE` / `LLM_API_KEY` / `LLM_MODEL`。

完整配置项说明见 `.env.example`。

### 3. 运行

#### 方式 A：直接给 PR URL

```bash
# Windows
py review_draft_cursor.py --pr "https://gitcode.com/Ascend/msserviceprofiler/pulls/123"

# macOS / Linux
python3 review_draft_cursor.py --pr "https://gitcode.com/Ascend/msserviceprofiler/pulls/123"
```

#### 方式 B：给 PR 编号

```bash
python3 review_draft_cursor.py --pr 123 --owner Ascend --repo msserviceprofiler
```

运行后在 `output/` 目录生成：

```
review_task_Ascend_msserviceprofiler_pr123_YYYYMMDD_HHMMSS.md
```

把该文件内容提供给任意 AI Agent，即可得到结构化审查意见。

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

## 可选：API 模式

不依赖 AI 工具，直接调用 OpenAI 兼容接口输出审查结论：

```bash
python3 review_draft_cursor.py --pr 123 --owner Ascend --repo msserviceprofiler --backend api
```

输出文件为 `review_Ascend_msserviceprofiler_pr123_YYYYMMDD_HHMMSS.md`。

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
