import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
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
) -> Tuple[str, str, Dict[str, str]]:
    """返回 (diff摘要文本, 覆盖说明, {filename: 压缩后diff})。
    第三个返回值保证与摘要文本使用完全相同的截断数据，供行号速查表使用。"""
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
    compacted_diffs: Dict[str, str] = {}  # filename -> 压缩后 diff（与摘要一致）

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
                # 记录实际写入摘要的截断版本
                compacted_diffs[str(filename)] = _safe_truncate(compact_diff, remain - len(header) - 10)
                if is_focus:
                    focused_included.append(str(filename))
            break
        chunks.append(header + body)
        used += add_len
        included_files.append(str(filename))
        compacted_diffs[str(filename)] = compact_diff
        if is_focus:
            focused_included.append(str(filename))

    if not chunks:
        return "（该 PR 未返回可用 diff，可能是二进制文件或平台限制）", "（未展开文件摘要不可用）", {}

    candidates = len(with_diff)
    omitted = max(candidates - len(included_files), 0)
    focus_names = ", ".join(focused_included[:8]) if focused_included else "（无）"
    omitted_summary = (
        f"二阶段展开：首轮筛选 {len(primary_items)}/{candidates} 个文件；"
        f"二轮对前 {focus_count} 个高风险文件做加深展开。"
        f"最终在预算内展开 {len(included_files)} 个文件，未展开 {omitted} 个。"
        f"二轮重点文件：{focus_names}"
    )
    return "".join(chunks), omitted_summary, compacted_diffs


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
    compacted_diffs: Dict[str, str],
    max_lines_per_file: int = 300,
) -> str:
    """根据 build_diff_excerpt 已压缩的 diff 生成行号速查表。

    使用与摘要完全相同的数据，保证速查表中的行号与 diff 摘要一致。
    每行格式：
      L<新行号> [+] <内容前80字符>
      [DEL→L<N>] [-] <被删除行内容前80字符>
    """
    sections: List[str] = []

    for filename, diff in compacted_diffs.items():
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
                new_line_no += 1
            elif raw.startswith("-"):
                content = raw[1:81]
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


def run_git_command(args: List[str], cwd: Optional[Path] = None, timeout: int = 120) -> Tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip()
    except subprocess.TimeoutExpired:
        return 1, f"git 操作超时（>{timeout}s）：git {' '.join(args[:2])}"


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
        code, output = run_git_command(git_auth_args + ["clone", remote_base, str(local_repo_path)], timeout=300)
        if code != 0:
            return (f"失败：clone 异常 -> {output}", str(local_repo_path.resolve()))

    code, output = run_git_command(git_auth_args + ["fetch", "--all", "--prune"], cwd=local_repo_path, timeout=180)
    if code != 0:
        return (f"失败：fetch 异常 -> {output}", str(local_repo_path.resolve()))

    branch = target_branch.strip() or "master"
    # 防止分支名以 - 开头被 git 解析为标志
    if branch.startswith("-"):
        return (f"失败：分支名非法: {branch}", str(local_repo_path.resolve()))

    code, output = run_git_command(["checkout", "--", "."], cwd=local_repo_path, timeout=30)  # 清理工作区
    code, output = run_git_command(["checkout", branch], cwd=local_repo_path, timeout=30)
    if code != 0:
        # 尝试 main 作为 fallback
        alt = "main" if branch == "master" else "master"
        code2, output2 = run_git_command(["checkout", alt], cwd=local_repo_path, timeout=30)
        if code2 == 0:
            branch = alt
            output = output2
        else:
            return (f"失败：checkout {branch} 异常 -> {output}", str(local_repo_path.resolve()))

    code, output = run_git_command(["reset", "--hard", f"origin/{branch}"], cwd=local_repo_path, timeout=30)
    if code != 0:
        return (f"失败：reset --hard origin/{branch} 异常 -> {output}", str(local_repo_path.resolve()))

    return (f"成功：仓库已同步到最新远端状态（分支：{branch}）", str(local_repo_path.resolve()))


