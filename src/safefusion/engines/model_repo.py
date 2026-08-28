"""模型仓库工具（PRD v0.3.0 M6 D1/D2）：HF 缓存解析/探测 + CLIP 权重后台下载任务。

职责：

- :func:`resolve_hf_cache_dir` —— 解析 HF 缓存根目录：环境变量 ``HF_HOME``
  优先（HF 惯例，``transformers`` / ``huggingface_hub`` 均尊重该变量），
  未设置时默认 ``{data_dir}/models/hf``（仓库约定布局，含 ``hub/`` 与
  ``xet/`` 子目录）。统一入口后，模型**下载**（:func:`download_clip_weights`
  显式传 ``cache_dir``）与模型**装载**（``LocalChineseCLIP`` 经
  ``AppContext`` 装配时同样显式传 ``cache_dir``）落到同一缓存根，天然一致；
- :func:`probe_hf_model` —— 探测 HF hub 缓存中某模型目录
  （``hub/models--<org>--<name>``）的 blobs 文件数与总字节数，供
  ``GET /admin/models`` 展示「未下载 / 已就绪」与缓存大小；
- :class:`DownloadManager` —— 进程内后台下载任务注册表：
  - ``start`` 触发后台线程下载（同模型并行下载**互斥**：进行中任务被复用）；
  - ``get(task_id)`` 供轮询端点读取进度快照；
  - 任务状态对象（阶段 / 百分比 / 已下载字节 / 错误）线程安全（独立锁）；
  - 下载实现在:func:`download_clip_weights`，经 ``huggingface_hub`` 的
    ``snapshot_download``（尊重 ``HF_ENDPOINT`` 镜像 / HTTP 代理，复用既有
    缓存块断点续传）；``huggingface_hub`` 缺失（ML 依赖未装）时任务失败并
    给出安装指引，不崩溃。

测试注入：:func:`download_clip_weights` 为**模块级名字**，单测直接
``monkeypatch.setattr(model_repo, "download_clip_weights", fake)`` 驱动任务
生命周期，无需真实网络。
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger("safefusion.engines.model_repo")

#: 默认 Chinese-CLIP 模型名（对齐 config.py EmbeddingLocalConfig 默认值）
DEFAULT_MODEL_NAME = "OFA-Sys/chinese-clip-vit-base-patch16"

#: HF 缓存根环境变量（transformers / huggingface_hub 惯例）
_ENV_HF_HOME = "HF_HOME"

#: HF hub 缓存中模型目录名：models--<org>--<name>
_REPO_DIR_PREFIX = "models--"

#: 下载任务状态全集
_TASK_STATUSES: tuple[str, ...] = ("running", "completed", "failed")


def _utc_now() -> str:
    """当前 UTC 时间的 ISO 8601 字符串（毫秒精度）。"""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def resolve_hf_cache_dir(data_dir: str | Path) -> Path:
    """解析 HF 缓存根目录：``HF_HOME`` 环境变量优先，否则 ``{data_dir}/models/hf``。

    Args:
        data_dir: 运行时数据目录（仅 HF_HOME 未设置时用于推算默认缓存根）。

    Returns:
        缓存根目录（``hub/`` 子目录存放模型快照与 blobs）。
    """

    env_root = os.environ.get(_ENV_HF_HOME)
    if env_root:
        return Path(env_root)
    return Path(data_dir) / "models" / "hf"


def probe_hf_model(cache_dir: str | Path, model_name: str) -> dict[str, Any]:
    """探测 HF hub 缓存中指定模型的下载状态与体积。

    Args:
        cache_dir: HF 缓存根目录（``resolve_hf_cache_dir`` 产出）。
        model_name: HF 模型标识（如 ``OFA-Sys/chinese-clip-vit-base-patch16``）。

    Returns:
        ``{"exists", "blobs", "size_bytes", "complete", "repo_dir"}``：
        ``blobs`` 为 cache 内已下载块文件数；``size_bytes`` 为块文件总字节；
        ``complete`` 表示存在非空 snapshots 目录（至少一次完整快照）；
        ``repo_dir`` 为仓库缓存目录路径（不存在也返回预期路径，便于提示）。
    """

    repo_dir = Path(cache_dir) / "hub" / (_REPO_DIR_PREFIX + model_name.replace("/", "--"))
    blobs: list[Path] = []
    blobs_dir = repo_dir / "blobs"
    if blobs_dir.is_dir():
        with os.scandir(blobs_dir) as entries:
            for entry in entries:
                if entry.is_file():
                    blobs.append(Path(entry.path))
    size_bytes = 0
    for blob in blobs:
        try:
            size_bytes += blob.stat().st_size
        except OSError:
            continue
    snapshots = repo_dir / "snapshots"
    complete = snapshots.is_dir() and any(snapshots.iterdir())
    return {
        "exists": repo_dir.is_dir(),
        "blobs": len(blobs),
        "size_bytes": size_bytes,
        "complete": complete,
        "repo_dir": str(repo_dir),
    }


@dataclass
class DownloadTask:
    """单个模型下载任务的状态对象（线程安全，供轮询端点读取）。

    Attributes:
        task_id: 任务唯一标识（轮询端点路径参数）。
        model_name: 目标 HF 模型名。
        status: ``running`` / ``completed`` / ``failed``。
        stage: 阶段：``starting`` / ``downloading`` / ``extracting`` / ``completed``。
        progress: 完成百分比（0~100，粗粒度由 tqdm 累计字节换算）。
        downloaded_bytes / total_bytes: 已下载 / 总字节数（total 为 None 时未知）。
        error: 失败原因（仅 failed 时非 None；已脱敏截断）。
    """

    task_id: str
    model_name: str
    status: str = "running"
    stage: str = "starting"
    progress: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    error: str | None = None
    started_at: str = field(default_factory=_utc_now)
    finished_at: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update_progress(
        self,
        *,
        progress: float | None = None,
        bytes_done: int | None = None,
        total: int | None = None,
        stage: str | None = None,
    ) -> None:
        """线程安全更新任务进度（tqdm 回调 / 阶段推进共用）。"""

        with self._lock:
            if progress is not None:
                self.progress = round(max(0.0, min(100.0, progress)), 1)
            if bytes_done is not None:
                self.downloaded_bytes = int(bytes_done)
            if total is not None:
                self.total_bytes = int(total)
            if stage is not None:
                self.stage = stage

    def mark_completed(self) -> None:
        """标记任务完成（status=completed，stage=completed，进度置 100）。"""

        with self._lock:
            self.status = "completed"
            self.stage = "completed"
            self.progress = 100.0
            self.finished_at = _utc_now()

    def mark_failed(self, error: str) -> None:
        """标记任务失败（status=failed，记录脱敏错误信息）。"""

        with self._lock:
            self.status = "failed"
            self.stage = "failed"
            self.error = (error or "未知错误").strip()[:300]
            self.finished_at = _utc_now()

    def snapshot(self) -> dict[str, Any]:
        """返回任务进度快照（轮询端点响应体；读锁内复制，不含锁对象）。"""

        with self._lock:
            return {
                "task_id": self.task_id,
                "model_name": self.model_name,
                "status": self.status,
                "stage": self.stage,
                "progress": self.progress,
                "downloaded_bytes": self.downloaded_bytes,
                "total_bytes": self.total_bytes,
                "error": self.error,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            }


class DownloadManager:
    """进程内模型下载任务注册表（线程安全；同模型并发下载互斥）。

    - ``start(model_name, cache_dir)``：同模型存在**进行中**任务时直接复用
      （返回 ``(task, reused=True)``，不重复起线程）；
      否则创建新任务并启动后台下载线程（daemon，异常隔离）；
    - ``get(task_id)``：按任务 id 取任务（快照由调用方经 ``snapshot`` 读取）；
    - ``running_for(model_name)``：查询某模型的进行中任务（供状态端点判定
      「下载中」）。
    """

    def __init__(self) -> None:
        self._tasks: dict[str, DownloadTask] = {}
        self._running_by_model: dict[str, str] = {}
        self._lock = threading.RLock()

    def start(self, model_name: str, cache_dir: str | Path) -> tuple[DownloadTask, bool]:
        """启动（或复用）一次模型下载。

        Args:
            model_name: HF 模型名。
            cache_dir: 下载目标 HF 缓存根目录（``resolve_hf_cache_dir`` 产出）。

        Returns:
            ``(task, reused)``：``reused=True`` 表示复用进行中任务（互斥）；
            ``False`` 表示本次新启动。
        """

        with self._lock:
            running_id = self._running_by_model.get(model_name)
            if running_id is not None and running_id in self._tasks:
                task = self._tasks[running_id]
                if task.status == "running":
                    return task, True
            task = DownloadTask(
                task_id=secrets.token_urlsafe(10),
                model_name=model_name,
            )
            self._tasks[task.task_id] = task
            self._running_by_model[model_name] = task.task_id
            thread = threading.Thread(
                target=self._run_download,
                args=(task, str(cache_dir)),
                daemon=True,
                name=f"safefusion-model-download-{task.task_id[:6]}",
            )
            thread.start()
            return task, False

    def _run_download(self, task: DownloadTask, cache_dir: str) -> None:
        """后台线程执行体：调用下载实现并在 finally 清理进行中登记。"""

        try:
            download_clip_weights(task.model_name, cache_dir, task)
        except Exception as exc:
            _LOGGER.exception("模型下载失败 task_id=%s model=%s", task.task_id, task.model_name)
            task.mark_failed(str(exc))
        finally:
            with self._lock:
                if self._running_by_model.get(task.model_name) == task.task_id:
                    self._running_by_model.pop(task.model_name, None)

    def get(self, task_id: str) -> DownloadTask | None:
        """按任务 id 取任务；不存在返回 None。"""

        with self._lock:
            return self._tasks.get(task_id)

    def running_for(self, model_name: str) -> DownloadTask | None:
        """返回指定模型的进行中任务；无则 None（供 /admin/models 状态判定）。"""

        with self._lock:
            task_id = self._running_by_model.get(model_name)
            if task_id is None:
                return None
            task = self._tasks.get(task_id)
            return task if task is not None and task.status == "running" else None


def download_clip_weights(model_name: str, cache_dir: str | Path, task: DownloadTask) -> None:
    """同步下载 CLIP 权重到 HF 缓存根目录（后台线程内执行）。

    - 使用 ``huggingface_hub.snapshot_download``：尊重 ``HF_ENDPOINT`` 镜像、
      HTTP(S) 代理等 HF 惯例环境变量；已缓存块自动复用（断点续传语义）；
    - 进度经 ``tqdm_class`` 钩子写回 ``task``（下载阶段百分比 / 字节数）；
    - ``huggingface_hub`` 缺失（ML 可选依赖未安装）抛 ``RuntimeError``，
      由调用方（``DownloadManager._run_download``）标记任务失败。
    """

    try:
        from huggingface_hub import snapshot_download  # type: ignore[import-not-found]
        from tqdm import tqdm  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "模型下载需要 huggingface_hub（huggingface_hub 由 ML 依赖提供，"
            "请执行 `uv sync --extra ml` 后重试）"
        ) from exc

    cache_root = Path(cache_dir)
    cache_root.mkdir(parents=True, exist_ok=True)
    task.update_progress(stage="downloading", progress=0.0, bytes_done=0)

    class _ProxyTqdm(tqdm):
        """把 snapshot_download 的 tqdm 进度桥接进任务对象（仅本下载进程使用）。"""

        def update(self, n: int = 1) -> bool | None:
            super().update(n)
            total = self.total
            if total:
                task.update_progress(
                    progress=(self.n / total) * 100.0,
                    bytes_done=self.n,
                    total=total,
                )
            else:
                task.update_progress(bytes_done=self.n)
            return None

    _LOGGER.info("开始下载模型 %s → %s", model_name, cache_root)
    snapshot_download(
        repo_id=model_name,
        cache_dir=str(cache_root),
        tqdm_class=_ProxyTqdm,
    )
    task.mark_completed()
    _LOGGER.info("模型下载完成 %s（cache=%s）", model_name, cache_root)
