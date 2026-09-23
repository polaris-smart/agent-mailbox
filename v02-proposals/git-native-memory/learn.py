#!/usr/bin/env python3
"""agent-mailbox v0.2 · Git-Native Memory 三件套原型（纯标准库，零重依赖）

三件套：
  1. learnings/  — Git 仓库式经验条目（markdown + frontmatter）
  2. recall      — BM25 检索（英文词 + 中文 bigram 分词，无需 jieba）
  3. 投票飞轮    — votes/<agent-id>.yaml 投票，检索排序随投票聚合变化

用法：
  python3 learn.py add --agent ZC --signal "PUT grants 外层字段被静默忽略" \
      --solution "必须写 inner grant 对象，写完读回验证" --tags dt,api
  python3 learn.py recall "grant 写入"
  python3 learn.py vote <entry-id> --agent HS --up
  python3 learn.py list
  python3 learn.py rebuild-index
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("LEARNINGS_ROOT", Path.home() / ".agent-mail" / "learnings"))
ENTRIES = ROOT / "entries"
VOTES = ROOT / "votes"
INDEX = ROOT / "index.json"


# ---------------------------------------------------------------- tokenizer

_CJK = re.compile(r"[\u4e00-\u9fff]+")
_WORD = re.compile(r"[a-zA-Z0-9_]+")


def tokenize(text: str) -> list[str]:
    """English words as-is; CJK runs as overlapping bigrams (+unigram fallback).

    Why bigrams: Chinese has no spaces. Bigram indexing ("符号链接" ->
    符号/号链/链接) makes mid-word queries work ("链接" hits), which naive
    whole-run tokenization (okf-agent-memory's current behaviour) cannot do.
    """
    text = text.lower()
    tokens: list[str] = []
    tokens.extend(_WORD.findall(text))
    for run in _CJK.findall(text):
        if len(run) == 1:
            tokens.append(run)
            continue
        for i in range(len(run) - 1):
            tokens.append(run[i : i + 2])
    return tokens


# ---------------------------------------------------------------- storage

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    fm: dict = {}
    for line in m.group(1).split("\n"):
        mm = re.match(r"^([a-z_]+):\s*(.*)$", line)
        if mm:
            fm[mm.group(1)] = mm.group(2).strip()
    return fm, m.group(2)


def load_entries() -> list[dict]:
    out = []
    if not ENTRIES.is_dir():
        return out
    for p in sorted(ENTRIES.glob("*.md")):
        fm, body = _parse_frontmatter(p.read_text(encoding="utf-8"))
        out.append({"id": p.stem, "path": p, "fm": fm, "body": body.strip()})
    return out


def load_votes() -> dict[str, int]:
    """Aggregate votes/<agent>.yaml: each line `<entry-id>: +1|-1`."""
    agg: dict[str, int] = {}
    if not VOTES.is_dir():
        return agg
    for p in sorted(VOTES.glob("*.yaml")):
        for line in p.read_text(encoding="utf-8").splitlines():
            mm = re.match(r"^([\w.-]+):\s*([+-]?\d+)\s*$", line.strip())
            if mm:
                eid, v = mm.group(1), int(mm.group(2))
                agg[eid] = agg.get(eid, 0) + v
    return agg


# ---------------------------------------------------------------- BM25

def build_index() -> dict:
    entries = load_entries()
    docs: dict[str, list[str]] = {}
    df: dict[str, int] = {}
    for e in entries:
        text = " ".join(
            [
                e["fm"].get("title", ""),
                e["fm"].get("signal", ""),
                e["fm"].get("solution", ""),
                e["fm"].get("tags", ""),
                e["body"],
            ]
        )
        toks = tokenize(text)
        docs[e["id"]] = toks
        for t in set(toks):
            df[t] = df.get(t, 0) + 1
    avgdl = sum(len(t) for t in docs.values()) / max(1, len(docs))
    payload = {"docs": docs, "df": df, "avgdl": avgdl, "n": len(docs), "built_at": time.time()}
    ROOT.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


def load_index() -> dict:
    if INDEX.is_file():
        return json.loads(INDEX.read_text(encoding="utf-8"))
    return build_index()


def bm25(index: dict, query: str, k1: float = 1.5, b: float = 0.75) -> dict[str, float]:
    q = tokenize(query)
    scores: dict[str, float] = {}
    n, avgdl, df, docs = index["n"], index["avgdl"], index["df"], index["docs"]
    for eid, toks in docs.items():
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        s = 0.0
        dl = len(toks) or 1
        for term in q:
            if term not in tf:
                continue
            idf = math.log(1 + (n - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
            s += idf * (tf[term] * (k1 + 1)) / (tf[term] + k1 * (1 - b + b * dl / avgdl))
        if s > 0:
            scores[eid] = s
    return scores


# ---------------------------------------------------------------- commands

def cmd_add(args) -> None:
    ENTRIES.mkdir(parents=True, exist_ok=True)
    eid = args.id or re.sub(r"[^a-z0-9-]+", "-", args.signal.lower())[:40].strip("-")
    eid = f"{time.strftime('%Y%m%d')}-{eid}"
    path = ENTRIES / f"{eid}.md"
    if path.exists():
        print(f"entry exists: {eid}", file=sys.stderr)
        sys.exit(1)
    tags = ",".join(args.tags.split(",")) if args.tags else ""
    path.write_text(
        "---\n"
        f"title: {args.signal}\n"
        f"agent: {args.agent}\n"
        f"date: {time.strftime('%Y-%m-%d')}\n"
        f"signal: {args.signal}\n"
        f"solution: {args.solution}\n"
        f"tags: {tags}\n"
        "---\n\n"
        f"## 摩擦信号\n\n{args.signal}\n\n## 解决方案\n\n{args.solution}\n",
        encoding="utf-8",
    )
    build_index()
    print(f"added: {eid}")


def cmd_recall(args) -> None:
    index = load_index()
    scores = bm25(index, args.query)
    votes = load_votes()
    entries = {e["id"]: e for e in load_entries()}
    # Vote flywheel: score * (1 + 0.1 * net_votes), floored at 0.1x
    ranked = sorted(
        ((eid, s * max(0.1, 1 + 0.1 * votes.get(eid, 0))) for eid, s in scores.items()),
        key=lambda kv: -kv[1],
    )[: args.limit]
    if args.json:
        print(json.dumps(
            [
                {
                    "id": eid,
                    "score": round(s, 4),
                    "votes": votes.get(eid, 0),
                    "title": entries[eid]["fm"].get("title", ""),
                }
                for eid, s in ranked
            ],
            ensure_ascii=False,
            indent=2,
        ))
        return
    if not ranked:
        print(f"no match: {args.query!r}")
        return
    for eid, s in ranked:
        e = entries[eid]
        print(f"[{s:.2f} | votes {votes.get(eid, 0):+d}] {eid}")
        print(f"    {e['fm'].get('title', '')}")
        print(f"    {e['fm'].get('solution', '')[:100]}")


def cmd_vote(args) -> None:
    VOTES.mkdir(parents=True, exist_ok=True)
    p = VOTES / f"{args.agent}.yaml"
    lines: list[str] = []
    if p.is_file():
        lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    delta = "+1" if args.up else "-1"
    # Replace existing vote for this entry
    lines = [ln for ln in lines if not ln.startswith(f"{args.entry}:")]
    lines.append(f"{args.entry}: {delta}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"voted: {args.entry} {delta} by {args.agent}")


def cmd_list(args) -> None:
    votes = load_votes()
    for e in load_entries():
        v = votes.get(e["id"], 0)
        print(f"{e['id']}  [votes {v:+d}]  {e['fm'].get('agent', '?')}  {e['fm'].get('title', '')}")


def cmd_rebuild(args) -> None:
    idx = build_index()
    print(f"index rebuilt: {idx['n']} entries, {len(idx['df'])} unique terms")


def main() -> None:
    ap = argparse.ArgumentParser(prog="learn", description="Git-Native Memory prototype (learnings/recall/votes)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="add a learning entry")
    p.add_argument("--agent", required=True)
    p.add_argument("--signal", required=True, help="friction signal observed")
    p.add_argument("--solution", required=True)
    p.add_argument("--tags", default="")
    p.add_argument("--id", default=None)
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("recall", help="BM25 recall (CJK bigram aware)")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_recall)

    p = sub.add_parser("vote", help="upvote/downvote an entry")
    p.add_argument("entry")
    p.add_argument("--agent", required=True)
    p.add_argument("--up", action="store_true")
    p.add_argument("--down", action="store_true")
    p.set_defaults(fn=cmd_vote)

    p = sub.add_parser("list", help="list all entries")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("rebuild-index", help="rebuild the BM25 index")
    p.set_defaults(fn=cmd_rebuild)

    args = ap.parse_args()
    if args.cmd == "vote" and not (args.up ^ args.down):
        ap.error("vote needs exactly one of --up/--down")
    args.fn(args)


if __name__ == "__main__":
    main()
