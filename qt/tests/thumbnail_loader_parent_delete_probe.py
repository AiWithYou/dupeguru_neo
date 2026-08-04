"""Child-process probe for native Qt thumbnail-pool teardown deadlocks."""

import gc
import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import sip  # noqa: E402
from PyQt6.QtCore import QObject, QSize  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from core.file_generation import FileGenerationToken  # noqa: E402
from qt.pe import review_gallery  # noqa: E402


class SlowCache:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def load(self, *_args):
        self.started.set()
        if not self.release.wait(10):
            raise RuntimeError("thumbnail worker was not released")
        return None

    def store(self, *_args):
        return False


def main():
    application = QApplication.instance() or QApplication([])
    parent = QObject()
    cache = SlowCache()
    loader = review_gallery.LazyThumbnailLoader(
        QSize(40, 30),
        disk_cache=cache,
        max_concurrent_tasks=1,
        parent=parent,
    )
    loader.request(
        "1" * 64,
        "",
        expected_generation_token=FileGenerationToken("probe", 1).encoded,
    )
    if not cache.started.wait(10):
        raise RuntimeError("thumbnail worker did not start")
    deletion_started = threading.Event()
    parent.destroyed.connect(lambda *_args: deletion_started.set())

    def release_after_deletion_starts():
        if not deletion_started.wait(10):
            return
        # Give QObject destruction time to enter the child pool teardown.  The
        # fixed drain releases the GIL; the raw C++ destructor does not.
        time.sleep(0.25)
        cache.release.set()

    releaser = threading.Thread(target=release_after_deletion_starts, daemon=True)
    releaser.start()

    # A private QThreadPool parented to the loader deadlocks here: QObject
    # destruction waits for the Python runnable while the worker needs the GIL.
    sip.delete(parent)
    releaser.join(5)
    if releaser.is_alive():
        raise RuntimeError("thumbnail worker release thread did not finish")
    del loader
    del parent
    gc.collect()

    application.processEvents()
    if review_gallery._thumbnail_task_runtime().task_count:
        raise RuntimeError("completed thumbnail task was not released")
    print("thumbnail parent deletion: PASS")


if __name__ == "__main__":
    main()
