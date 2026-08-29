#!/usr/bin/env python3
"""SafeFusion 向量清单合并脚本（v0.2.2，群聊白语料并入）。

将 ``data/corpus/white_groupchat.csv``（text,label,category,source）并入
``data/vectors/import_manifest.jsonl``：池 = white，category/source 取 CSV 列，
按 (pool, text) 池内去重（首见保留，与 normalize_assets 语义一致）。

设计要点：
- 幂等：重复运行不产生重复行（基于 (pool, text) 去重）；
- 顺序：先读既有 manifest 全部行 → 追加 groupchat 行 → 池内去重 → 覆盖写回，
  id 序号在构建脚本里按完整清单重算，无需在此维护；
- 仅处理 groupchat 一份文件，不动其他语料来源（white/black 已在 manifest 中）；
- --dry-run 只统计不写盘。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="merge_manifest",
        description="把 white_groupchat.csv 并入向量导入清单（池内去重）。",
    )
    parser.add_argument(
        "--manifest",
        default="data/vectors/import_manifest.jsonl",
        help="目标清单（默认 data/vectors/import_manifest.jsonl）",
    )
    parser.add_argument(
        "--groupchat",
        default="data/corpus/white_groupchat.csv",
        help="群聊白语料 CSV（默认 data/corpus/white_groupchat.csv）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只统计不写盘",
    )
    return parser.parse_args(argv)


def read_manifest(path: Path) -> list[dict]:
    rows: list[dict] = []
    if path.is_file():
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    return rows


def read_groupchat(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            text = str(row.get("text") or "").strip()
            if not text:
                continue
            rows.append(
                {
                    "pool": "white",
                    "text": text,
                    "category": str(row.get("category") or ""),
                    "source": str(row.get("source") or "node:白群聊.csv"),
                }
            )
    return rows


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    manifest_path = Path(args.manifest)
    groupchat_path = Path(args.groupchat)

    existing = read_manifest(manifest_path)
    added = read_groupchat(groupchat_path)
    if not added:
        print(f"[错误] 群聊白语料为空或文件不存在: {groupchat_path}")
        return 1

    # 池内 (pool, text) 去重，首见保留
    seen: set[tuple[str, str]] = set()
    merged: list[dict] = []
    dup_skipped = 0
    for row in existing + added:
        key = (str(row["pool"]), str(row["text"]))
        if key in seen:
            dup_skipped += 1
            continue
        seen.add(key)
        merged.append(row)

    n_existing = len(existing)
    n_added = len(added)
    n_merged = len(merged)
    n_white = sum(1 for r in merged if r["pool"] == "white")
    n_black = sum(1 for r in merged if r["pool"] == "black")

    print("=" * 64)
    print("SafeFusion 清单合并 —— 群聊白语料并入")
    print(f"既有清单   : {n_existing} 行")
    print(f"群聊白新增 : {n_added} 行")
    print(f"去重跳过   : {dup_skipped} 行（(pool,text) 重复）")
    print(f"合并后     : {n_merged} 行（black {n_black} / white {n_white}）")
    print(f"目标文件   : {manifest_path}")
    if args.dry_run:
        print("[dry-run] 未写盘")
    else:
        with manifest_path.open("w", encoding="utf-8") as fh:
            for row in merged:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        print("[已写盘]")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
