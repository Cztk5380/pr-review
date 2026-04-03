import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv


def must_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量: {name}")
    return value


def parse_pr_input(pr_input: str) -> Tuple[str, str, int]:
    s = pr_input.strip()
    if s.isdigit():
        return "", "", int(s)

    patterns = [
        r"https?://gitcode\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pulls/(?P<number>\d+)",
        r"https?://gitcode\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)",
        r"https?://gitcode\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/merge_requests/(?P<number>\d+)",
    ]
    for pat in patterns:
        m = re.match(pat, s)
        if m:
            return m.group("owner"), m.group("repo"), int(m.group("number"))
    raise ValueError("无法解析 PR 输入。请提供 PR URL 或 PR 编号。")


def gitcode_get(api_base: str, token: str, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    url = f"{api_base.rstrip('/')}/{path.lstrip('/')}"
    headers = {"Authorization": f"token {token}"}
    resp = requests.get(url, headers=headers, params=params or {}, timeout=60)
    if resp.status_code >= 400:
        raise RuntimeError(f"GitCode API 失败: {resp.status_code} {resp.text[:500]}")
    return resp.json()


def gitcode_get_all_pages(api_base: str, token: str, path: str, params: Optional[Dict[str, Any]] = None) -> List[Any]:
    """分页拉取列表接口，自动合并所有页结果。"""
    base_params = dict(params or {})
    base_params.setdefault("per_page", 100)
    page = 1
    results: List[Any] = []
    while True:
        base_params["page"] = page
        data = gitcode_get(api_base, token, path, base_params)
        if not isinstance(data, list):
            # 某些接口把列表包在字段里
            if isinstance(data, dict):
                inner = data.get("files") or data.get("data") or data.get("items")
                if isinstance(inner, list):
                    data = inner
                else:
                    break
            else:
                break
        if not data:
            break
        results.extend(data)
        if len(data) < base_params["per_page"]:
            break
        page += 1
    return results


def fetch_pr_context(api_base: str, token: str, owner: str, repo: str, number: int) -> Dict[str, Any]:
    pr = gitcode_get(api_base, token, f"repos/{owner}/{repo}/pulls/{number}")
    files = gitcode_get_all_pages(api_base, token, f"repos/{owner}/{repo}/pulls/{number}/files")
    if not isinstance(files, list):
        files = []
    return {"pr": pr, "files": files}


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _file_priority(file_item: Dict[str, Any]) -> int:
    filename = str(file_item.get("filename") or file_item.get("patch", {}).get("new_path") or "").lower()
    additions = _to_int(file_item.get("additions"), 0)
    deletions = _to_int(file_item.get("deletions"), 0)
    churn = additions + deletions

    risk_bonus = 0
    risk_keywords = ("core", "processor", "plugin", "exporter", "service", "security", "auth")
    if any(k in filename for k in risk_keywords):
        risk_bonus += 800
    if filename.endswith((".py", ".go", ".java", ".ts", ".js")):
        risk_bonus += 200
    return churn + risk_bonus


def _safe_truncate(s: str, max_chars: int) -> str:
    """在 max_chars 处截断，但不切断 UTF-8 多字节字符，尽量在换行处截断。"""
    if len(s) <= max_chars:
        return s
    # 尝试在换行处截断，避免切断 diff 行结构
    cut = s.rfind("\n", 0, max_chars)
    if cut < max_chars // 2:
        cut = max_chars
    return s[:cut]
    lines = diff_text.splitlines()
    hunks: List[List[str]] = []
    current: List[str] = []
    for line in lines:
        if line.startswith("@@"):
            if current:
                hunks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        hunks.append(current)
    return ["\n".join(h) for h in hunks]


def _compact_diff_for_file(diff_text: str, max_hunks_per_file: int, max_chars_per_file: int) -> str:
    hunks = _split_hunks(diff_text)
    if not hunks:
        compact = diff_text[:max_chars_per_file]
        return compact

    selected = hunks[:max_hunks_per_file]
    compact = "\n".join(selected)
    if len(compact) > max_chars_per_file:
        compact = _safe_truncate(compact, max_chars_per_file)
    return compact


def build_diff_excerpt(
    files: List[Dict[str, Any]],
    max_patch_chars: int,
    max_files_in_diff: int,
    max_hunks_per_file: int,
    max_chars_per_file: int,
    second_pass_focus_files: int,
    second_pass_hunks_per_file: int,
    second_pass_chars_per_file: int,
) -> Tuple[str, str]:
    files_sorted = sorted(files, key=_file_priority, reverse=True)
    with_diff: List[Tuple[str, Dict[str, Any], str]] = []
    for file_item in files_sorted:
        filename = file_item.get("filename") or file_item.get("patch", {}).get("new_path") or "unknown"
        patch = file_item.get("patch", {})
        diff = patch.get("diff", "") if isinstance(patch, dict) else ""
        if diff:
            with_diff.append((str(filename), file_item, diff))

    primary_items = with_diff[:max_files_in_diff]
    focus_count = min(second_pass_focus_files, len(primary_items))
    focus_indices = set(range(focus_count))

    chunks: List[str] = []
    used = 0
    included_files: List[str] = []
    focused_included: List[str] = []

    for idx, (filename, _file_item, diff) in enumerate(primary_items):
        is_focus = idx in focus_indices
        hunks_limit = second_pass_hunks_per_file if is_focus else max_hunks_per_file
        chars_limit = second_pass_chars_per_file if is_focus else max_chars_per_file
        compact_diff = _compact_diff_for_file(diff, hunks_limit, chars_limit)
        if not compact_diff.strip():
            continue

        header = f"\n### FILE: {filename}\n"
        body = f"```diff\n{compact_diff}\n```\n"
        add_len = len(header) + len(body)
        if used + add_len > max_patch_chars:
            remain = max_patch_chars - used
            if remain > 300:
                truncated_body = _safe_truncate(body, remain - len(header))
                chunks.append(header + truncated_body + "\n... [TRUNCATED]\n")
                included_files.append(str(filename))
                if is_focus:
                    focused_included.append(str(filename))
            break
        chunks.append(header + body)
        used += add_len
        included_files.append(str(filename))
        if is_focus:
            focused_included.append(str(filename))

    if not chunks:
        return "（该 PR 未返回可用 diff，可能是二进制文件或平台限制）", "（未展开文件摘要不可用）"

    candidates = len(with_diff)
    omitted = max(candidates - len(included_files), 0)
    focus_names = ", ".join(focused_included[:8]) if focused_included else "（无）"
    omitted_summary = (
        f"二阶段展开：首轮筛选 {len(primary_items)}/{candidates} 个文件；"
        f"二轮对前 {focus_count} 个高风险文件做加深展开。"
        f"最终在预算内展开 {len(included_files)} 个文件，未展开 {omitted} 个。"
        f"二轮重点文件：{focus_names}"
    )
    return "".join(chunks), omitted_summary


def build_changed_file_list(files: List[Dict[str, Any]], limit: int = 120) -> str:
    paths: List[str] = []
    for file_item in files:
        filename = file_item.get("filename") or file_item.get("patch", {}).get("new_path")
        if filename:
            paths.append(str(filename))
        if len(paths) >= limit:
            break
    if not paths:
        return "（无可用文件列表）"
    return "\n".join(f"- {path}" for path in paths)


def _parse_new_line_range(hunk_header: str) -> Tuple[int, int]:
    m = re.search(r"\+(\d+)(?:,(\d+))?", hunk_header)
    if not m:
        return 0, 0
    start = int(m.group(1))
    count = int(m.group(2)) if m.group(2) else 1
    end = start + max(count - 1, 0)
    return start, end


def _extract_added_code_blocks(
    diff_text: str,
    max_lines_per_block: int = 16,
) -> List[Tuple[int, int, str, str]]:
    """从 diff hunk 中提取更细粒度的新增代码块行号范围。

    返回: [(start_line, end_line, hunk_header, preview_text), ...]
    """
    blocks: List[Tuple[int, int, str, str]] = []
    for hunk in _split_hunks(diff_text):
        lines = hunk.splitlines()
        if not lines:
            continue
        header_line = lines[0]
        new_start, _ = _parse_new_line_range(header_line)
        if new_start <= 0:
            continue

        new_line_no = new_start
        block_start: Optional[int] = None
        block_end: Optional[int] = None
        block_preview = ""

        def flush_block() -> None:
            nonlocal block_start, block_end, block_preview
            if block_start is not None and block_end is not None:
                blocks.append((block_start, block_end, header_line, block_preview))
            block_start = None
            block_end = None
            block_preview = ""

        for raw in lines[1:]:
            if raw.startswith("+") and not raw.startswith("+++"):
                content = raw[1:]
                stripped = content.strip()

                # 空行也计入行号，但不并入有效代码块，避免范围过大。
                if not stripped:
                    flush_block()
                    new_line_no += 1
                    continue

                if block_start is None:
                    block_start = new_line_no
                    block_preview = stripped[:120]
                block_end = new_line_no
                new_line_no += 1

                if block_end - block_start + 1 >= max_lines_per_block:
                    flush_block()
                continue

            if raw.startswith(" "):
                flush_block()
                new_line_no += 1
                continue

            if raw.startswith("-") and not raw.startswith("---"):
                flush_block()
                continue

            flush_block()

        flush_block()

    return blocks


def build_diff_line_reference(
    files: List[Dict[str, Any]],
    max_files: int = 12,
    max_lines_per_file: int = 1200,
) -> str:
    """为每个变更文件生成精确的新文件行号速查表。

    每行格式：
      L<新行号> [+] <内容前80字符>
      [DEL→L<N>] [-] <被删除行内容前80字符>
    仅保留 hunk 头与真实改动行（+/-），不输出上下文行，降低截断风险。
    速查表供 AI 审查时精确定位行号，避免估算偏差。
    """
    sorted_files = sorted(files, key=_file_priority, reverse=True)[:max_files]
    sections: List[str] = []

    for file_item in sorted_files:
        filename = str(
            file_item.get("filename") or file_item.get("patch", {}).get("new_path") or "unknown"
        )
        patch = file_item.get("patch", {})
        diff = patch.get("diff", "") if isinstance(patch, dict) else ""
        if not diff:
            continue

        file_lines: List[str] = []
        new_line_no = 0
        line_count = 0

        for raw in diff.splitlines():
            if line_count >= max_lines_per_file:
                file_lines.append("  ... [行号表已截断]")
                break

            if raw.startswith("@@"):
                new_start, _ = _parse_new_line_range(raw)
                if new_start > 0:
                    new_line_no = new_start
                file_lines.append(f"  {raw}")
                line_count += 1
                continue

            if raw.startswith("---") or raw.startswith("+++"):
                continue

            if raw.startswith("+"):
                content = raw[1:81]
                file_lines.append(f"  L{new_line_no:>5} [+] {content}")
                new_line_no += 1
                line_count += 1
            elif raw.startswith(" "):
                # 速查表仅保留真实改动行，避免上下文过多导致后续改动被截断
                new_line_no += 1
            elif raw.startswith("-"):
                content = raw[1:81]
                # 被删行无新行号，标注 DEL，并注明其紧邻的下一新行号供定位参考
                file_lines.append(f"  [DEL→L{new_line_no:>4}] [-] {content}")
                line_count += 1

        if file_lines:
            sections.append(f"### {filename}\n" + "\n".join(file_lines))

    if not sections:
        return "（无可用行号速查表）"
    return "\n\n".join(sections)


def build_line_comment_candidates(
    files: List[Dict[str, Any]],
    max_candidates: int,
    max_candidates_in_prompt: int,
) -> Tuple[List[Dict[str, Any]], str]:
    candidates: List[Dict[str, Any]] = []
    sorted_files = sorted(files, key=_file_priority, reverse=True)
    for file_item in sorted_files:
        filename = str(file_item.get("filename") or file_item.get("patch", {}).get("new_path") or "unknown")
        patch = file_item.get("patch", {})
        diff = patch.get("diff", "") if isinstance(patch, dict) else ""
        if not diff:
            continue
        for start, end, header_line, preview in _extract_added_code_blocks(diff):
            candidates.append(
                {
                    "file": filename,
                    "new_start_line": start,
                    "new_end_line": end,
                    "hunk_header": header_line,
                    "preview": preview,
                    "priority": _file_priority(file_item),
                }
            )
            if len(candidates) >= max_candidates:
                break
        if len(candidates) >= max_candidates:
            break

    prompt_candidates = candidates[:max_candidates_in_prompt]
    if not prompt_candidates:
        return candidates, "（未生成行级评论候选）"

    lines = []
    for i, c in enumerate(prompt_candidates, 1):
        preview = str(c.get("preview", "")).strip()
        if preview:
            lines.append(f"- [{i}] {c['file']}:{c['new_start_line']}-{c['new_end_line']} | {preview}")
        else:
            lines.append(f"- [{i}] {c['file']}:{c['new_start_line']}-{c['new_end_line']} | {c['hunk_header']}")
    summary = "\n".join(lines)
    if len(candidates) > len(prompt_candidates):
        summary += f"\n- ... 其余 {len(candidates) - len(prompt_candidates)} 条候选已写入 JSON 文件"
    return candidates, summary


def _state_file_path(cache_dir: Path, owner: str, repo: str, number: int) -> Path:
    safe_key = f"{owner}__{repo}__{number}".replace("/", "_")
    return cache_dir / f"{safe_key}.json"


def load_review_state(cache_dir: Path, owner: str, repo: str, number: int) -> Dict[str, Any]:
    state_file = _state_file_path(cache_dir, owner, repo, number)
    if not state_file.exists():
        return {}
    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_review_state(cache_dir: Path, owner: str, repo: str, number: int, state: Dict[str, Any]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    state_file = _state_file_path(cache_dir, owner, repo, number)
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def build_diff_fingerprint(files: List[Dict[str, Any]]) -> Tuple[str, Dict[str, str]]:
    file_hashes: Dict[str, str] = {}
    for item in files:
        filename = str(item.get("filename") or item.get("patch", {}).get("new_path") or "unknown")
        patch = item.get("patch", {})
        diff = patch.get("diff", "") if isinstance(patch, dict) else ""
        per_file_hash = hashlib.sha256(diff.encode("utf-8", errors="ignore")).hexdigest()
        file_hashes[filename] = per_file_hash
    joined = "".join(f"{k}:{file_hashes[k]}\n" for k in sorted(file_hashes.keys()))
    all_hash = hashlib.sha256(joined.encode("utf-8")).hexdigest()
    return all_hash, file_hashes


def filter_incremental_files(
    files: List[Dict[str, Any]],
    previous_hashes: Dict[str, str],
    incremental_enabled: bool,
) -> Tuple[List[Dict[str, Any]], str]:
    if not incremental_enabled or not previous_hashes:
        return files, "全量复审模式"

    changed: List[Dict[str, Any]] = []
    unchanged = 0
    for item in files:
        filename = str(item.get("filename") or item.get("patch", {}).get("new_path") or "unknown")
        patch = item.get("patch", {})
        diff = patch.get("diff", "") if isinstance(patch, dict) else ""
        per_file_hash = hashlib.sha256(diff.encode("utf-8", errors="ignore")).hexdigest()
        if previous_hashes.get(filename) != per_file_hash:
            changed.append(item)
        else:
            unchanged += 1

    if not changed:
        return changed, f"增量复审模式：无新增变更文件（未变更文件 {unchanged} 个）"
    return changed, f"增量复审模式：仅复审变更文件 {len(changed)} 个（未变更文件 {unchanged} 个）"


def run_git_command(args: List[str], cwd: Optional[Path] = None) -> Tuple[int, str]:
    proc = subprocess.run(
        ["git"] + args,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, proc.stdout.strip()


def sync_local_repo(owner: str, repo: str, target_branch: str = "master") -> Tuple[str, str]:
    repo_root = Path(os.getenv("LOCAL_REPO_ROOT", "repos"))
    repo_override = os.getenv("LOCAL_REPO_PATH", "").strip()
    token = os.getenv("GITCODE_TOKEN", "").strip()

    local_repo_path = Path(repo_override) if repo_override else (repo_root / owner / repo)
    remote_base = os.getenv("REPO_REMOTE_URL", "").strip()
    if not remote_base:
        remote_base = f"https://gitcode.com/{owner}/{repo}.git"
    # 将 token 注入 URL，避免存入 .git/config（用 -c http.extraHeader 而非 URL 嵌入）
    git_auth_args = ["-c", f"http.extraHeader=Authorization: token {token}"] if token else []

    if local_repo_path.exists() and not (local_repo_path / ".git").exists():
        return (
            "失败：本地路径已存在但不是 Git 仓库，请清理后重试",
            str(local_repo_path.resolve()),
        )

    local_repo_path.parent.mkdir(parents=True, exist_ok=True)
    if not local_repo_path.exists():
        code, output = run_git_command(git_auth_args + ["clone", remote_base, str(local_repo_path)])
        if code != 0:
            return (f"失败：clone 异常 -> {output}", str(local_repo_path.resolve()))

    code, output = run_git_command(git_auth_args + ["fetch", "--all", "--prune"], cwd=local_repo_path)
    if code != 0:
        return (f"失败：fetch 异常 -> {output}", str(local_repo_path.resolve()))

    branch = target_branch.strip() or "master"
    # 防止分支名以 - 开头被 git 解析为标志
    if branch.startswith("-"):
        return (f"失败：分支名非法: {branch}", str(local_repo_path.resolve()))

    code, output = run_git_command(["checkout", "--", "."], cwd=local_repo_path)  # 清理工作区
    code, output = run_git_command(["checkout", branch], cwd=local_repo_path)
    if code != 0:
        # 尝试 main 作为 fallback
        alt = "main" if branch == "master" else "master"
        code2, output2 = run_git_command(["checkout", alt], cwd=local_repo_path)
        if code2 == 0:
            branch = alt
            output = output2
        else:
            return (f"失败：checkout {branch} 异常 -> {output}", str(local_repo_path.resolve()))

    code, output = run_git_command(["reset", "--hard", f"origin/{branch}"], cwd=local_repo_path)
    if code != 0:
        return (f"失败：reset --hard origin/{branch} 异常 -> {output}", str(local_repo_path.resolve()))

    return (f"成功：仓库已同步到最新远端状态（分支：{branch}）", str(local_repo_path.resolve()))


def llm_generate_markdown(
    llm_base: str,
    llm_key: str,
    model: str,
    owner: str,
    repo: str,
    number: int,
    pr_data: Dict[str, Any],
    diff_text: str,
) -> str:
    url = f"{llm_base.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {llm_key}", "Content-Type": "application/json"}

    pr_title = pr_data.get("title", "")
    pr_body = pr_data.get("body") or pr_data.get("description") or ""
    source_branch = pr_data.get("source_branch", "")
    target_branch = pr_data.get("target_branch", "")

    system_prompt = (
        "你是资深代码审查工程师。请基于给定 PR 信息与 diff，"
        "输出中文审查草稿，要求严谨、可执行、避免臆测。只输出 Markdown。"
    )
    user_prompt = f"""
仓库: {owner}/{repo}
PR: #{number}
标题: {pr_title}
源分支: {source_branch}
目标分支: {target_branch}
描述:
{pr_body}

请按以下结构输出：
1) 概览（1-3条）
2) 重点风险（按高/中/低）
3) 详细审查意见（每条必须使用固定模板，见下）
4) 建议补充测试（若无写“暂无”）
5) 可直接发布到PR的“总评草稿”

注意：
- 如果证据不足，明确标注“需人工确认”
- 不要编造未在diff中出现的文件或逻辑
- 评审偏向 bug/稳定性/性能/可维护性

详细审查意见固定模板（强制）：
[文件:<path>] [行号:<start>-<end>] [严重级别:<高/中/低>] 【review】<一句话标题> <具体问题点，定位到文件/函数/行为> <影响与触发条件；证据不足时写“需人工确认”> 修改建议：<可直接落地的改法 + 最小改动代码（必须包含代码，优先使用行内代码 `...`）>

以下是 diff 片段：
{diff_text}
"""
    payload = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=120)
    if resp.status_code >= 400:
        raise RuntimeError(f"LLM API 失败: {resp.status_code} {resp.text[:500]}")
    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        raise RuntimeError(f"LLM 返回格式异常: {data}") from exc


def build_cursor_task_markdown(
    owner: str,
    repo: str,
    number: int,
    diff_text: str,
    changed_files_text: str,
    diff_coverage_text: str,
    line_comment_candidates_text: str,
    diff_line_reference_text: str,
    incremental_summary: str,
    repo_sync_status: str,
    local_repo_path: str,
    target_branch: str = "master",
) -> str:
    return f"""请你作为资深代码审查工程师，对以下 PR 进行审查，并输出“可直接发到 PR 的审查草稿”。

要求：
1) 输出中文
2) 先给“发现的问题”，按严重级别排序（高/中/低）
3) 每条问题必须包含：严重级别、文件、问题描述、证据、建议修改
4) 证据不足时明确写“需人工确认”
5) 额外给一段“总评草稿”（可直接粘贴到 PR）

检视质量（强约束）：
a. 避免仅规范复述或咨询式意见
b. 结论必须给出推理与影响
c. 优先覆盖设计、安全、性能、稳定性
d. 证据不足必须标注“需人工确认”

检视全面性（强约束）：
a. 不能只做规范类检查（编码要求、license 要求、咨询类意见）
b. 不能只关注功能正确性或编码技巧优化
c. 必须覆盖代码设计、安全、性能等多个维度（按风险给出优先级）

输出风格（强约束）：
- 优先给“可落地修改建议”，避免空泛表述
- 对高风险项给出潜在后果和触发场景，直接融入问题描述，不要单独写「触发条件：」标签
- 若未发现问题，明确写“未发现实质性缺陷”，并补充剩余测试风险
- 行号定位【强制约束】：必须且只能使用下方“diff 行号速查表”中明确列出的 L<行号>；禁止通过数行数、图案规模、经验估算行号
- 每条详细意见必须显式包含 `[严重级别:<高/中/低>]`
- 每条详细意见的“修改建议”必须包含最小改动代码，代码必须使用行内代码 `...` 格式（禁止使用代码块，禁止只写文字建议）
- 详细意见必须严格按以下模板输出，便于一键复制：
  [文件:<path>] [行号:<start>-<end>] [严重级别:<高/中/低>] 【review】<一句话标题> <具体问题点 + 影响范围 + 触发场景，自然融入描述，禁止显式写「触发条件：」标签；证据不足时写“需人工确认”> 修改建议：<改法描述，代码必须使用行内代码 `改动示例` 格式，禁止只写文字>
  - 行号不在速查表中时（如需引用被删除行），右表中 [DEL→L<N>] 的 N 作为挂靠行号，并标注“(被删除行的挂靠行)”；行号确实无法确定时写“需人工确认”

审查目标：{owner}/{repo} PR #{number}

本地仓同步状态：{repo_sync_status}
本地仓路径：{local_repo_path}
本地仓当前分支：{target_branch}（即 PR 目标分支，代码上下文基于此分支）
复审模式：{incremental_summary}
为避免“只看 diff 的盲区”，请在必要时先搜索本地代码上下文（调用链、实现处、同名符号）再下结论。

本次变更文件列表：
{changed_files_text}

diff 覆盖说明：
{diff_coverage_text}

行级评论候选定位（用于行级评论回帖）：
{line_comment_candidates_text}


diff 行号速查表（行号为新文件行号，审查时严格从此表取行号）：
{diff_line_reference_text}
以下是 diff 片段：
{diff_text}
"""


def save_markdown(owner: str, repo: str, number: int, content: str, prefix: str) -> Path:
    out_dir = Path("output")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = out_dir / f"{prefix}_{owner}_{repo}_pr{number}_{ts}.md"
    output.write_text(content, encoding="utf-8")
    return output


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="GitCode PR 审查草稿生成器")
    parser.add_argument("--pr", required=True, help="PR URL 或 PR 编号")
    parser.add_argument("--owner", default="", help="当 --pr 为纯数字时必填")
    parser.add_argument("--repo", default="", help="当 --pr 为纯数字时必填")
    parser.add_argument(
        "--backend",
        choices=["agent", "api", "cursor"],  # cursor 保留为 agent 的兼容别名
        default="agent",
        help="agent=生成给任意 AI Agent 的任务草稿（默认）；api=调用 OpenAI 兼容接口直接生成结论；cursor=同 agent（兼容旧用法）",
    )
    args = parser.parse_args()
    # 兼容旧的 --backend cursor 写法
    if args.backend == "cursor":
        args.backend = "agent"

    try:
        token = must_env("GITCODE_TOKEN")
        api_base = os.getenv("GITCODE_API_BASE", "https://api.gitcode.com/api/v5")
        max_patch_chars = int(os.getenv("MAX_PATCH_CHARS", "40000"))
        max_files_in_diff = int(os.getenv("MAX_FILES_IN_DIFF", "25"))
        max_hunks_per_file = int(os.getenv("MAX_HUNKS_PER_FILE", "4"))
        max_chars_per_file = int(os.getenv("MAX_CHARS_PER_FILE", "5000"))
        second_pass_focus_files = int(os.getenv("SECOND_PASS_FOCUS_FILES", "6"))
        second_pass_hunks_per_file = int(os.getenv("SECOND_PASS_HUNKS_PER_FILE", "10"))
        second_pass_chars_per_file = int(os.getenv("SECOND_PASS_CHARS_PER_FILE", "12000"))
        changed_file_list_limit = int(os.getenv("CHANGED_FILE_LIST_LIMIT", "120"))
        max_line_candidates = int(os.getenv("MAX_LINE_CANDIDATES", "120"))
        max_candidates_in_prompt = int(os.getenv("MAX_CANDIDATES_IN_PROMPT", "40"))
        enable_line_candidates = os.getenv("ENABLE_LINE_CANDIDATES", "false").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        incremental_review = os.getenv("INCREMENTAL_REVIEW", "true").lower() in ("1", "true", "yes", "on")
        cache_dir = Path(os.getenv("CACHE_DIR", ".review_cache"))

        owner, repo, number = parse_pr_input(args.pr)
        if not owner or not repo:
            owner, repo = args.owner.strip(), args.repo.strip()
        if not owner or not repo:
            raise ValueError("当 --pr 是纯数字时，必须提供 --owner 和 --repo。")

        context = fetch_pr_context(api_base, token, owner, repo, number)
        files = context.get("files", [])
        pr_data = context.get("pr", {})

        # 从 PR 信息中提取目标分支，优先用于同步本地仓库上下文
        target_branch = (
            pr_data.get("target_branch")
            or (pr_data.get("base") or {}).get("ref")
            or "master"
        )
        repo_sync_status, local_repo_path = sync_local_repo(owner, repo, target_branch)
        current_fingerprint, current_file_hashes = build_diff_fingerprint(files)
        old_state = load_review_state(cache_dir, owner, repo, number)
        old_file_hashes = old_state.get("file_hashes", {})

        files_for_review, incremental_summary = filter_incremental_files(
            files=files,
            previous_hashes=old_file_hashes,
            incremental_enabled=incremental_review,
        )
        if incremental_review and old_state.get("fingerprint") == current_fingerprint:
            incremental_summary = "增量复审模式：PR diff 指纹未变化（含强制推送但内容未变场景）"

        # 增量模式且无变化时直接跳过，不生成重复任务文件
        if incremental_review and old_file_hashes and not files_for_review:
            print(f"[SKIP] {incremental_summary}，无需重新生成审查文件。")
            return 0

        target_files = files_for_review if files_for_review else files
        diff_excerpt, diff_coverage_text = build_diff_excerpt(
            target_files,
            max_patch_chars=max_patch_chars,
            max_files_in_diff=max_files_in_diff,
            max_hunks_per_file=max_hunks_per_file,
            max_chars_per_file=max_chars_per_file,
            second_pass_focus_files=second_pass_focus_files,
            second_pass_hunks_per_file=second_pass_hunks_per_file,
            second_pass_chars_per_file=second_pass_chars_per_file,
        )
        changed_files_text = build_changed_file_list(target_files, limit=changed_file_list_limit)
        # 始终生成候选并写入任务提示，保证审查输出中的行号锚点稳定。
        # 仅通过 ENABLE_LINE_CANDIDATES 控制是否额外落盘 line_candidates 文件。
        line_candidates, line_comment_candidates_text = build_line_comment_candidates(
            target_files,
            max_candidates=max_line_candidates,
            max_candidates_in_prompt=max_candidates_in_prompt,
        )
        diff_line_reference_text = build_diff_line_reference(
            target_files,
            max_files=int(os.getenv("DIFF_LINE_REF_MAX_FILES", "10")),       # 行号速查表最多展开文件数
            max_lines_per_file=int(os.getenv("DIFF_LINE_REF_MAX_LINES", "300")),  # 每文件最多行数
        )

        if not enable_line_candidates:
            line_comment_candidates_text += "\n（候选文件默认不落盘；设置 ENABLE_LINE_CANDIDATES=true 可输出 line_candidates 文件）"

        if args.backend == "api":
            llm_base = must_env("LLM_API_BASE")
            llm_key = must_env("LLM_API_KEY")
            model = os.getenv("LLM_MODEL", "gpt-4o-mini")
            markdown = llm_generate_markdown(
                llm_base=llm_base,
                llm_key=llm_key,
                model=model,
                owner=owner,
                repo=repo,
                number=number,
                pr_data=pr_data,
                diff_text=diff_excerpt,
            )
            output_path = save_markdown(owner, repo, number, markdown, prefix="review")
            print(f"[OK] 审查草稿已生成: {output_path}")
            return 0

        task_markdown = build_cursor_task_markdown(
            owner=owner,
            repo=repo,
            number=number,
            diff_text=diff_excerpt,
            changed_files_text=changed_files_text,
            diff_coverage_text=diff_coverage_text,
            line_comment_candidates_text=line_comment_candidates_text,
            diff_line_reference_text=diff_line_reference_text,
            incremental_summary=incremental_summary,
            repo_sync_status=repo_sync_status,
            local_repo_path=local_repo_path,
            target_branch=target_branch,
        )
        output_path = save_markdown(owner, repo, number, task_markdown, prefix="review_task")
        line_candidates_path: Optional[Path] = None
        if enable_line_candidates:
            line_candidates_path = save_markdown(
                owner,
                repo,
                number,
                json.dumps(line_candidates, ensure_ascii=False, indent=2),
                prefix="line_candidates",
            )
        save_review_state(
            cache_dir,
            owner,
            repo,
            number,
            {
                "owner": owner,
                "repo": repo,
                "number": number,
                "head_sha": str(pr_data.get("sha", "")),
                "fingerprint": current_fingerprint,
                "file_hashes": current_file_hashes,
                "task_path": str(output_path),
                "line_candidates_enabled": enable_line_candidates,
                "line_candidates_path": str(line_candidates_path) if line_candidates_path else "",
                "updated_at": datetime.now().isoformat(),
            },
        )
        print(f"[OK] 已生成 Cursor 任务草稿: {output_path}")
        if line_candidates_path:
            print(f"[OK] 已生成行级评论候选: {line_candidates_path}")
        print("[TIP] 把该文件内容粘贴给 Cursor Agent，即可得到审查意见。")
        return 0
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
