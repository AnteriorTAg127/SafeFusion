#!/usr/bin/env python3
"""SafeFusion 向量库构建脚本（T15，PRD v0.2 M2）。

读取 ``data/vectors/import_manifest.jsonl``（T12 归一化脚本产出，每行
``{"pool","text","category","source"}``）→ 本地 Chinese-CLIP
（``LocalChineseCLIP.encode_texts``）批量编码 → 写入 ``NumpyVectorStore``
（black/white 双池，``--out`` 默认 ``data/vectors/``，持久化与 v0.1 §5.3
契约一致：每池 ``<pool>.npz`` 向量矩阵 + ``<pool>.meta.json`` 元数据）。

设计要点：

- **断点续跑（--resume，默认开）**：已编码 id 记录在 ``out/done_ids.json``，
  启动时按 id 集合跳过；该文件缺失时回退从 ``<pool>.meta.json`` 的 ``ids``
  字段恢复（防文件丢失）。编码 id 恒定为 ``{pool}:{seq}``（seq 为池内 1-based
  序号，基于**完整清单**次序计算，与 --max-rows 截断无关），保证续跑幂等。
  全量重建 = 清空 ``--out`` 目录（或另指定 --out）。
- **分批编码 + 分批持久化**：--batch 默认 64；每 ``_SAVE_EVERY_BATCHES`` 批
  （默认 50 批 ≈ 3200 行）执行一次 store.save + done_ids.json 写盘，防止掉电
  丢失过多已编码数据。
- **图像扩展点**：条目含 ``image_path`` / ``image_url`` 字段时走
  ``encode_images``（Pillow 读文件 / httpx 下载，get_embedding_backend 工厂
  返回的后端均实现该接口）；当前清单全部为文本条目，图像路径作为图文混入时
  的预留（文本标题字段此时不参与该行编码，如需融合见 fuse_vectors）。
- **失败降级**：整批编码失败退逐条编码，单条失败仅告警跳过（不拖垮任务，
  遗漏条目由下次续跑补）；清单单行解析失败 / 池名未知 / 空文本仅告警跳过。
- **依赖缺失提示**：torch/transformers 缺失或权重下载失败时输出
  ``uv sync --extra ml`` 与 HuggingFace 下载指引后退出，不裸抛 traceback。
- **--dry-run**：只读清单统计（含既有库概况），不编码、不写盘。

用法（一律用 .venv 解释器，禁 uv run）::

    .venv\\Scripts\\python.exe scripts\\build_vector_db.py --dry-run
    .venv\\Scripts\\python.exe scripts\\build_vector_db.py --batch 64 --device auto
    .venv\\Scripts\\python.exe scripts\\build_vector_db.py --max-rows 5   # 调试小样本
    .venv\\Scripts\\python.exe scripts\\build_vector_db.py --model <本地权重目录>
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from PIL import Image

#: 便于脱离 editable 安装直接运行（src 布局；正常 uv sync 后项目已装入 .venv）
_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from safefusion.engines.embedding import BaseEmbedding, get_embedding_backend  # noqa: E402
from safefusion.storage.vector_store import NumpyVectorStore, VectorItem  # noqa: E402

logger = logging.getLogger("build_vector_db")

#: 默认 CLIP 模型（与 config.py EmbeddingLocalConfig 默认值一致）
_DEFAULT_MODEL = "OFA-Sys/chinese-clip-vit-base-patch16"
#: 池名全集（与 vector_store.POOLS 一致）
_POOLS = ("black", "white")
#: 入库元数据中 text 字段最大保留长度
_TEXT_TRUNC = 200
#: 每 N 批执行一次持久化检查点（50 批 × batch 64 ≈ 3200 行，权衡掉电窗口与 IO）
_SAVE_EVERY_BATCHES = 50
#: 图像 URL 下载超时（秒）
_IMAGE_URL_TIMEOUT = 10.0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        prog="build_vector_db",
        description="SafeFusion 向量库构建：清单 → Chinese-CLIP 编码 → NumpyVectorStore。",
    )
    parser.add_argument(
        "--manifest",
        default="data/vectors/import_manifest.jsonl",
        help="导入清单 JSONL（每行 {pool,text,...}），默认 data/vectors/import_manifest.jsonl",
    )
    parser.add_argument(
        "--out",
        default="data/vectors/",
        help="向量库持久化目录（black/white 双池 npz+meta+done_ids.json），默认 data/vectors/",
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=64,
        help="单批编码行数（默认 64；GPU 显存不足可调小）",
    )
    parser.add_argument(
        "--device",
        default="auto",
        help="auto（有 GPU 用 GPU，默认）| cpu | cuda",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=0,
        help="仅处理清单前 N 行（0 = 全量，默认 0；调试用小样本）",
    )
    parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="断点续跑：跳过 done_ids.json / 既有库中已编码 id（默认开；--no-resume 从头重建）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只统计清单（含既有库概况），不编码、不写盘",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=f"覆盖 CLIP model_name（HF 模型名或本地权重目录），默认 {_DEFAULT_MODEL}",
    )
    return parser.parse_args(argv)


def is_image_row(row: dict[str, Any]) -> bool:
    """判断是否为图像条目：含 image_path / image_url 字段即视为图像。

    当前清单为纯文本条目（只有 ``text`` 字段），该分支作为图文混入库的预留扩展点。
    """
    return bool(row.get("image_path") or row.get("image_url"))


def read_manifest(path: Path) -> tuple[list[tuple[str, dict[str, Any]]], int]:
    """读取清单并为每行分配恒定 id（``{pool}:{seq}``，池内 1-based）。

    id 基于完整清单的行序计算，不受 --max-rows 截断影响，保证续跑幂等。
    解析失败 / 池名未知 / 空文本的行告警跳过。返回 ``([(id, row), ...], 跳过行数)``。

    Raises:
        FileNotFoundError: 清单文件不存在。
    """
    if not path.is_file():
        raise FileNotFoundError(f"清单不存在: {path}（请先运行 scripts/normalize_assets.py 生成）")
    rows: list[tuple[str, dict[str, Any]]] = []
    skipped = 0
    seqs: dict[str, int] = {pool: 0 for pool in _POOLS}
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("清单第 %d 行不是合法 JSON，跳过", line_no)
                skipped += 1
                continue
            if not isinstance(row, dict):
                logger.warning("清单第 %d 行不是 JSON 对象，跳过", line_no)
                skipped += 1
                continue
            pool = row.get("pool")
            if pool not in _POOLS:
                logger.warning("清单第 %d 行池名未知 %r，跳过", line_no, pool)
                skipped += 1
                continue
            if not is_image_row(row) and not str(row.get("text") or "").strip():
                logger.warning("清单第 %d 行缺少文本内容且非图像条目，跳过", line_no)
                skipped += 1
                continue
            seqs[str(pool)] += 1
            rows.append((f"{pool}:{seqs[str(pool)]}", row))
    return rows, skipped


def load_done_ids(out_dir: Path) -> set[str]:
    """恢复已编码 id 集合：优先 ``done_ids.json``，缺失/损坏时从 ``<pool>.meta.json`` 回退。"""
    done: set[str] = set()
    done_file = out_dir / "done_ids.json"
    if done_file.is_file():
        try:
            payload = json.loads(done_file.read_text(encoding="utf-8"))
            done = set(payload.get("ids") or ())
            logger.info("从 done_ids.json 恢复已编码 id %d 个", len(done))
            return done
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("done_ids.json 读取失败（%s），回退到向量库 meta", exc)
    for pool in _POOLS:
        meta_path = out_dir / f"{pool}.meta.json"
        if meta_path.is_file():
            try:
                payload = json.loads(meta_path.read_text(encoding="utf-8"))
                done.update(payload.get("ids") or ())
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("meta 文件读取失败，忽略该池恢复（%s）: %s", meta_path, exc)
    if done:
        logger.info("从向量库 meta 恢复已编码 id %d 个", len(done))
    return done


def write_done_ids(out_dir: Path, done: set[str]) -> None:
    """将已编码 id 全量写盘（checkpoint 时调用；排序输出便于 diff 与追溯）。"""
    payload = {"version": 1, "ids": sorted(done)}
    (out_dir / "done_ids.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def build_backend(model: str | None, device: str) -> BaseEmbedding:
    """按 T15 契约经工厂获取本地 Chinese-CLIP 后端（L2 归一化由后端保证）。"""
    local_cfg: dict[str, Any] = {"device": device}
    if model:
        local_cfg["model_name"] = model
    return get_embedding_backend({"backend": "local", "local": local_cfg})


def format_backend_error(model: str | None, exc: Exception) -> str:
    """组装 ML 依赖 / 权重失败的清晰错误提示（不裸抛 traceback）。"""
    model_desc = model or _DEFAULT_MODEL
    return "\n".join(
        [
            "本地 Chinese-CLIP 后端初始化失败：",
            f"  {exc}",
            "排查建议：",
            "  1) 确认已安装 ML 依赖：uv sync --extra ml（或 pip install torch transformers）；",
            "  2) 首次运行会自动从 HuggingFace 下载权重 "
            f"{model_desc}，约数百 MB，请确认网络可达"
            "（离线/内网环境可用 --model <本地权重目录> 指向已下载权重）；",
            "  3) 若 torch/transformers 已安装仍失败，请确认版本兼容 "
            "（transformers 5.x 与 Chinese-CLIP 组合问题可先降级 transformers 4.x 排查）。",
        ]
    )


def load_image(row_id: str, row: dict[str, Any]) -> Image.Image | None:
    """加载图像条目为 RGB PIL 图像（image_path 优先，其次 image_url）；失败返回 None 并告警。"""
    path = row.get("image_path")
    if path:
        try:
            return Image.open(str(path)).convert("RGB")
        except (OSError, ValueError) as exc:
            logger.warning("图像文件打不开，跳过（id=%s）: %s", row_id, exc)
    url = row.get("image_url")
    if url:
        try:
            resp = httpx.get(str(url), timeout=_IMAGE_URL_TIMEOUT, follow_redirects=True)
            resp.raise_for_status()
            return Image.open(io.BytesIO(resp.content)).convert("RGB")
        except (httpx.HTTPError, OSError, ValueError) as exc:
            logger.warning("图像 URL 下载/解码失败，跳过（id=%s）: %s", row_id, exc)
    logger.warning("图像条目缺少可用 image_path/image_url（id=%s）", row_id)
    return None


def encode_texts_safely(
    backend: BaseEmbedding, pairs: list[tuple[str, dict[str, Any]]]
) -> list[tuple[str, np.ndarray]]:
    """批量编码文本；整批失败降级逐条编码，单条失败仅跳过并告警。"""
    try:
        vecs = backend.encode_texts([str(row["text"]) for _, row in pairs])
        return [(rid, vec) for (rid, _), vec in zip(pairs, vecs, strict=True)]
    except Exception as exc:
        logger.warning("批文本编码失败（%s），降级逐条编码", exc)
        out: list[tuple[str, np.ndarray]] = []
        for rid, row in pairs:
            try:
                vec = backend.encode_texts([str(row["text"])])[0]
                out.append((rid, vec))
            except Exception as exc2:
                logger.warning("单条文本编码失败，跳过（id=%s）: %s", rid, exc2)
        return out


def encode_images_safely(
    backend: BaseEmbedding, pairs: list[tuple[str, dict[str, Any]]]
) -> list[tuple[str, np.ndarray]]:
    """加载并按批编码图像；加载失败/整批编码失败逐条降级，单条失败仅跳过。"""
    loaded: list[tuple[str, Image.Image]] = []
    for rid, row in pairs:
        img = load_image(rid, row)
        if img is not None:
            loaded.append((rid, img))
    if not loaded:
        return []
    try:
        vecs = backend.encode_images([img for _, img in loaded])
        return [(rid, vec) for (rid, _), vec in zip(loaded, vecs, strict=True)]
    except Exception as exc:
        logger.warning("批图像编码失败（%s），降级逐条编码", exc)
        out: list[tuple[str, np.ndarray]] = []
        for rid, img in loaded:
            try:
                vec = backend.encode_images([img])[0]
                out.append((rid, vec))
            except Exception as exc2:
                logger.warning("单条图像编码失败，跳过（id=%s）: %s", rid, exc2)
        return out


def build_items(
    pairs: list[tuple[str, dict[str, Any]]], encoded: list[tuple[str, np.ndarray]]
) -> list[VectorItem]:
    """由编码结果构造 VectorItem，metadata 含 category/source/text（text 截断 200）。"""
    by_id = {rid: row for rid, row in pairs}
    items: list[VectorItem] = []
    for rid, vec in encoded:
        row = by_id[rid]
        items.append(
            VectorItem(
                id=rid,
                pool=str(row["pool"]),
                vector=vec,
                metadata={
                    "category": str(row.get("category") or ""),
                    "source": str(row.get("source") or ""),
                    "text": str(row.get("text") or "")[:_TEXT_TRUNC],
                },
            )
        )
    return items


def summarize_out_dir(out_dir: Path) -> str:
    """只读统计既有向量库概况（用于 dry-run 与续跑预期提示）。"""
    counts: dict[str, int] = {}
    for pool in _POOLS:
        meta_path = out_dir / f"{pool}.meta.json"
        if meta_path.is_file():
            try:
                payload = json.loads(meta_path.read_text(encoding="utf-8"))
                counts[pool] = len(payload.get("ids") or ())
            except (json.JSONDecodeError, OSError):
                counts[pool] = -1
    if not counts:
        return "无（首次构建）"
    return (
        "black "
        + str(counts.get("black", 0))
        + " / white "
        + str(counts.get("white", 0))
        + " 条（--resume 将跳过这些 id）"
    )


def format_dry_run(
    args: argparse.Namespace, rows: list[tuple[str, dict[str, Any]]], skipped_lines: int
) -> str:
    """dry-run 统计报告（仅读清单与既有库，不编码不写盘）。"""
    n_black = sum(1 for _, row in rows if row["pool"] == "black")
    n_white = len(rows) - n_black
    img_rows = sum(1 for _, row in rows if is_image_row(row))
    lines = ["=" * 64, "SafeFusion 向量库构建 —— dry-run（只统计，不编码不写盘）"]
    lines.append(f"清单      : {args.manifest}")
    lines.append(f"输出目录  : {args.out}")
    lines.append(
        f"清单行数  : {len(rows)}（black {n_black} / white {n_white}；解析跳过 {skipped_lines} 行）"
    )
    lines.append(
        f"图像条目  : {img_rows}（含 image_path/image_url；当前清单为纯文本，走 encode_texts）"
    )
    if args.max_rows > 0:
        sub = rows[: args.max_rows]
        sub_black = sum(1 for _, row in sub if row["pool"] == "black")
        lines.append(
            f"截断预览  : --max-rows={args.max_rows} 时处理前 {len(sub)} 行"
            f"（black {sub_black} / white {len(sub) - sub_black}）"
        )
    lines.append(f"既有库    : {summarize_out_dir(Path(args.out))}")
    lines.append(
        f"编码 id   : {{pool}}:{{seq}}（池内 1-based，完整清单次序）；续跑源 "
        f"{Path(args.out) / 'done_ids.json'}"
    )
    lines.append("=" * 64)
    return "\n".join(lines)


def format_run_report(
    args: argparse.Namespace,
    scope_rows: int,
    n_black: int,
    n_white: int,
    skipped_done: int,
    encoded_black: int,
    encoded_white: int,
    failed: int,
    dim: int | None,
    elapsed: float,
    store: NumpyVectorStore,
    out_dir: Path,
) -> str:
    """真实运行完成报告：黑白各入库条数、维度、耗时。"""
    lines = ["=" * 64, "SafeFusion 向量库构建 —— 完成报告"]
    trunc_note = f"（--max-rows={args.max_rows} 截断）" if args.max_rows > 0 else ""
    lines.append(f"清单范围  : {scope_rows} 行（black {n_black} / white {n_white}）{trunc_note}")
    lines.append(f"断续续跑  : {'开' if args.resume else '关'}（跳过已入库 {skipped_done} 条）")
    lines.append(f"模型      : {args.model or _DEFAULT_MODEL}（device={args.device}）")
    lines.append(f"本次编码  : black {encoded_black} / white {encoded_white}，失败 {failed} 条")
    lines.append(f"入库总数  : black {store.count('black')} / white {store.count('white')}")
    lines.append(f"向量维度  : {dim if dim is not None else '本次未编码（已全部在库）'}")
    lines.append(f"耗时      : {elapsed:.1f}s")
    lines.append(f"持久化    : {out_dir}（<pool>.npz + <pool>.meta.json + done_ids.json）")
    lines.append("=" * 64)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """构建主流程：读清单 →（dry-run 结束）→ 恢复 done → 批编码 → 分批持久化 → 报告。"""
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr
    )
    args = parse_args(argv)

    try:
        rows, skipped_lines = read_manifest(Path(args.manifest))
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1
    if not rows:
        logger.error("清单为空或无有效行，退出（解析跳过 %d 行）", skipped_lines)
        return 1

    if args.dry_run:
        print(format_dry_run(args, rows, skipped_lines))
        logger.info("dry-run 完成，未编码未写盘")
        return 0

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    done: set[str] = set()
    if args.resume:
        done = load_done_ids(out_dir)
        store = NumpyVectorStore.load(out_dir)  # 恢复既有库，追加后 save 不丢旧数据
    else:
        store = NumpyVectorStore(out_dir)

    if args.max_rows > 0:
        rows = rows[: args.max_rows]
    pending = [(rid, row) for rid, row in rows if rid not in done]
    skipped_done = len(rows) - len(pending)
    n_black = sum(1 for _, row in rows if row["pool"] == "black")
    n_white = len(rows) - n_black

    if not pending:
        elapsed_zero = 0.0
        print(
            format_run_report(
                args,
                len(rows),
                n_black,
                n_white,
                skipped_done,
                0,
                0,
                0,
                None,
                elapsed_zero,
                store,
                out_dir,
            )
        )
        logger.info("无待编码条目（已全部在库），直接结束")
        return 0

    try:
        backend = build_backend(args.model, args.device)
    except (RuntimeError, ImportError, OSError, ValueError) as exc:
        logger.error("本地 Chinese-CLIP 后端不可用")
        print(format_backend_error(args.model, exc))
        return 1

    batch_size = max(1, args.batch)
    total = len(pending)
    batch_count = (total + batch_size - 1) // batch_size
    encoded_black = 0
    encoded_white = 0
    failed = 0
    dim: int | None = None
    start = time.perf_counter()

    for batch_no, start_idx in enumerate(range(0, total, batch_size), start=1):
        chunk = pending[start_idx : start_idx + batch_size]
        text_pairs = [(rid, row) for rid, row in chunk if not is_image_row(row)]
        image_pairs = [(rid, row) for rid, row in chunk if is_image_row(row)]

        items: list[VectorItem] = []
        if text_pairs:
            items.extend(build_items(text_pairs, encode_texts_safely(backend, text_pairs)))
        if image_pairs:
            items.extend(build_items(image_pairs, encode_images_safely(backend, image_pairs)))

        if items:
            store.add(items)
            done.update(item.id for item in items)
            if dim is None:
                dim = int(items[0].vector.shape[0])
            for item in items:
                if item.pool == "black":
                    encoded_black += 1
                else:
                    encoded_white += 1
        failed += len(chunk) - len(items)

        if batch_no % _SAVE_EVERY_BATCHES == 0:
            store.save()
            write_done_ids(out_dir, done)
            logger.info(
                "检查点保存完成（第 %d/%d 批，已入库 %d/%d 条）",
                batch_no,
                batch_count,
                len(done),
                total,
            )
        logger.info(
            "批次 %d/%d：编码 %d 条（累计编码 %d/%d，失败 %d）",
            batch_no,
            batch_count,
            len(items),
            encoded_black + encoded_white,
            total,
            failed,
        )

    store.save()
    write_done_ids(out_dir, done)
    elapsed = time.perf_counter() - start
    print(
        format_run_report(
            args,
            len(rows),
            n_black,
            n_white,
            skipped_done,
            encoded_black,
            encoded_white,
            failed,
            dim,
            elapsed,
            store,
            out_dir,
        )
    )
    logger.info(
        "向量库构建完成（入库 black %d / white %d，耗时 %.1fs）",
        store.count("black"),
        store.count("white"),
        elapsed,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
