import sys
from PySide6.QtCore import Qt, QThread, Signal, QSize, QTimer, QPoint
from PySide6.QtGui import QPixmap, QAction, QPainter, QFontMetrics, QFont, QCursor
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QVBoxLayout, QWidget,
    QSystemTrayIcon, QMenu, QLabel
)
from mpd import MPDClient, MPDError

from qfluentwidgets import (
    CardWidget, BodyLabel, CaptionLabel,
    TransparentToolButton, FluentIcon as FIF,
    setTheme, Theme
)


# --- Continuous Loop Marquee Label ---
class MarqueeLabel(QLabel):
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.offset = 0.0
        self.is_overflowing = False
        self.text_width = 0
        self.space_width = 0

        self.timer = QTimer(self)
        self.timer.setInterval(30)
        self.timer.timeout.connect(self.update_offset)

    def setText(self, text):
        if self.text() == text:
            return
        super().setText(text)
        self.check_overflow()

    def check_overflow(self):
        fm = QFontMetrics(self.font())
        self.text_width = fm.horizontalAdvance(self.text())
        self.space_width = fm.horizontalAdvance("      ")

        if self.text_width > self.width():
            self.is_overflowing = True
            self.offset = 0.0
            if not self.timer.isActive():
                self.timer.start()
        else:
            self.is_overflowing = False
            self.timer.stop()
            self.offset = 0.0
        self.update()

    def update_offset(self):
        if not self.is_overflowing:
            return

        self.offset += 1.2
        total_loop_width = self.text_width + self.space_width
        if self.offset >= total_loop_width:
            self.offset = 0.0

        self.update()

    def paintEvent(self, event):
        if not self.is_overflowing:
            super().paintEvent(event)
        else:
            painter = QPainter(self)
            painter.setPen(self.palette().color(self.foregroundRole()))
            painter.setFont(self.font())

            x = int(self.offset) - self.text_width - self.space_width
            y = self.height() - 4

            painter.drawText(x, y, self.text())
            painter.drawText(x + self.text_width + self.space_width, y, self.text())


# --- MPD Async Worker Thread ---
class MPDWorker(QThread):
    status_updated = Signal(dict)
    song_updated = Signal(dict)
    cover_art_updated = Signal(bytes)
    connection_changed = Signal(bool)

    def __init__(self, host="127.0.0.1", port=6600):
        super().__init__()
        self.host = host
        self.port = port
        self.client = MPDClient()
        self.running = True

    def run(self):
        while self.running:
            try:
                self.client.connect(self.host, self.port)
                self.connection_changed.emit(True)

                while self.running:
                    status = self.client.status()
                    song = self.client.currentsong()
                    self.status_updated.emit(status)
                    self.song_updated.emit(song)

                    if "file" in song:
                        try:
                            art_data = self.client.readpicture(song["file"])
                            if art_data and "binary" in art_data:
                                self.cover_art_updated.emit(art_data["binary"])
                            else:
                                self.cover_art_updated.emit(b"")
                        except MPDError:
                            self.cover_art_updated.emit(b"")

                    self.client.idle("player", "mixer")

            except (MPDError, ConnectionRefusedError, OSError):
                if self.running:
                    self.connection_changed.emit(False)
                    self.msleep(3000)

    def stop(self):
        self.running = False
        try:
            self.client.close()
            self.client.disconnect()
        except Exception:
            pass

    def send_command(self, func, *args):
        try:
            cmd_client = MPDClient()
            cmd_client.connect(self.host, self.port)
            getattr(cmd_client, func)(*args)
            cmd_client.disconnect()
        except Exception as e:
            print(f"MPD Command Error ({func}): {e}")


