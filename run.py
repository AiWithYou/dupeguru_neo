#!/usr/bin/python3
# Copyright 2017 Virgil Dupras
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

import gc
import gettext
import os.path as op
import sys
from pathlib import Path

from PyQt6.QtCore import QCoreApplication, QObject, Qt
from PyQt6.QtGui import QGuiApplication, QIcon, QPixmap
from PyQt6.QtWidgets import QApplication

from hscommon.trans import install_gettext_trans_under_qt
from qt.error_report_dialog import install_excepthook
from qt.resources import resource_path
from qt.util import setup_qt_logging, create_qsettings
from qt.platform import BASE_PATH
from core import __version__, __appname__, __organization__, __issue_url__

# SIGQUIT is not defined on Windows
if sys.platform == "win32":
    from signal import signal, SIGINT, SIGTERM

    SIGQUIT = SIGTERM
else:
    from signal import signal, SIGINT, SIGTERM, SIGQUIT

dgapp = None


def _configure_high_dpi():
    """Preserve the operating system's fractional per-monitor scale factor."""

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)


def signal_handler(sig, frame):
    if dgapp is None:
        return
    if sig in (SIGINT, SIGTERM, SIGQUIT):
        dgapp.SIGTERM.emit()


def setup_signals():
    signal(SIGINT, signal_handler)
    signal(SIGTERM, signal_handler)
    signal(SIGQUIT, signal_handler)


def _launcher_mode(argv):
    """Consume launcher-only switches while leaving Qt platform switches intact."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    show_version = "--version" in arguments
    self_test = "--self-test" in arguments
    arguments = [item for item in arguments if item not in {"--version", "--self-test"}]
    if show_version and self_test:
        raise ValueError("--version and --self-test cannot be used together")
    return show_version, self_test, arguments


def _run_self_test(qt_arguments):
    """Validate the frozen GUI runtime without opening a window or user data."""

    _configure_high_dpi()
    app = QApplication(["dupeguru-neo-self-test", "-platform", "offscreen", *qt_arguments])
    _validate_frozen_localizations()
    pixmap = QPixmap(resource_path("logo_se"))
    if pixmap.isNull():
        raise RuntimeError("The packaged application icon could not be decoded")
    from qt.app import DupeGuru

    if not issubclass(DupeGuru, QObject):
        raise RuntimeError("The packaged Qt application class is invalid")
    app.quit()
    return 0


def _validate_frozen_localizations():
    """Fail packaged smoke tests when application translations are unreachable."""

    if not getattr(sys, "frozen", False):
        return
    data_root = Path(BASE_PATH)
    locale_root = data_root / "locale"
    japanese_ui = gettext.translation("ui", localedir=locale_root, languages=["ja"])
    if japanese_ui.gettext("File") == "File":
        raise RuntimeError("the packaged Japanese UI catalog did not translate a known message")
    if not (data_root / "help" / "ja" / "index.html").is_file():
        raise RuntimeError("the packaged Japanese help entry point is missing")


def main(argv=None):
    show_version, self_test, qt_arguments = _launcher_mode(argv)
    if show_version:
        # PyInstaller's Windows ``--windowed`` bootloader intentionally exposes
        # no console streams. Version probing must still be a successful,
        # side-effect-free operation in that environment.
        if sys.stdout is not None:
            print(__version__)
        return 0
    if self_test:
        return _run_self_test(qt_arguments)

    _configure_high_dpi()
    app = QApplication([sys.argv[0], *qt_arguments])
    QCoreApplication.setOrganizationName(__organization__)
    QCoreApplication.setApplicationName(__appname__)
    QCoreApplication.setApplicationVersion(__version__)
    setup_qt_logging()
    settings = create_qsettings()
    lang = settings.value("Language")
    locale_folder = op.join(BASE_PATH, "locale")
    install_gettext_trans_under_qt(locale_folder, lang)
    # Handle OS signals
    setup_signals()
    # Let the Python interpreter runs every 500ms to handle signals.  This is
    # required because Python cannot handle signals while the Qt event loop is
    # running.
    from PyQt6.QtCore import QTimer

    timer = QTimer()
    timer.start(500)
    timer.timeout.connect(lambda: None)
    # Many strings are translated at import time, so this is why we only import after the translator
    # has been installed
    from qt.app import DupeGuru

    app.setWindowIcon(QIcon(QPixmap(resource_path(DupeGuru.LOGO_NAME))))
    global dgapp
    dgapp = DupeGuru()
    install_excepthook(__issue_url__)
    result = app.exec()
    # I was getting weird crashes when quitting under Windows, and manually deleting main app
    # references with gc.collect() in between seems to fix the problem.
    del dgapp
    gc.collect()
    del app
    gc.collect()
    return result


if __name__ == "__main__":
    sys.exit(main())
