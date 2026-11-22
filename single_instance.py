"""单实例运行保护。

第一个进程持有数据目录下的文件锁；后续启动不再创建窗口，只写一个
show-request 文件。第一个进程的轻量监视线程看到后会把已有主窗口拉回前台。

锁定用操作系统文件锁而不是 PID 文件：进程崩溃或被杀掉时锁会自动释放，
不会留下“看似还在运行”的僵尸锁。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("nsu.single_instance")


class SingleInstance:
    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.lock_path = self.data_dir / "instance.lock"
        self.show_path = self.data_dir / "instance.show"
        self._lock_file = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._show_callback: Optional[Callable[[], None]] = None
        self._callback_lock = threading.Lock()

    @property
    def primary(self) -> bool:
        return self._lock_file is not None

    def acquire(self) -> bool:
        """尝试成为主实例。成功返回 True，已有实例返回 False。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        lock_file = open(self.lock_path, "a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt

                lock_file.seek(0)
                if not lock_file.read(1):
                    lock_file.write("0")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock_file.close()
            return False
        self._lock_file = lock_file
        return True

    def start(self) -> None:
        """主实例启动 show-request 监视线程。"""
        if not self.primary or (self._thread and self._thread.is_alive()):
            return
        self._thread = threading.Thread(
            target=self._watch_show_requests,
            name="nsu-single-instance",
            daemon=True,
        )
        self._thread.start()

    def set_show_callback(self, callback: Callable[[], None]) -> None:
        with self._callback_lock:
            self._show_callback = callback

    def request_show(self) -> bool:
        """请求已运行实例显示主窗口。第二个进程调用后即可退出。"""
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.show_path.with_name(f"{self.show_path.name}.{os.getpid()}.tmp")
            tmp.write_text(str(time.time()), encoding="utf-8")
            os.replace(tmp, self.show_path)
            return True
        except OSError as exc:
            log.warning("写入显示请求失败：%s", exc)
            return False

    def _watch_show_requests(self) -> None:
        while not self._stop.wait(0.5):
            if not self.show_path.exists():
                continue
            with self._callback_lock:
                callback = self._show_callback
            if callback is None:
                # 主窗口还没创建完，先保留请求，回调注册后会马上消费。
                continue
            try:
                self.show_path.unlink(missing_ok=True)
            except OSError:
                continue
            try:
                callback()
            except Exception:
                log.exception("处理显示请求失败")

    def close(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        lock_file = self._lock_file
        self._lock_file = None
        if lock_file is not None:
            try:
                lock_file.close()
            except OSError:
                pass
        try:
            self.show_path.unlink(missing_ok=True)
        except OSError:
            pass
