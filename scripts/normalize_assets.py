#!/usr/bin/env python3
"""SafeFusion 资产归一化脚本（T12，PRD §5）。

从旧 Node 版数据（node/data）与 cherry 文本分类语料（开发/cherry文本分类/data）
抽取并归一化为统一数据布局：

    data/
    ├── keywords/<类别>.csv          # 词库：(word, category, source)
    ├── corpus/black.csv             # 违规语料：(text, label, category, source)
    ├── corpus/white.csv             # 安全语料：(text, label, category, source)
    ├── vectors/import_manifest.jsonl         # 文本导入清单：[{pool,text,category,source}]
    └── vectors/import_manifest_images.jsonl  # 图片导入清单：[{pool,text,image_path,source}]

约定：
- 图片语料（--images-dir，可多次指定）：按目录名 black/white 自动归属池（就近优先），
  或用 --image-pool 显式指定；Pillow 校验（可读 + verify + 完整解码）通过的图片以
  {pool,text:"",image_path,source:"images"} 逐行写入**独立** images 清单，不混入文本
  清单（向后兼容；图文合并由主模型集成时决定）；重复路径去重、损坏文件跳过并列入报告。
- 仅做文件级迁移，不做分词；node/data/keywords 目录下有什么就归一什么
  （.txt 每行一词；.csv 取「关键词/类型」列）。
- 语料池：cherry 13 类中「正常」→ white（label=0），其余类别 → black（label=1）；
  node 白.csv → white，违规语句（向量化查询）.csv → black；池内按 text 去重（首见保留）。
- 编码探测顺序 utf-8-sig → gbk → utf-8；产物统一写 utf-8-sig（兼容 Windows Excel）。
- 幂等：每次覆盖写；--dry-run 只统计不写盘。
- 真实数据不进入 git：产物写到 --out 指定目录（默认 ./data，已被 .gitignore 排除）。
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

logger = logging.getLogger("normalize_assets")

OUTPUT_ENCODING = "utf-8-sig"
MANIFEST_ENCODING = "utf-8"
ENCODING_CANDIDATES = ("utf-8-sig", "gbk", "utf-8")
COMMENT_PREFIXES = ("#", "//")
NORMAL_CATEGORY = "正常"
KEYWORD_COL = "关键词"
TYPE_COL = "类型"

#: 图片语料可接受的扩展名（大小写不敏感）
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
#: 图片独立导入清单文件名（与文本 import_manifest.jsonl 分离，向后兼容）
IMAGES_MANIFEST_FILE = "import_manifest_images.jsonl"

DEFAULT_NODE_DATA = r"E:\语义+关键词检索违规匹配\node\data"
DEFAULT_CHERRY_DATA = "开发/cherry文本分类/data"
DEFAULT_OUT = "./data"


@dataclass
class KeywordRow:
    """归一化词库条目（词 / 类别 / 来源）。"""

    word: str
    category: str
    source: str


@dataclass
class CorpusRow:
    """归一化语料条目（入库池由 label 语义决定：正常→white，其余→black）。"""

    text: str
    category: str
    source: str


@dataclass
class FileInfo:
    """输入文件统计（路径 / 探测到的编码 / 数据行数）。"""

    path: str
    encoding: str
    raw_rows: int


@dataclass
class CorpusPoolStats:
    """单个语料池的合并去重统计。"""

    raw_rows: int = 0
    unique_rows: int = 0
    dropped_rows: int = 0
    sources: dict[str, int] = field(default_factory=dict)


@dataclass
class ImageStats:
    """图片语料扫描统计（候选 / 去重 / 校验通过 / 跳过）。"""

    scanned: int = 0
    unique: int = 0
    valid: int = 0
    deduped: int = 0
    invalid: int = 0
    unclassified: int = 0
    pools: dict[str, int] = field(default_factory=lambda: {"black": 0, "white": 0})
    invalid_files: list[tuple[str, str]] = field(default_factory=list)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数（全部路径参数化，不硬编码输出位置）。"""
    parser = argparse.ArgumentParser(
        prog="normalize_assets",
        description="SafeFusion 资产归一化：node/data 与 cherry 语料 → PRD §5 统一布局。",
    )
    parser.add_argument(
        "--node-data",
        default=DEFAULT_NODE_DATA,
        help=f"旧 Node 版数据目录（白.csv / 违规语句 CSV / keywords/），默认 {DEFAULT_NODE_DATA}",
    )
    parser.add_argument(
        "--cherry-data",
        default=DEFAULT_CHERRY_DATA,
        help="cherry 文本分类语料目录（13 类 CSV 位于其 分类/ 子目录），默认相对仓库根",
    )
    parser.add_argument(
        "--out",
        default=DEFAULT_OUT,
        help=f"归一化产物输出目录，默认 {DEFAULT_OUT}（已 gitignore）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只统计并打印报告，不写任何文件")
    parser.add_argument(
        "--max-rows",
        type=int,
        default=0,
        help="向量库导入清单最大行数上限（0=全量；设置后黑白两池轮流取样保持均衡）",
    )
    parser.add_argument(
        "--images-dir",
        action="append",
        metavar="DIR",
        default=None,
        help="递归扫描的图片目录（可多次指定）。归属池：--image-pool 显式指定；"
        "缺省按文件所在目录名自动归属（路径中含 black→black、含 white→white，就近优先；"
        "均不含则跳过并计入报告）。产物写入独立 data/vectors/import_manifest_images.jsonl，"
        "不混入文本清单",
    )
    parser.add_argument(
        "--image-pool",
        choices=("black", "white"),
        default=None,
        help="图片归属池（black | white）；缺省时按 --images-dir 目录名自动归属",
    )
    return parser.parse_args(argv)


