"""轻量可解释词法检索与上下文包生成。

特征权重常量：完整短语标题命中 12，标题 token 4，标签 token 4，正文 token 1；
按唯一特征计分，并以 ``sqrt(token 数)`` 做长度归一，避免长查询/重复堆词无限加分。
排序：相关性 → 作用范围具体程度 → ID（确定性）。
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from .schema import STATUSES, ParsedEntry
from .store import (
    Paths,
    StoreSnapshot,
    compute_conflicts,
    entry_summary,
    is_current,
    review_due,
    scope_applies,
    scope_specificity,
    status_label,
    validity_state,
)

TITLE_PHRASE_WEIGHT = 12
TITLE_TOKEN_WEIGHT = 4
TAG_TOKEN_WEIGHT = 4
BODY_TOKEN_WEIGHT = 1

_ASCII_WORD_RE = re.compile(r"[a-z0-9]+")
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")


@dataclass(frozen=True)
class QuerySpec:
    at: datetime
    cwd: Path
    scenes: tuple[str, ...] = ()
    task_id: str | None = None
    history: bool = False


def normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def tokenize(normalized_query: str) -> list[str]:
    """英文/数字词 + 连续中文双字片段；极短中文查询保留单字退路。"""

    tokens: list[str] = []
    seen: set[str] = set()

    def push(token: str) -> None:
        if token and token not in seen:
            seen.add(token)
            tokens.append(token)

    for word in _ASCII_WORD_RE.findall(normalized_query):
        push(word)
    for run in _CJK_RUN_RE.findall(normalized_query):
        if len(run) == 1:
            push(run)
        else:
            for index in range(len(run) - 1):
                push(run[index : index + 2])
    return tokens


def normalize_query(query: str) -> str:
    return normalize_text(query).strip()


def _phrase(normalized_query: str) -> str:
    return " ".join(normalized_query.split())


def _body_without_h1(body: str) -> str:
    lines = body.split("\n")
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if re.match(r"^#\s+", stripped):
            remaining = lines[index + 1 :]
            return "\n".join(remaining).lstrip("\n")
        return body
    return body


def score_entry(entry: ParsedEntry, normalized_query: str) -> dict[str, Any] | None:
    tokens = tokenize(normalized_query)
    if not tokens:
        return None
    title = normalize_text(entry.title)
    tags = [normalize_text(tag) for tag in entry.metadata["tags"]]
    body = normalize_text(entry.body)

    matched: dict[str, Any] = {"title_phrase": False, "title_tokens": [], "tag_tokens": [], "body_tokens": []}
    base = 0
    phrase = _phrase(normalized_query)
    if len(phrase) >= 2 and phrase in title:
        base += TITLE_PHRASE_WEIGHT
        matched["title_phrase"] = True
    title_hits = [token for token in tokens if token in title]
    if title_hits:
        base += TITLE_TOKEN_WEIGHT * len(title_hits)
        matched["title_tokens"] = title_hits
    tag_hits = [token for token in tokens if any(token in tag for tag in tags)]
    if tag_hits:
        base += TAG_TOKEN_WEIGHT * len(tag_hits)
        matched["tag_tokens"] = tag_hits
    body_hits = [token for token in tokens if token in body]
    if body_hits:
        base += BODY_TOKEN_WEIGHT * len(body_hits)
        matched["body_tokens"] = body_hits
    if base <= 0:
        return None
    score = round(base / math.sqrt(len(tokens)), 6)
    return {"score": score, "matched": matched, "base": base}


def _search_sort_key(entry: ParsedEntry, score: float) -> tuple[Any, ...]:
    rank, depth = scope_specificity(entry)
    return (-score, -rank, -depth, entry.id)


def _scope_sort_key(entry: ParsedEntry) -> tuple[Any, ...]:
    rank, depth = scope_specificity(entry)
    return (-rank, -depth, entry.id)


def select_applicable(paths: Paths, snapshot: StoreSnapshot, spec: QuerySpec) -> list[ParsedEntry]:
    selected: list[ParsedEntry] = []
    for entry in snapshot.sorted_entries():
        if not spec.history:
            if not is_current(entry, spec.at):
                continue
        if entry.status not in STATUSES:
            continue
        if not scope_applies(paths, entry, cwd=spec.cwd, scenes=spec.scenes, task_id=spec.task_id):
            continue
        selected.append(entry)
    return selected


def result_payload(
    paths: Paths,
    entry: ParsedEntry,
    at: datetime,
    *,
    score: float | None = None,
    matched: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = entry_summary(paths, entry, at)
    if score is not None:
        payload["score"] = score
        payload["matched"] = matched or {}
    return payload


def search(
    paths: Paths,
    snapshot: StoreSnapshot,
    spec: QuerySpec,
    *,
    normalized_query: str,
    limit: int,
) -> dict[str, Any]:
    applicable = select_applicable(paths, snapshot, spec)
    conflicts = compute_conflicts(paths, applicable)
    tokens = tokenize(normalized_query)
    scored: list[tuple[ParsedEntry, dict[str, Any]]] = []
    if tokens:
        for entry in applicable:
            result = score_entry(entry, normalized_query)
            if result is not None:
                scored.append((entry, result))
    scored.sort(key=lambda pair: _search_sort_key(pair[0], pair[1]["score"]))
    total = len(scored)
    selected = scored[:limit]
    items = [
        result_payload(paths, entry, spec.at, score=result["score"], matched=result["matched"])
        for entry, result in selected
    ]
    return {
        "query": normalized_query,
        "tokens": tokens,
        "total_matched": total,
        "returned": len(items),
        "limit": limit,
        "items": items,
        "conflicts": conflicts,
        "history": spec.history,
    }


def _render_meta_lines(paths: Paths, entry: ParsedEntry, at: datetime) -> list[str]:
    metadata = entry.metadata
    scope = entry.scope
    scenes = ",".join(scope["scenes"]) if scope["scenes"] else "通用"
    lines = [
        f"- ID: `{entry.id}` · kind: {entry.kind} · status: {entry.status} · revision: {entry.revision}",
        f"- 状态说明: {_label(entry, at)}",
        f"- 作用范围: {scope['level']}:{scope['target']} · 场景: {scenes}"
        + (f" · task_id: {scope['task_id']}" if scope["task_id"] else ""),
        f"- 权威路径: {entry.origin} · sha256: {entry.sha256}",
        f"- 有效区间: [{metadata['valid_from'] or '未设起点'}, {metadata['valid_until'] or '未设终点'})"
        + (f" · review_after: {metadata['review_after']}" if metadata["review_after"] else ""),
    ]
    sources = "；".join(
        f"{source['kind']} @ {source['locator']}" for source in metadata["sources"]
    )
    lines.append(f"- 来源: {sources}")
    return lines


def _label(entry: ParsedEntry, at: datetime) -> str:
    from .store import status_label

    return status_label(entry, at)


def render_entry_text(paths: Paths, entry: ParsedEntry, at: datetime) -> str:
    lines = [f"## {entry.title}"]
    lines.extend(_render_meta_lines(paths, entry, at))
    if review_due(entry, at):
        lines.append("- 复核提示: 已到 review_after，仍可返回；执行前请按需重新核实。")
    state = validity_state(entry, at)
    if state["expired"]:
        lines.append("- 时间说明: 已过 valid_until，不属于当前有效结果。")
    if entry.kind == "episode":
        lines.append("- 历史经历: 这是过去发生的事，不是当前持续状态。")
    lines.append("")
    lines.append(_body_without_h1(entry.body))
    return "\n".join(lines)


def render_stub_text(paths: Paths, entry: ParsedEntry, at: datetime, *, reason: str) -> str:
    lines = [f"## {entry.title}"]
    lines.extend(_render_meta_lines(paths, entry, at))
    lines.append(f"- {reason}")
    return "\n".join(lines)


def context_payload(paths: Paths, entry: ParsedEntry, at: datetime, *, body_omitted: bool, rendered_chars: int) -> dict[str, Any]:
    payload = result_payload(paths, entry, at)
    payload["body"] = None if body_omitted else entry.body
    payload["body_omitted"] = body_omitted
    payload["rendered_chars"] = rendered_chars
    return payload


def build_context(
    paths: Paths,
    snapshot: StoreSnapshot,
    spec: QuerySpec,
    *,
    normalized_query: str,
    max_chars: int,
    limit: int,
) -> dict[str, Any]:
    applicable = select_applicable(paths, snapshot, spec)
    conflicts = compute_conflicts(paths, applicable)
    pinned = sorted((entry for entry in applicable if entry.metadata["pinned"]), key=_scope_sort_key)
    ordered: list[ParsedEntry] = list(pinned)
    seen = {entry.id for entry in pinned}
    tokens: list[str] = []
    scores: dict[str, dict[str, Any]] = {}
    if normalized_query:
        tokens = tokenize(normalized_query)
        if tokens:
            for entry in applicable:
                scored = score_entry(entry, normalized_query)
                if scored is not None:
                    scores[entry.id] = scored
            relevant = sorted(
                (entry for entry in applicable if entry.id in scores),
                key=lambda entry: _search_sort_key(entry, scores[entry.id]["score"]),
            )
            for entry in relevant:
                if entry.id not in seen:
                    seen.add(entry.id)
                    ordered.append(entry)

    items: list[dict[str, Any]] = []
    used = 0
    omitted = 0
    omitted_ids: list[str] = []
    for entry in ordered:
        if len(items) >= limit:
            omitted += 1
            omitted_ids.append(entry.id)
            continue
        full_text = render_entry_text(paths, entry, spec.at)
        rendered_chars = len(full_text)
        if used + rendered_chars <= max_chars:
            used += rendered_chars
            items.append(context_payload(paths, entry, spec.at, body_omitted=False, rendered_chars=rendered_chars))
            continue
        stub_reason = "正文超出本次上下文预算，未展开；请用 `show {id}` 读取完整条目。".format(id=entry.id)
        stub_text = render_stub_text(paths, entry, spec.at, reason=stub_reason)
        stub_chars = len(stub_text)
        if used + stub_chars <= max_chars:
            used += stub_chars
            items.append(context_payload(paths, entry, spec.at, body_omitted=True, rendered_chars=stub_chars))
        else:
            omitted += 1
            omitted_ids.append(entry.id)

    markdown_parts: list[str] = []
    for item in items:
        entry = snapshot.entries[item["id"]]
        if item["body_omitted"]:
            reason = f"正文超出本次上下文预算，未展开；请用 `show {entry.id}` 读取完整条目。"
            markdown_parts.append(render_stub_text(paths, entry, spec.at, reason=reason))
        else:
            markdown_parts.append(render_entry_text(paths, entry, spec.at))
    if conflicts:
        ids = sorted({entry_id for group in conflicts for entry_id in group["ids"]})
        markdown_parts.append(
            "## 结构化冲突提示\n\n"
            + "本次适用范围内存在同 subject_key 的 active 冲突组，不能只采信其中一边。"
            + f" 相关 ID: {', '.join(ids)}。请定点 `show <id>` 对比来源与范围。"
        )
    if not items and ordered:
        markdown_parts.append(
            "## 上下文预算不足\n\n"
            f"本次预算 {max_chars} 字符不足以展示任何条目的完整元数据；"
            f"有 {len(ordered)} 项未展开。请提高 --max-chars 或用 `show <id>` 定点读取。"
        )
    if omitted_ids:
        markdown_parts.append(
            "## 未进入上下文的条目\n\n"
            f"因条数上限或预算未纳入 {len(omitted_ids)} 项；可用以下命令定点读取：\n"
            + "\n".join(f"- `show {entry_id}`" for entry_id in omitted_ids)
        )
    markdown = "\n\n".join(markdown_parts).strip() + "\n"
    more_commands = [f"show {item['id']}" for item in items if item["body_omitted"]]
    more_commands.extend(f"show {entry_id}" for entry_id in omitted_ids)
    return {
        "query": normalized_query,
        "tokens": tokens,
        "cwd": str(spec.cwd),
        "scenes": list(spec.scenes),
        "task_id": spec.task_id,
        "at": spec.at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "history": spec.history,
        "limit": limit,
        "max_chars": max_chars,
        "char_count": used,
        "omitted_count": omitted,
        "omitted_ids": omitted_ids,
        "total_applicable": len(applicable),
        "items": items,
        "conflicts": conflicts,
        "markdown": markdown,
        "more_commands": more_commands,
    }


__all__ = [
    "TITLE_PHRASE_WEIGHT",
    "TITLE_TOKEN_WEIGHT",
    "TAG_TOKEN_WEIGHT",
    "BODY_TOKEN_WEIGHT",
    "QuerySpec",
    "normalize_text",
    "normalize_query",
    "tokenize",
    "score_entry",
    "select_applicable",
    "result_payload",
    "search",
    "build_context",
    "render_entry_text",
]