def _call_llm_single(
    url: str,
    headers: Dict[str, str],
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.2,
    timeout: int = 120,
) -> str:
    """向 OpenAI 兼容接口发送单次请求，返回文本内容。"""
    payload = {
        "model": model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    if resp.status_code >= 400:
        raise RuntimeError(f"LLM API 失败: {resp.status_code} {resp.text[:500]}")
    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        raise RuntimeError(f"LLM 返回格式异常: {data}") from exc


def llm_generate_markdown(
    llm_base: str,
    llm_key: str,
    model: str,
    owner: str,
    repo: str,
    number: int,
    pr_data: Dict[str, Any],
    diff_text: str,
    timeout: int = 120,
) -> str:
    """单次请求模式（--backend api），适合上下文窗口足够大的云端模型。"""
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
    user_prompt = f"""仓库: {owner}/{repo}  PR: #{number}
标题: {pr_title}  源分支: {source_branch} → 目标分支: {target_branch}
描述: {pr_body}

请按以下结构输出：
1) 概览（1-3条）
2) 重点风险（按高/中/低）
3) 详细审查意见（固定模板见下）
4) 建议补充测试（若无写"暂无"）
5) 总评草稿（可直接发布到PR）

详细意见模板（强制）：
[文件:<path>] [行号:<start>-<end>] [严重级别:<高/中/低>] 【review】<标题> <问题点+影响+触发场景> 修改建议：<改法+行内代码>

注意：证据不足写"需人工确认"；不编造 diff 中未出现的内容。

diff 片段：
{diff_text}"""

    return _call_llm_single(url, headers, model, system_prompt, user_prompt, timeout=timeout)


def _build_batch_prompts(
    owner: str,
    repo: str,
    number: int,
    pr_title: str,
    compacted_diffs: Dict[str, str],
    batch_chars: int,
) -> List[Tuple[List[str], str]]:
    """将 compacted_diffs 按 batch_chars 分批，返回 [(filenames, diff_text), ...]。"""
    batches: List[Tuple[List[str], str]] = []
    current_files: List[str] = []
    current_parts: List[str] = []
    current_len = 0

    for filename, diff in compacted_diffs.items():
        entry = f"\n### FILE: {filename}\n```diff\n{diff}\n```\n"
        if current_len + len(entry) > batch_chars and current_files:
            batches.append((current_files, "".join(current_parts)))
            current_files, current_parts, current_len = [], [], 0
        current_files.append(filename)
        current_parts.append(entry)
        current_len += len(entry)

    if current_files:
        batches.append((current_files, "".join(current_parts)))
    return batches


def llm_generate_local(
    llm_base: str,
    llm_key: str,
    model: str,
    owner: str,
    repo: str,
    number: int,
    pr_data: Dict[str, Any],
    compacted_diffs: Dict[str, str],
    batch_chars: int = 6000,
    max_workers: int = 4,
    request_timeout: int = 180,
) -> str:
    """并发批次模式（--backend local），适合本地小模型。

    将 diff 按 batch_chars 拆分成多个批次，并发发送给本地模型，
    最后把各批次结论合并为一个完整的审查报告。
    """
    url = f"{llm_base.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {llm_key}", "Content-Type": "application/json"}

    pr_title = pr_data.get("title", "")
    pr_body = pr_data.get("body") or pr_data.get("description") or ""

    batches = _build_batch_prompts(owner, repo, number, pr_title, compacted_diffs, batch_chars)
    if not batches:
        return "（无可用 diff，跳过本地模型审查）"

    total = len(batches)
    print(f"[LOCAL] 共 {total} 个批次，并发数={max_workers}，逐批发送给本地模型…")

    system_prompt = (
        "你是资深代码审查工程师，只输出中文。"
        "针对给出的代码变更，找出真实存在的 bug、安全、性能或稳定性问题。"
        "证据不足时写"需人工确认"，不编造内容，不重复规范类意见。"
    )

    def review_one_batch(idx_batch: Tuple[int, Tuple[List[str], str]]) -> Tuple[int, str]:
        idx, (filenames, diff_text) = idx_batch
        file_list = ", ".join(filenames)
        user_prompt = (
            f"仓库: {owner}/{repo}  PR #{number}  标题: {pr_title}\n"
            f"本批文件（{idx+1}/{total}）：{file_list}\n\n"
            "对每个问题严格按以下模板输出一行：\n"
            "[文件:<path>] [行号:<start>-<end>] [严重级别:<高/中/低>] "
            "【review】<一句话标题> <问题点+影响+触发场景> "
            "修改建议：<改法，代码用行内代码 `示例`>\n\n"
            "若本批无实质性问题，只输出：本批无实质性问题。\n\n"
            f"diff：\n{diff_text}"
        )
        try:
            result = _call_llm_single(
                url, headers, model, system_prompt, user_prompt,
                temperature=0.1, timeout=request_timeout,
            )
            print(f"[LOCAL] 批次 {idx+1}/{total} 完成")
            return idx, result
        except Exception as exc:
            print(f"[LOCAL] 批次 {idx+1}/{total} 失败: {exc}", file=sys.stderr)
            return idx, f"[批次 {idx+1} 审查失败: {exc}]"

    results: List[Tuple[int, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(review_one_batch, (i, b)): i for i, b in enumerate(batches)}
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())

    results.sort(key=lambda x: x[0])
    batch_sections = "\n\n".join(f"<!-- 批次 {i+1} -->\n{text}" for i, text in results)

    # 如果只有一个批次，直接返回；多批次做一次汇总
    if total == 1:
        return results[0][1]

    print(f"[LOCAL] 所有批次完成，正在汇总…")
    summary_user_prompt = (
        f"仓库: {owner}/{repo}  PR #{number}  标题: {pr_title}\n\n"
        "以下是对各批文件分别审查得到的原始意见，请你：\n"
        "1. 去除重复意见，保留最具代表性的一条\n"
        "2. 按严重级别排序（高→中→低）\n"
        "3. 在末尾写一段"总评草稿"（3-5句话，可直接粘贴到PR）\n"
        "输出格式保持原有模板，只输出 Markdown。\n\n"
        f"原始意见：\n{batch_sections}"
    )
    try:
        final = _call_llm_single(
            url, headers, model, system_prompt, summary_user_prompt,
            temperature=0.1, timeout=request_timeout,
        )
    except Exception as exc:
        # 汇总失败时直接拼接各批次结果
        print(f"[LOCAL] 汇总请求失败（{exc}），直接输出各批次原始结果", file=sys.stderr)
        final = batch_sections

    return final


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
    return f"""请你作为资深代码审查工程师，对以下 PR 进行审查，并输出"可直接发到 PR 的审查草稿"。

要求：
1) 输出中文
2) 先给"发现的问题"，按严重级别排序（高/中/低）
3) 每条问题必须包含：严重级别、文件、问题描述、证据、建议修改
4) 证据不足时明确写"需人工确认"
5) 额外给一段"总评草稿"（可直接粘贴到 PR）

检视质量（强约束）：
a. 避免仅规范复述或咨询式意见
b. 结论必须给出推理与影响
c. 优先覆盖设计、安全、性能、稳定性
d. 证据不足必须标注"需人工确认"

检视全面性（强约束）：
a. 不能只做规范类检查（编码要求、license 要求、咨询类意见）
b. 不能只关注功能正确性或编码技巧优化
c. 必须覆盖代码设计、安全、性能等多个维度（按风险给出优先级）

输出风格（强约束）：
- 优先给"可落地修改建议"，避免空泛表述
- 对高风险项给出潜在后果和触发场景，直接融入问题描述，不要单独写「触发条件：」标签
- 若未发现问题，明确写"未发现实质性缺陷"，并补充剩余测试风险
- 行号定位【强制约束】：必须且只能使用下方"diff 行号速查表"中明确列出的 L<行号>；禁止通过数行数、图案规模、经验估算行号
- 每条详细意见必须显式包含 `[严重级别:<高/中/低>]`
- 每条详细意见的"修改建议"必须包含最小改动代码，代码必须使用行内代码 `...` 格式（禁止使用代码块，禁止只写文字建议）
- 详细意见必须严格按以下模板输出，便于一键复制：
  [文件:<path>] [行号:<start>-<end>] [严重级别:<高/中/低>] 【review】<一句话标题> <具体问题点 + 影响范围 + 触发场景，自然融入描述，禁止显式写「触发条件：」标签；证据不足时写"需人工确认"> 修改建议：<改法描述，代码必须使用行内代码 `改动示例` 格式，禁止只写文字>
  - 行号不在速查表中时（如需引用被删除行），右表中 [DEL→L<N>] 的 N 作为挂靠行号，并标注"(被删除行的挂靠行)"；行号确实无法确定时写"需人工确认"

审查目标：{owner}/{repo} PR #{number}

本地仓同步状态：{repo_sync_status}
本地仓路径：{local_repo_path}
本地仓当前分支：{target_branch}（即 PR 目标分支，代码上下文基于此分支）
复审模式：{incremental_summary}
为避免"只看 diff 的盲区"，请在必要时先搜索本地代码上下文（调用链、实现处、同名符号）再下结论。

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


def cleanup_old_cache(cache_dir: Path, max_age_days: int) -> int:
    """删除超过 max_age_days 天未修改的缓存文件，返回删除数量。"""
    if max_age_days <= 0 or not cache_dir.exists():
        return 0
    cutoff = datetime.now() - timedelta(days=max_age_days)
    removed = 0
    for f in cache_dir.glob("*.json"):
        if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
            f.unlink()
            removed += 1
    return removed


def cleanup_old_repos(repo_root: Path, max_age_days: int) -> int:
    """删除超过 max_age_days 天未使用（按 FETCH_HEAD mtime 判断）的本地克隆，返回删除数量。"""
    if max_age_days <= 0 or not repo_root.exists():
        return 0
    cutoff = datetime.now() - timedelta(days=max_age_days)
    removed = 0
    # 结构为 repo_root/owner/repo/.git/FETCH_HEAD
    for fetch_head in repo_root.glob("*/*/.git/FETCH_HEAD"):
        if datetime.fromtimestamp(fetch_head.stat().st_mtime) < cutoff:
            repo_path = fetch_head.parent.parent
            shutil.rmtree(repo_path, ignore_errors=True)
            removed += 1
    return removed


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
        choices=["agent", "api", "local", "cursor"],  # cursor 保留为 agent 的兼容别名
        default="agent",
        help="agent=生成给任意 AI Agent 的任务草稿（默认）；api=单次请求云端模型；local=并发批次本地模型；cursor=同 agent（兼容旧用法）",
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
        cache_max_age_days = int(os.getenv("CACHE_MAX_AGE_DAYS", "90"))
        repo_root = Path(os.getenv("LOCAL_REPO_ROOT", "repos"))
        repo_max_age_days = int(os.getenv("REPO_MAX_AGE_DAYS", "0"))  # 0=不自动删除

        # 自动清理过期缓存和本地克隆
        removed_cache = cleanup_old_cache(cache_dir, cache_max_age_days)
        if removed_cache:
            print(f"[CLEANUP] 已清理 {removed_cache} 个过期缓存文件（>{cache_max_age_days}天）")
        removed_repos = cleanup_old_repos(repo_root, repo_max_age_days)
        if removed_repos:
            print(f"[CLEANUP] 已清理 {removed_repos} 个过期本地克隆（>{repo_max_age_days}天）")

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
        diff_excerpt, diff_coverage_text, compacted_diffs = build_diff_excerpt(
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
            compacted_diffs,
            max_lines_per_file=int(os.getenv("DIFF_LINE_REF_MAX_LINES", "300")),
        )

        if not enable_line_candidates:
            line_comment_candidates_text += "\n（候选文件默认不落盘；设置 ENABLE_LINE_CANDIDATES=true 可输出 line_candidates 文件）"

        if args.backend in ("api", "local"):
            llm_base = must_env("LLM_API_BASE")
            llm_key = os.getenv("LLM_API_KEY", "")   # 本地模型可以不需要 key
            model = must_env("LLM_MODEL")

            if args.backend == "local":
                batch_chars = int(os.getenv("LOCAL_BATCH_CHARS", "6000"))
                max_workers = int(os.getenv("LOCAL_MAX_WORKERS", "4"))
                request_timeout = int(os.getenv("LOCAL_REQUEST_TIMEOUT", "180"))
                markdown = llm_generate_local(
                    llm_base=llm_base,
                    llm_key=llm_key,
                    model=model,
                    owner=owner,
                    repo=repo,
                    number=number,
                    pr_data=pr_data,
                    compacted_diffs=compacted_diffs,
                    batch_chars=batch_chars,
                    max_workers=max_workers,
                    request_timeout=request_timeout,
                )
            else:
                api_timeout = int(os.getenv("LLM_REQUEST_TIMEOUT", "120"))
                markdown = llm_generate_markdown(
                    llm_base=llm_base,
                    llm_key=llm_key,
                    model=model,
                    owner=owner,
                    repo=repo,
                    number=number,
                    pr_data=pr_data,
                    diff_text=diff_excerpt,
                    timeout=api_timeout,
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