def detect_encoding(path: Path) -> str:
    """按 utf-8-sig → gbk → utf-8 依次尝试，返回首个可完整解码的编码名。"""
    raw = path.read_bytes()
    for enc in ENCODING_CANDIDATES:
        try:
            raw.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    raise ValueError(f"无法识别的文件编码: {path}")


def read_csv(path: Path, encoding: str) -> list[list[str]]:
    """读取 CSV 全部非空行（含表头）。"""
    with open(path, encoding=encoding, newline="") as fh:
        return [row for row in csv.reader(fh) if any(cell.strip() for cell in row)]


def load_keyword_rows(node_data: Path, files: list[FileInfo]) -> list[KeywordRow]:
    """迁移 node/data/keywords 下所有词库文件（txt 每行一词；csv 取关键词/类型列）。"""
    kdir = node_data / "keywords"
    if not kdir.is_dir():
        logger.warning("关键词目录不存在，跳过词库迁移: %s", kdir)
        return []
    rows: list[KeywordRow] = []
    for path in sorted(kdir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in (".txt", ".csv"):
            continue
        enc = detect_encoding(path)
        source = f"node:{path.relative_to(node_data).as_posix()}"
        if path.suffix.lower() == ".csv":
            table = read_csv(path, enc)
            files.append(FileInfo(str(path), enc, max(0, len(table) - 1)))
            if not table:
                continue
            header = table[0]
            if KEYWORD_COL not in header:
                logger.warning("跳过无『%s』列的词库 CSV: %s", KEYWORD_COL, path)
                continue
            word_col = header.index(KEYWORD_COL)
            cat_col = header.index(TYPE_COL) if TYPE_COL in header else None
            for row in table[1:]:
                if len(row) <= word_col or not row[word_col].strip():
                    continue
                word = row[word_col].strip()
                category = (
                    row[cat_col].strip()
                    if cat_col is not None and len(row) > cat_col and row[cat_col].strip()
                    else path.stem
                )
                rows.append(KeywordRow(word=word, category=category, source=source))
        else:
            lines = path.read_text(encoding=enc).splitlines()
            words = [
                ln.strip()
                for ln in lines
                if ln.strip() and not ln.lstrip().startswith(COMMENT_PREFIXES)
            ]
            files.append(FileInfo(str(path), enc, len(words)))
            for word in words:
                rows.append(KeywordRow(word=word, category=path.stem, source=source))
    return rows


def _load_text_col_csv(
    path: Path,
    files: list[FileInfo],
    text_col_name: str,
    category_col_name: str | None,
    source: str,
) -> list[CorpusRow]:
    """通用 CSV 语料加载：按表头定位文本/类别列，产出 CorpusRow。"""
    enc = detect_encoding(path)
    table = read_csv(path, enc)
    header = table[0] if table else []
    if not table or text_col_name not in header:
        logger.warning("表头缺少『%s』列，跳过: %s", text_col_name, path)
        return []
    text_col = header.index(text_col_name)
    cat_col = header.index(category_col_name) if category_col_name in header else None
    files.append(FileInfo(str(path), enc, len(table) - 1))
    rows: list[CorpusRow] = []
    for row in table[1:]:
        if len(row) <= text_col or not row[text_col].strip():
            continue
        category = (
            row[cat_col].strip()
            if cat_col is not None and len(row) > cat_col and row[cat_col].strip()
            else ""
        )
        rows.append(CorpusRow(text=row[text_col].strip(), category=category, source=source))
    return rows


def load_node_white(node_data: Path, files: list[FileInfo]) -> list[CorpusRow]:
    """迁移 node/data/白.csv（编号,内容 → white 池候选）。"""
    path = node_data / "白.csv"
    if not path.is_file():
        logger.warning("node 白名单语料缺失: %s", path)
        return []
    return _load_text_col_csv(path, files, "内容", None, "node:白.csv")


def load_node_black(node_data: Path, files: list[FileInfo]) -> list[CorpusRow]:
    """迁移 node/data/违规语句（向量化查询）.csv（id,类别,文本 → black 池候选）。"""
    path = node_data / "违规语句（向量化查询）.csv"
    if not path.is_file():
        logger.warning("node 违规语料缺失: %s", path)
        return []
    return _load_text_col_csv(path, files, "文本", "类别", "node:违规语句（向量化查询）.csv")


def load_cherry_rows(cherry_data: Path, files: list[FileInfo]) -> list[CorpusRow]:
    """迁移 cherry 13 类语料：优先 cherry-data/分类/ 下 CSV，回退 cherry-data 根目录。"""
    candidates: list[Path] = []
    for base in (cherry_data / "分类", cherry_data):
        if base.is_dir():
            found = sorted(p for p in base.glob("*.csv") if p.is_file())
            if found:
                candidates = found
                break
    if not candidates:
        logger.warning("cherry 语料目录下未发现 CSV: %s", cherry_data)
        return []
    rows: list[CorpusRow] = []
    for path in candidates:
        rows.extend(_load_text_col_csv(path, files, "文本", "类别", "cherry"))
    return rows


def build_pool(rows: list[CorpusRow]) -> tuple[list[CorpusRow], CorpusPoolStats]:
    """池内按 text 去重（首见保留），返回唯一行与统计。"""
    stats = CorpusPoolStats(raw_rows=len(rows))
    for row in rows:
        stats.sources[row.source] = stats.sources.get(row.source, 0) + 1
    seen: dict[str, CorpusRow] = {}
    for row in rows:
        if row.text in seen:
            stats.dropped_rows += 1
        else:
            seen[row.text] = row
    stats.unique_rows = len(seen)
    return list(seen.values()), stats


def group_keywords(rows: list[KeywordRow]) -> dict[str, list[KeywordRow]]:
    """按类别分组并在类别内按词去重（首见保留），返回 {类别: 词条列表}。"""
    grouped: dict[str, list[KeywordRow]] = {}
    seen: dict[str, set[str]] = {}
    for row in rows:
        bucket = seen.setdefault(row.category, set())
        if row.word in bucket:
            continue
        bucket.add(row.word)
        grouped.setdefault(row.category, []).append(row)
    return grouped


def build_manifest_rows(
    white: list[CorpusRow], black: list[CorpusRow], max_rows: int
) -> list[dict[str, str]]:
    """构造向量库导入清单：max_rows<=0 全量（黑池在前），否则黑白轮流取样保证均衡。"""

    def rec(pool: str, rows: list[CorpusRow]) -> list[dict[str, str]]:
        return [
            {"pool": pool, "text": row.text, "category": row.category, "source": row.source}
            for row in rows
        ]

    if max_rows <= 0:
        return rec("black", black) + rec("white", white)
    out: list[dict[str, str]] = []
    bi, wi = 0, 0
    while len(out) < max_rows and (bi < len(black) or wi < len(white)):
        if bi < len(black):
            row = black[bi]
            bi += 1
            out.append(
                {"pool": "black", "text": row.text, "category": row.category, "source": row.source}
            )
        if len(out) >= max_rows:
            break
        if wi < len(white):
            row = white[wi]
            wi += 1
            out.append(
                {"pool": "white", "text": row.text, "category": row.category, "source": row.source}
            )
    return out


def write_csv(path: Path, header: list[str], rows: Iterable[list[str]]) -> int:
    """覆盖写 CSV（utf-8-sig + 换行规范化），返回数据行数。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with open(path, "w", encoding=OUTPUT_ENCODING, newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def write_keyword_csvs(out_dir: Path, grouped: dict[str, list[KeywordRow]]) -> dict[str, int]:
    """按类别分写 data/keywords/<类别>.csv，返回 {类别: 词数}。"""
    counts: dict[str, int] = {}
    for category, items in sorted(grouped.items()):
        path = out_dir / "keywords" / f"{category}.csv"
        rows = (
            [item.word, item.category, item.source] for item in sorted(items, key=lambda x: x.word)
        )
        counts[category] = write_csv(path, ["word", "category", "source"], rows)
    return counts


def write_corpus_csvs(
    out_dir: Path, white: list[CorpusRow], black: list[CorpusRow]
) -> tuple[int, int]:
    """写 data/corpus/white.csv 与 black.csv，返回 (white 行数, black 行数)。"""
    header = ["text", "label", "category", "source"]
    white_rows = ([row.text, "0", row.category, row.source] for row in white)
    black_rows = ([row.text, "1", row.category, row.source] for row in black)
    w = write_csv(out_dir / "corpus" / "white.csv", header, white_rows)
    b = write_csv(out_dir / "corpus" / "black.csv", header, black_rows)
    return w, b


def write_manifest(out_dir: Path, rows: list[dict[str, str]]) -> None:
    """写 data/vectors/import_manifest.jsonl（每行一个 JSON 对象）。"""
    path = out_dir / "vectors" / "import_manifest.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding=MANIFEST_ENCODING, newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def scan_image_files(root: Path) -> list[Path]:
    """递归扫描目录下全部图片扩展名文件（确定性排序，去重前）。"""
    return sorted(
        (p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS),
        key=lambda p: str(p).lower(),
    )


def detect_pool(file_path: Path, explicit: str | None) -> str:
    """确定图片归属池：``--image-pool`` 显式值优先，否则按文件目录名就近推断。

    目录判断取文件绝对父目录链，从离文件最近的一级开始；任一段含 ``black`` → black、
    含 ``white`` → white；两者都不含则抛 ValueError。

    Args:
        file_path: 图片文件路径。
        explicit: ``--image-pool`` 参数值（black/white），None 表示未显式指定。

    Raises:
        ValueError: 无法从目录链推断归属池且未显式指定。
    """
    if explicit:
        return explicit
    for part in reversed(file_path.parent.parts):
        low = part.lower()
        if "black" in low:
            return "black"
        if "white" in low:
            return "white"
    raise ValueError(
        f"无法从目录名推断归属池: {file_path}（目录链需含 black 或 white，"
        "或用 --image-pool 显式指定）"
    )


def validate_image(path: Path) -> str | None:
    """校验图片文件可读且 Pillow 可完整解码；返回错误描述，有效返回 None。"""
    try:
        with Image.open(path) as img:
            img.verify()
        # verify() 只校验结构不解码像素，重新打开触发完整解码
        with Image.open(path) as img:
            img.load()
            _ = img.size
    except (OSError, ValueError) as exc:
        return str(exc)
    return None


def collect_image_rows(
    roots: list[Path], explicit_pool: str | None
) -> tuple[list[dict[str, str]], ImageStats]:
    """扫描并校验图片语料，产出图片清单行与统计。

    清单行结构：``{"pool", "text": "", "image_path", "source": "images"}``；
    ``image_path`` 为绝对路径（与本地消费方同一机器，词库/图片本体均不入库）。
    重复路径（resolve 后大小写归一）仅保留首个；损坏 / 无法推断归属池的图片跳过并记录。

    Args:
        roots: ``--images-dir`` 给出的目录（须为已存在目录）。
        explicit_pool: ``--image-pool`` 显式归属池，None 走目录名自动归属。

    Returns:
        (清单行列表, ImageStats)。

    Raises:
        ValueError: 任一目录不存在或不是目录。
    """
    rows: list[dict[str, str]] = []
    stats = ImageStats()
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            raise ValueError(f"--images-dir 不是目录: {root}")
        for path in scan_image_files(root):
            stats.scanned += 1
            key = str(path.resolve()).lower()
            if key in seen:
                stats.deduped += 1
                continue
            seen.add(key)
            stats.unique += 1
            try:
                pool = detect_pool(path, explicit_pool)
            except ValueError:
                stats.unclassified += 1
                logger.warning("跳过（无法推断归属池）: %s", path)
                continue
            err = validate_image(path)
            if err is not None:
                stats.invalid += 1
                stats.invalid_files.append((str(path), err))
                logger.warning("跳过损坏图片: %s（%s）", path, err)
                continue
            stats.valid += 1
            stats.pools[pool] += 1
            rows.append(
                {
                    "pool": pool,
                    "text": "",
                    "image_path": str(path.resolve()),
                    "source": "images",
                }
            )
    return rows, stats


def write_image_manifest(out_dir: Path, rows: list[dict[str, str]]) -> None:
    """写独立图片清单 data/vectors/import_manifest_images.jsonl（不覆盖文本清单）。"""
    path = out_dir / "vectors" / IMAGES_MANIFEST_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding=MANIFEST_ENCODING, newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def format_report(
    args: argparse.Namespace,
    files: list[FileInfo],
    keyword_raw: int,
    keyword_grouped: dict[str, list[KeywordRow]],
    white_stats: CorpusPoolStats,
    black_stats: CorpusPoolStats,
    conflicts: list[str],
    manifest_rows: list[dict[str, str]],
) -> str:
    """组装归一化统计报告（dry-run 与真实运行共用）。"""
    lines: list[str] = []
    lines.append("=" * 64)
    lines.append("SafeFusion 资产归一化统计")
    mode = "dry-run（仅统计，不写盘）" if args.dry_run else "完整运行（覆盖写盘）"
    lines.append(f"模式      : {mode}")
    lines.append(f"输出目录  : {args.out}")

    lines.append("")
    lines.append(f"[输入文件] 共 {len(files)} 个（编码 / 数据行数）")
    for info in files:
        lines.append(f"  {info.path}  |  {info.encoding}  |  {info.raw_rows} 行")

    kw_unique = sum(len(items) for items in keyword_grouped.values())
    lines.append("")
    lines.append(f"[词库] {args.out}/keywords/<类别>.csv")
    lines.append(
        f"  原始词条 {keyword_raw}，类别内去重后 {kw_unique}"
        f"（丢弃重复 {keyword_raw - kw_unique}，类别数 {len(keyword_grouped)}）"
    )
    for category in sorted(keyword_grouped):
        lines.append(f"  - {category}: {len(keyword_grouped[category])} 词")

    lines.append("")
    lines.append(f"[语料] {args.out}/corpus/*.csv")
    for pool_name, label, stats in (("white", 0, white_stats), ("black", 1, black_stats)):
        lines.append(
            f"  {pool_name} 池 (label={label}): 原始 {stats.raw_rows} → "
            f"去重后 {stats.unique_rows}（丢弃 {stats.dropped_rows}）"
        )
        for src, count in sorted(stats.sources.items()):
            lines.append(f"      {src}: {count} 条")
    if conflicts:
        lines.append(
            f"  [!] 黑白池文本冲突 {len(conflicts)} 条（同名文本同时落入黑白两池，两池均保留）"
        )
        for text in conflicts[:3]:
            snippet = text[:40] + ("…" if len(text) > 40 else "")
            lines.append(f"      - {snippet}")
        if len(conflicts) > 3:
            lines.append(f"      - …等共 {len(conflicts)} 条")

    black_n = sum(1 for row in manifest_rows if row["pool"] == "black")
    white_n = sum(1 for row in manifest_rows if row["pool"] == "white")
    lines.append("")
    lines.append(f"[向量清单] {args.out}/vectors/import_manifest.jsonl")
    cap_note = "" if args.max_rows <= 0 else f"（--max-rows={args.max_rows} 截断）"
    lines.append(f"  共 {len(manifest_rows)} 行（black {black_n} / white {white_n}）{cap_note}")
    lines.append("=" * 64)
    return "\n".join(lines)


def format_image_report(
    args: argparse.Namespace, stats: ImageStats, rows: list[dict[str, str]]
) -> str:
    """组装图片语料归一化统计报告（dry-run 与真实运行共用）。"""
    lines = ["", "=" * 64, "图片语料归一化统计"]
    mode = "dry-run（仅统计，不写盘）" if args.dry_run else "完整运行（覆盖写盘）"
    lines.append(f"模式      : {mode}")
    lines.append(f"扫描目录  : {', '.join(args.images_dir or [])}")
    lines.append(f"候选文件  : 扫描 {stats.scanned}（重复路径去重后 {stats.unique}）")
    lines.append(
        f"有效图片  : {stats.valid}（black {stats.pools['black']} / "
        f"white {stats.pools['white']}；损坏跳过 {stats.invalid}，"
        f"无法分类跳过 {stats.unclassified}）"
    )
    if stats.invalid_files:
        lines.append(f"[!] 损坏 / 无法打开的图片（跳过，共 {stats.invalid} 张）:")
        for path, reason in stats.invalid_files[:10]:
            lines.append(f"      - {path}: {reason}")
        if len(stats.invalid_files) > 10:
            lines.append(f"      - …等共 {len(stats.invalid_files)} 张")
    lines.append(
        f"清单      : {args.out}/vectors/{IMAGES_MANIFEST_FILE}（{len(rows)} 行，与文本清单分离）"
    )
    lines.append("=" * 64)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """归一化主流程：读取 → 统计 →（非 dry-run）覆盖写盘。"""
    # 报告可能含 GBK 之外的字符，统一 UTF-8 输出避免控制台编码崩溃
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr
    )
    args = parse_args(argv)
    node_data = Path(args.node_data)
    cherry_data = Path(args.cherry_data)
    if not cherry_data.is_absolute():
        cherry_data = Path.cwd() / cherry_data

    files: list[FileInfo] = []
    keyword_rows = load_keyword_rows(node_data, files)
    node_white = load_node_white(node_data, files)
    node_black = load_node_black(node_data, files)
    cherry_rows = load_cherry_rows(cherry_data, files)

    normal_rows = [row for row in cherry_rows if row.category == NORMAL_CATEGORY]
    violation_rows = [row for row in cherry_rows if row.category != NORMAL_CATEGORY]
    white, white_stats = build_pool(node_white + normal_rows)
    black, black_stats = build_pool(node_black + violation_rows)

    black_texts = {row.text for row in black}
    conflicts = sorted(black_texts & {row.text for row in white})

    keyword_grouped = group_keywords(keyword_rows)
    manifest_rows = build_manifest_rows(white, black, args.max_rows)

    image_rows: list[dict[str, str]] = []
    image_stats: ImageStats | None = None
    if args.images_dir:
        try:
            image_rows, image_stats = collect_image_rows(
                [Path(d) for d in args.images_dir], args.image_pool
            )
        except ValueError as exc:
            logger.error("%s", exc)
            return 1

    if not keyword_grouped and not white and not black and not image_rows:
        if args.images_dir:
            logger.error(
                "--images-dir 未产出任何有效图片行（扫描 %s 个文件，有效 %s 个），"
                "请检查图片目录与目录名 black/white",
                image_stats.scanned if image_stats else 0,
                len(image_rows),
            )
        else:
            logger.error("未发现任何可迁移的资产，请检查 --node-data 与 --cherry-data 路径")
        return 1

    print(
        format_report(
            args,
            files,
            len(keyword_rows),
            keyword_grouped,
            white_stats,
            black_stats,
            conflicts,
            manifest_rows,
        )
    )
    if image_stats is not None:
        print(format_image_report(args, image_stats, image_rows))
    if args.dry_run:
        logger.info("dry-run 完成，未写任何文件")
        return 0

    write_keyword_csvs(Path(args.out), keyword_grouped)
    write_corpus_csvs(Path(args.out), white, black)
    write_manifest(Path(args.out), manifest_rows)
    if image_rows:
        write_image_manifest(Path(args.out), image_rows)
    logger.info("产物已写入 %s", Path(args.out).resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
