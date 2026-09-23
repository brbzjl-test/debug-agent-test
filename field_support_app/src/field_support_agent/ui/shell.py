from __future__ import annotations

import os
import sys
from typing import Optional

from .preview import preview_server


def run_desktop_shell(
    host: str = "127.0.0.1",
    port: int = 0,
    *,
    core_url: Optional[str] = None,
    session_token: Optional[str] = None,
) -> None:
    try:
        from PySide6.QtCore import QObject, Qt, QUrl, Signal, Slot
        from PySide6.QtWebChannel import QWebChannel
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        raise ImportError("PySide6 with QtWebEngine is required for the desktop shell") from exc

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("现场调试助手")
    app.setQuitOnLastWindowClosed(False)

    with preview_server(host, port, core_url=core_url, session_token=session_token) as (_, base_url):
        chat_url = base_url
        if not core_url and os.environ.get("FIELD_SUPPORT_UI_DEMO", "1") != "0":
            chat_url = f"{base_url}?demo=1"

        class ChatWindow(QWebEngineView):
            def closeEvent(self, event: object) -> None:
                event.ignore()
                self.hide()

        chat_window = ChatWindow()
        chat_window.setWindowTitle("现场调试助手")
        chat_window.resize(430, 760)
        chat_window.setMinimumSize(360, 620)
        chat_window.load(QUrl(chat_url))

        class Bridge(QObject):
            openRequested = Signal()

            @Slot()
            def openChat(self) -> None:
                self.openRequested.emit()

            @Slot()
            def hideChat(self) -> None:
                chat_window.hide()

            @Slot()
            def startDrag(self) -> None:
                handle = float_window.windowHandle()
                if handle is not None:
                    handle.startSystemMove()

            @Slot()
            def quitApp(self) -> None:
                app.quit()

        bridge = Bridge()
        bridge.openRequested.connect(chat_window.show)
        bridge.openRequested.connect(chat_window.raise_)
        bridge.openRequested.connect(chat_window.activateWindow)

        float_window = QWebEngineView()
        float_window.setWindowTitle("现场调试助手浮窗")
        float_window.setFixedSize(72, 72)
        float_window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        float_window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        float_window.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        float_window.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        float_window.setWindowFlag(Qt.WindowType.Tool, True)
        float_window.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)

        float_channel = QWebChannel(float_window.page())
        float_channel.registerObject("desktopBridge", bridge)
        float_window.page().setWebChannel(float_channel)
        chat_channel = QWebChannel(chat_window.page())
        chat_channel.registerObject("desktopBridge", bridge)
        chat_window.page().setWebChannel(chat_channel)
        float_url = base_url.replace("index.html", "float.html")
        float_window.load(QUrl(float_url))

        screen = app.primaryScreen().availableGeometry()
        float_window.move(screen.right() - 92, screen.bottom() - 100)
        float_window.show()
        sys.exit(app.exec())