# --- Taskbar Square Flyout Widget ---
class FluentMPDFlyout(QWidget):
    def __init__(self, tray_icon: QSystemTrayIcon):
        super().__init__()
        self.tray_icon = tray_icon
        self.opened_by_hover = False
        setTheme(Theme.AUTO)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)

        self.worker = MPDWorker()
        self.init_ui()
        self.bind_signals()
        self.init_hover_timers()
        self.worker.start()

    def init_ui(self):
        self.setFixedSize(210, 240)

        self.card = CardWidget(self)
        self.card.setFixedSize(210, 240)
        self.card.setStyleSheet("""
            CardWidget {
                background-color: rgba(32, 32, 32, 0.95);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 10px;
            }
        """)

        main_layout = QVBoxLayout(self.card)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(8)

        # 1. TOP: Album Cover Art
        cover_container_layout = QHBoxLayout()
        cover_container_layout.addStretch()
        self.cover_label = BodyLabel(self.card)
        self.cover_label.setFixedSize(120, 120)
        self.cover_label.setStyleSheet(
            "background-color: rgba(0, 0, 0, 0.2); border-radius: 6px;"
        )
        self.cover_label.setScaledContents(True)
        cover_container_layout.addWidget(self.cover_label)
        cover_container_layout.addStretch()

        # 2. MIDDLE: Song Info
        info_layout = QVBoxLayout()
        info_layout.setSpacing(1)
        info_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        title_font = QFont("Segoe UI", 10, QFont.Weight.Bold)
        artist_font = QFont("Segoe UI", 9)

        self.title_label = MarqueeLabel("Not Connected", self.card)
        self.title_label.setFont(title_font)
        self.title_label.setStyleSheet("color: white;")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_label.setFixedWidth(190)

        self.artist_label = CaptionLabel("Waiting for MPD...", self.card)
        self.artist_label.setFont(artist_font)
        self.artist_label.setStyleSheet("color: rgba(255, 255, 255, 0.6);")
        self.artist_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.artist_label.setFixedWidth(190)

        info_layout.addWidget(self.title_label)
        info_layout.addWidget(self.artist_label)

        # 3. BOTTOM: Playback Controls
        controls_layout = QHBoxLayout()
        controls_layout.setSpacing(10)
        controls_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Using clean text symbols for media playback buttons to guarantee compatibility
        self.btn_prev = TransparentToolButton(self.card)
        self.btn_prev.setText("◀")
        
        self.btn_play = TransparentToolButton(FIF.PLAY, self.card)
        
        self.btn_next = TransparentToolButton(self.card)
        self.btn_next.setText("▶")

        for btn in (self.btn_prev, self.btn_play, self.btn_next):
            btn.setFixedSize(32, 32)

        controls_layout.addWidget(self.btn_prev)
        controls_layout.addWidget(self.btn_play)
        controls_layout.addWidget(self.btn_next)

        main_layout.addLayout(cover_container_layout)
        main_layout.addLayout(info_layout)
        main_layout.addLayout(controls_layout)

    def init_hover_timers(self):
        self.hover_poll_timer = QTimer(self)
        self.hover_poll_timer.setInterval(60)
        self.hover_poll_timer.timeout.connect(self._poll_cursor)
        self.hover_poll_timer.start()

        self.hover_open_timer = QTimer(self)
        self.hover_open_timer.setSingleShot(True)
        self.hover_open_timer.setInterval(120)
        self.hover_open_timer.timeout.connect(self._open_from_hover)

        self.hover_hide_timer = QTimer(self)
        self.hover_hide_timer.setSingleShot(True)
        self.hover_hide_timer.setInterval(150)
        self.hover_hide_timer.timeout.connect(self._hide_from_hover)

    def _is_cursor_over_tray(self, pos: QPoint) -> bool:
        tray_rect = self.tray_icon.geometry()
        return not tray_rect.isEmpty() and tray_rect.adjusted(-2, -2, 2, 2).contains(pos)

    def _is_cursor_over_flyout(self, pos: QPoint) -> bool:
        if not self.isVisible():
            return False
        # Slight margin bridges the 8px gap between tray icon and flyout
        return self.frameGeometry().adjusted(-4, -4, 4, 12).contains(pos)

    def _poll_cursor(self):
        pos = QCursor.pos()
        over_tray = self._is_cursor_over_tray(pos)
        over_flyout = self._is_cursor_over_flyout(pos)

        if over_tray:
            self.hover_hide_timer.stop()
            if not self.isVisible() and not self.hover_open_timer.isActive():
                self.hover_open_timer.start()
        elif over_flyout:
            self.hover_open_timer.stop()
            self.hover_hide_timer.stop()
        else:
            self.hover_open_timer.stop()
            if self.isVisible() and self.opened_by_hover and not self.hover_hide_timer.isActive():
                self.hover_hide_timer.start()

    def _open_from_hover(self):
        if not self.isVisible():
            self.opened_by_hover = True
            self._position_and_show(activate=False)

    def _hide_from_hover(self):
        pos = QCursor.pos()
        if not self._is_cursor_over_tray(pos) and not self._is_cursor_over_flyout(pos):
            self.hide()
            self.opened_by_hover = False

    def bind_signals(self):
        self.worker.song_updated.connect(self.on_song_change)
        self.worker.status_updated.connect(self.on_status_change)
        self.worker.cover_art_updated.connect(self.on_cover_change)
        self.worker.connection_changed.connect(self.on_connection_change)

        self.btn_play.clicked.connect(lambda: self.worker.send_command("pause"))
        self.btn_prev.clicked.connect(lambda: self.worker.send_command("previous"))
        self.btn_next.clicked.connect(lambda: self.worker.send_command("next"))

    def on_song_change(self, song: dict):
        self.title_label.setText(song.get("title", song.get("file", "Unknown")))
        self.artist_label.setText(song.get("artist", "Unknown Artist"))

    def on_status_change(self, status: dict):
        state = status.get("state", "stop")
        self.btn_play.setIcon(FIF.PAUSE if state == "play" else FIF.PLAY)

    def on_cover_change(self, binary_data: bytes):
        if binary_data:
            pixmap = QPixmap()
            pixmap.loadFromData(binary_data)
            self.cover_label.setPixmap(pixmap)
        else:
            self.cover_label.clear()

    def on_connection_change(self, connected: bool):
        if not connected:
            self.title_label.setText("Disconnected")
            self.artist_label.setText("Reconnecting...")

    def _position_and_show(self, activate: bool):
        tray_rect = self.tray_icon.geometry()
        screen_geometry = QApplication.primaryScreen().geometry()

        if not tray_rect.isEmpty():
            x = tray_rect.x() + (tray_rect.width() // 2) - (self.width() // 2)
            y = tray_rect.y() - self.height() - 8
        else:
            x = screen_geometry.width() - self.width() - 16
            y = screen_geometry.height() - self.height() - 56

        if x < screen_geometry.x():
            x = screen_geometry.x() + 8
        elif x + self.width() > screen_geometry.x() + screen_geometry.width():
            x = screen_geometry.x() + screen_geometry.width() - self.width() - 8

        self.move(x, y)
        self.show()
        self.raise_()
        if activate:
            self.activateWindow()

    def toggle_position_above_tray(self):
        if self.isVisible() and not self.opened_by_hover:
            self.hide()
            return

        self.opened_by_hover = False
        self._position_and_show(activate=True)

    def closeEvent(self, event):
        self.hover_poll_timer.stop()
        self.hover_open_timer.stop()
        self.hover_hide_timer.stop()
        self.worker.stop()
        self.worker.wait(1000)
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    tray_icon = QSystemTrayIcon(FIF.MUSIC.icon(Theme.DARK), app)
    widget = FluentMPDFlyout(tray_icon)

    tray_menu = QMenu()
    quit_action = QAction("Quit", app)

    def clean_exit():
        widget.close()
        app.quit()

    quit_action.triggered.connect(clean_exit)

    tray_menu.addAction(quit_action)
    tray_icon.setContextMenu(tray_menu)

    tray_icon.activated.connect(
        lambda reason: widget.worker.send_command("pause")
        if reason == QSystemTrayIcon.ActivationReason.Trigger else None
    )

    tray_icon.show()
    sys.exit(app.exec())
