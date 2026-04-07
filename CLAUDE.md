# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A single-file Python CLI tool that fetches GitCode PR diffs, syncs the local repo, and generates review draft files — either as Cursor Agent task prompts (`cursor` backend) or direct LLM review conclusions (`api` backend). It never auto-posts comments or modifies repository code.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env  # then fill in credentials
```

Python 环境由用户自行管理（系统 Python 或虚拟环境均可）。

## Running

```bash
# By PR URL (agent backend, default)
python review_draft.py --pr "https://gitcode.com/Owner/repo/pulls/123"

# By PR number
python review_draft.py --pr 123 --owner Owner --repo myrepo

# Direct LLM mode (requires LLM_API_BASE / LLM_API_KEY / LLM_MODEL in .env)
python review_draft.py --pr 123 --owner Owner --repo myrepo --backend api

# Local small model mode (concurrent batches)
python review_draft.py --pr 123 --owner Owner --repo myrepo --backend local
```

Output lands in `output/`:
- `review_task_<owner>_<repo>_pr<N>_<timestamp>.md` — agent backend (default)
- `review_<owner>_<repo>_pr<N>_<timestamp>.md` — API backend
- `line_candidates_<...>.md` — only when `ENABLE_LINE_CANDIDATES=true`

## Key Environment Variables (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `GITCODE_TOKEN` | — | GitCode PAT (required) |
| `GITCODE_API_BASE` | `https://api.gitcode.com/api/v5` | GitCode API root |
| `LOCAL_REPO_ROOT` | `repos` | Root dir for auto-cloned repos |
| `LOCAL_REPO_PATH` | — | Override: force specific local repo path |
| `REPO_REMOTE_URL` | — | Override: force specific remote URL |
| `LLM_API_BASE` / `LLM_API_KEY` / `LLM_MODEL` | — | Only for `--backend api` |
| `MAX_PATCH_CHARS` | `40000` | Total diff character budget |
| `MAX_FILES_IN_DIFF` | `25` | Files expanded in stage 1 |
| `MAX_HUNKS_PER_FILE` | `4` | Hunks per file in stage 1 |
| `MAX_CHARS_PER_FILE` | `5000` | Chars per file in stage 1 |
| `SECOND_PASS_FOCUS_FILES` | `6` | High-risk files re-expanded in stage 2 |
| `SECOND_PASS_HUNKS_PER_FILE` | `10` | Hunks per file in stage 2 |
| `SECOND_PASS_CHARS_PER_FILE` | `12000` | Chars per file in stage 2 |
| `ENABLE_LINE_CANDIDATES` | `false` | Output `line_candidates_*.md` |
| `INCREMENTAL_REVIEW` | `true` | Skip unchanged files via SHA256 cache |
| `CACHE_DIR` | `.review_cache` | Incremental review cache directory |

## Architecture

All logic is in `review_draft.py`. The pipeline is:

```
parse_pr_input()          # URL or number → (owner, repo, number)
fetch_pr_context()        # GitCode API: PR metadata + file diffs
sync_local_repo()         # git clone/fetch/reset for code context
build_diff_fingerprint()  # SHA256 per file for incremental tracking
filter_incremental_files()# compare to .review_cache/, skip unchanged
build_diff_excerpt()      # two-stage diff compaction (see below)
build_line_comment_candidates()  # precise new-file line number mapping
  → agent backend: build_cursor_task_markdown() → review_task_*.md
  → api backend:   llm_generate_markdown() → OpenAI-compatible call → review_*.md
  → local backend: llm_generate_local() → concurrent batches → review_*.md
save_review_state()       # persist fingerprints to .review_cache/
```

### Two-Stage Diff Compaction

For large PRs, `build_diff_excerpt()` uses a risk-based two-pass strategy to stay within `MAX_PATCH_CHARS`:

- **Stage 1**: Rank all changed files by `_file_priority()` (additions + deletions, +800 bonus for core/security/auth/service filenames, +200 for code file extensions). Expand top `MAX_FILES_IN_DIFF` files with `MAX_HUNKS_PER_FILE` hunks each.
- **Stage 2**: Re-expand the top `SECOND_PASS_FOCUS_FILES` highest-risk files with deeper budgets (`SECOND_PASS_HUNKS_PER_FILE`, `SECOND_PASS_CHARS_PER_FILE`), replacing their stage-1 entries.

### Line-Level Comment Positioning

`build_line_comment_candidates()` + `_extract_added_code_blocks()` parse unified diff hunks to build a lookup table mapping each added/deleted line to its new-file line number. Output format in prompts: `L<line_no> [+] <content>` for additions, `[DEL→L<next_line>] [-] <content>` for deletions.

## Cursor Skill Integration

`.cursor/skills/pr-review-auto/SKILL.md` defines the `pr-review-auto` skill. Trigger phrase: `review <pr-link>`. The skill runs `python review_draft.py`, reads the generated `review_task_*.md`, and outputs formatted review comments in Chinese with the template:

```
[文件:<path>] [行号:<start>-<end>] [严重级别:<高/中/低>] 【review】<title> <problem description> 修改建议：<suggestion with inline code>
```

Comments are sorted by severity (高/中/低), with a "总评草稿" (general assessment) appended.

## Claude Code Command

`.claude/commands/review.md` registers `/review` as a slash command. Usage:

```
/review https://gitcode.com/Owner/repo/pulls/123
```

It runs `python review_draft.py` and outputs the full structured review inline.

## GitCode APIs Used

- `GET /repos/{owner}/{repo}/pulls/{number}` — PR metadata
- `GET /repos/{owner}/{repo}/pulls/{number}/files` — file changes and diffs
