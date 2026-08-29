#!/usr/bin/env python3
"""SafeFusion Embedding 服务吞吐调优测试脚本。

用于找出 llama.cpp embedding 服务的**最优吞吐配置组合**（batch 大小 × 并发数），
帮助确认服务端 GPU 是否吃满、以及客户端该用什么参数构建向量库。

用法（venv 解释器，禁 uv run）::

    .venv\\Scripts\\python.exe scripts\\bench_embedding.py
    .venv\\Scripts\\python.exe scripts\\bench_embedding.py --url http://127.0.0.1:5545/v1
    .venv\\Scripts\\python.exe scripts\\bench_embedding.py --model <模型名> `
        --batches "1,4,16,64,256" --workers "1,2,4,8,16"
    .venv\\Scripts\\python.exe scripts\\bench_embedding.py --repeat 3 --probe 8
        # 每组合测 3 次，预热 8 条

设计：
- 每个 (batch, workers) 组合：先发 ``--probe`` 条预热（排除冷启动），再并发发
  ``--repeat`` 轮（每 worker 每轮一批），统计总吞吐 条/s 与单批延迟；
- 输出一张表：batch × workers → 条/s（★ 标记最高值）；
- 附带服务端 n_ctx / 模型 / 端口信息（经 /v1/models）；
- 纯 httpx，无额外依赖；Ctrl+C 随时中断。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import random
import sys
import time
from typing import Any

import httpx

_DEFAULT_URL = "http://127.0.0.1:5545/v1"
_DEFAULT_MODEL = "/home/featurize/WeMM-Embedding-2B-Q4_K_M.gguf"
_DEFAULT_BATCHES = (1, 4, 16, 64, 256)
_DEFAULT_WORKERS = (1, 2, 4, 8, 16)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bench_embedding",
        description="Embedding 服务吞吐调优：batch × workers 网格测试。",
    )
    parser.add_argument("--url", default=_DEFAULT_URL, help=f"服务 base_url（默认 {_DEFAULT_URL}）")
    parser.add_argument(
        "--model", default=_DEFAULT_MODEL, help=f"模型名（默认 {_DEFAULT_MODEL}）"
    )
    parser.add_argument(
        "--batches",
        default=",".join(str(b) for b in _DEFAULT_BATCHES),
        help="batch 大小列表（逗号分隔），默认 1,4,16,64,256",
    )
    parser.add_argument(
        "--workers",
        default=",".join(str(w) for w in _DEFAULT_WORKERS),
        help="并发 worker 数列表（逗号分隔），默认 1,2,4,8,16",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=3,
        help="每 worker 每组合的批次数（默认 3；越大越准但越久）",
    )
    parser.add_argument(
        "--probe",
        type=int,
        default=8,
        help="预热条数（默认 8；排除冷启动）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="单请求超时秒数（默认 300；大 batch 可能要更久）",
    )
    parser.add_argument(
        "--text-len",
        type=int,
        default=20,
        help="每条测试文本长度（中文字数，默认 20）",
    )
    return parser.parse_args(argv)


def fetch_server_info(url: str) -> dict[str, Any]:
    """读取 /v1/models 的服务端信息（模型维度/上下文/量化等）。"""
    try:
        r = httpx.get(f"{url}/models", timeout=15)
        r.raise_for_status()
        payload = r.json()
        data = payload.get("data") or []
        if data:
            meta = data[0].get("meta") or {}
            return {
                "model": data[0].get("id", ""),
                "n_embd": meta.get("n_embd"),
                "n_ctx": meta.get("n_ctx"),
                "n_params": meta.get("n_params"),
                "quant": meta.get("ftype"),
                "owned_by": data[0].get("owned_by", ""),
            }
    except Exception as exc:  # noqa: BLE001 - 信息可选，失败不阻断
        return {"error": str(exc)}
    return {}


def build_texts(length: int, n: int, tag: str = "") -> list[str]:
    """生成 n 条测试文本（前 16 字符随机字母前缀，避免缓存命中）。

    真实语料长短不一，这里用随机前缀 + 中文主体模拟；前缀随机保证
    每批文本 token 序列几乎不重复，prompt cache 无法命中。
    """
    letters = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    base = "这是一段用于嵌入编码吞吐测试的普通中文句子。"
    out: list[str] = []
    for i in range(n):
        prefix = "".join(random.choice(letters) for _ in range(16))
        core = base[: length - 16] if length > 16 else base[:length]
        out.append(f"{prefix}{core}{tag}{i}")
    return out


def one_batch(
    client: httpx.Client, url: str, model: str, texts: list[str], timeout: float
) -> float:
    """发一批，返回耗时（秒）。"""
    t0 = time.perf_counter()
    resp = client.post(f"{url}/embeddings", json={"model": model, "input": texts}, timeout=timeout)
    resp.raise_for_status()
    return time.perf_counter() - t0


def bench_combination(
    url: str,
    model: str,
    batch: int,
    workers: int,
    repeat: int,
    probe: int,
    timeout: float,
    text_len: int,
) -> tuple[float, float, list[str]]:
    """测一个 (batch, workers) 组合，返回 (吞吐条/s, 平均单批秒, 备注列表)。"""
    notes: list[str] = []
    # 预热：单请求 probe 条（或 batch 条，取大）
    warm_n = max(probe, batch)
    with httpx.Client(timeout=timeout) as c:
        try:
            one_batch(c, url, model, build_texts(text_len, warm_n), timeout)
        except Exception as exc:  # noqa: BLE001
            return 0.0, 0.0, [f"预热失败: {type(exc).__name__}: {str(exc)[:80]}"]

        def _worker(i: int) -> float:
            texts = build_texts(text_len, batch, tag=f"w{i}")
            with httpx.Client(timeout=timeout) as cc:
                t0 = time.perf_counter()
                for _ in range(repeat):
                    one_batch(cc, url, model, texts, timeout)
                return time.perf_counter() - t0

        t0 = time.perf_counter()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(_worker, range(workers)))
        except Exception as exc:  # noqa: BLE001
            return 0.0, 0.0, [f"并发失败: {type(exc).__name__}: {str(exc)[:80]}"]

    elapsed = time.perf_counter() - t0
    total_items = workers * repeat * batch
    rate = total_items / elapsed if elapsed > 0 else 0.0
    per_batch = (elapsed / workers / repeat) if workers and repeat else 0.0
    return rate, per_batch, notes


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)

    batches = [int(b) for b in args.batches.split(",") if b.strip()]
    workers = [int(w) for w in args.workers.split(",") if w.strip()]
    if not batches or not workers:
        print("错误：--batches / --workers 至少各一个值")
        return 1

    print("=" * 72)
    print("Embedding 服务吞吐调优")
    info = fetch_server_info(args.url)
    for k, v in info.items():
        print(f"  {k}: {v}")
    print(f"  测试文本长度: {args.text_len} 字 | 每组合 {args.repeat} 轮 | 预热 {args.probe} 条")
    print(f"  网格: batch {batches} × workers {workers}")
    print("=" * 72)

    # 表格收集：rows[batch][workers] = rate
    results: dict[int, dict[int, float]] = {}
    best = (0.0, 0, 0)  # (rate, batch, workers)
    total_combos = len(batches) * len(workers)
    combo_no = 0

    for batch in batches:
        results[batch] = {}
        for w in workers:
            combo_no += 1
            print(
                f"\n[{combo_no}/{total_combos}] batch={batch} workers={w} ...",
                flush=True,
            )
            rate, per_batch, notes = bench_combination(
                args.url, args.model, batch, w, args.repeat, args.probe, args.timeout, args.text_len
            )
            results[batch][w] = rate
            print(f"    吞吐 {rate:7.1f} 条/s | 单批 {per_batch:6.2f}s", flush=True)
            for note in notes:
                print(f"    ! {note}", flush=True)
            if rate > best[0]:
                best = (rate, batch, w)

    # 汇总表
    print("\n" + "=" * 72)
    print("汇总（条/s）  batch \\ workers")
    header = "".join(f"{w:>10}" for w in workers)
    print(f"{'batch':>8}{header}")
    for batch in batches:
        row = f"{batch:>8}"
        for w in workers:
            rate = results[batch].get(w, 0.0)
            mark = " ★" if (batch, w) == (best[1], best[2]) else "  "
            row += f"{rate:>8.1f}{mark}"
        print(row)
    print("-" * 72)
    print(f"最优: batch={best[1]} workers={best[2]} => {best[0]:.1f} 条/s")
    if best[0] > 0:
        total_est = 189262 / best[0] / 3600
        print(f"按此配置全量 189,262 条约 {total_est:.1f} 小时")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
