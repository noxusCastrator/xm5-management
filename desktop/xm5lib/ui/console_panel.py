"""The 指令 tab beside 标签: a plain terminal. One dark surface; the scrollback above, the prompt line
pinned at the bottom (› and the input, separated by a hairline). Each command is echoed after the
prompt glyph, its output follows in grey, errors start with a red ✗, a blank line separates commands.

Enter runs (Shift+Enter starts a new line for a multi-line script), Tab completes a command name
(several matches are listed like a shell does), ↑/↓ walk the history (kept in the settings),
Ctrl+L clears, Esc clears the input or stops a running script. The page is exactly as tall as the
标签 page, so the right-hand column keeps its height when the tab changes. ``attach(console)``
connects the dispatcher (``console_live.LiveConsole``) once the window's pages exist.
"""
from __future__ import annotations

import html
import re

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QFontDatabase, QTextCursor
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QSizePolicy, QVBoxLayout, QWidget

from . import style
from .i18n import tr

HISTORY = 200
BG = "#0a0a0a"
TEXT = "#e6e6e6"          # commands
OUT = "#a6a6a6"           # output
DIM = "#6a6a6a"           # hints
ERR = "#e5534b"


def mono(size: float = 9.0) -> QFont:
    f = QFontDatabase.systemFont(QFontDatabase.FixedFont)
    f.setFamilies(["Cascadia Mono", "Consolas", "Microsoft YaHei UI", "Sarasa Mono SC", "DejaVu Sans Mono",
                   "Noto Sans Mono CJK SC", f.family()])
    f.setPointSizeF(size)
    f.setStyleHint(QFont.Monospace)
    return f


class CommandInput(QPlainTextEdit):
    submitted = Signal(str)
    complete = Signal(str)            # the word before the cursor
    clearScreen = Signal()
    escape = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("commandInput")
        self.setTabChangesFocus(False)
        self.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setPlaceholderText(tr("输入指令，Enter 执行"))
        self.history: list[str] = []
        self._pos = 0
        self._draft = ""
        self.document().setDocumentMargin(2)
        self.document().contentsChanged.connect(self._fit)
        self._fit()

    def _fit(self) -> None:
        lines = max(1, min(4, int(self.document().size().height())))
        self.setFixedHeight(int(self.fontMetrics().lineSpacing() * lines + 8))

    def keyPressEvent(self, e):
        k, mods = e.key(), e.modifiers()
        if k in (Qt.Key_Return, Qt.Key_Enter) and not (mods & Qt.ShiftModifier):
            text = self.toPlainText()
            if text.strip():
                self.submitted.emit(text)
            return
        if k == Qt.Key_L and mods & Qt.ControlModifier:
            self.clearScreen.emit()
            return
        if k == Qt.Key_Escape:
            if self.toPlainText():
                self.clear()
            else:
                self.escape.emit()
            return
        if k == Qt.Key_Tab:
            c = self.textCursor()
            m = re.search(r"[A-Za-z_][A-Za-z_0-9]*$", self.toPlainText()[:c.position()])
            if m:
                self.complete.emit(m.group())
            return
        c = self.textCursor()
        if k == Qt.Key_Up and self.history and c.blockNumber() == 0:
            self._walk(-1)
            return
        if k == Qt.Key_Down and self.history and c.blockNumber() == self.blockCount() - 1 and self._pos < len(self.history):
            self._walk(1)
            return
        super().keyPressEvent(e)

    def _walk(self, d: int) -> None:
        if self._pos == len(self.history):
            self._draft = self.toPlainText()
        self._pos = max(0, min(len(self.history), self._pos + d))
        self.setPlainText(self.history[self._pos] if self._pos < len(self.history) else self._draft)
        self.moveCursor(QTextCursor.End)

    def remember(self, text: str) -> None:
        text = text.strip()
        if text and (not self.history or self.history[-1] != text):
            self.history.append(text)
            del self.history[:-HISTORY]
        self._pos = len(self.history)
        self._draft = ""

    def replace_word(self, word: str, full: str) -> None:
        c = self.textCursor()
        c.movePosition(QTextCursor.Left, QTextCursor.KeepAnchor, len(word))
        c.insertText(full)
        self.setTextCursor(c)


class ConsolePage(QWidget):
    """``match`` (a widget) gives the height to keep: the 标签 page."""
    busyChanged = Signal(bool)

    def __init__(self, match: QWidget | None = None):
        super().__init__()
        self.setObjectName("consolePage")
        self.match = match
        self.console = None
        self.prefs = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 4, 0, 0)
        outer.setSpacing(0)
        self.surface = QFrame()
        self.surface.setObjectName("terminal")
        outer.addWidget(self.surface, 1)
        lay = QVBoxLayout(self.surface)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.log = QPlainTextEdit()
        self.log.setObjectName("commandLog")
        self.log.setReadOnly(True)
        self.log.setFont(mono(9))
        self.log.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.log.setMaximumBlockCount(4000)
        self.log.setFrameShape(QFrame.NoFrame)
        self.log.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.log.document().setDocumentMargin(8)
        self.log.setFocusPolicy(Qt.ClickFocus)
        lay.addWidget(self.log, 1)
        rule = QFrame()
        rule.setObjectName("terminalRule")
        rule.setFixedHeight(1)
        lay.addWidget(rule)
        row = QHBoxLayout()
        row.setContentsMargins(8, 4, 6, 4)
        row.setSpacing(4)
        self.prompt = QLabel("›")
        self.prompt.setObjectName("terminalPrompt")
        self.prompt.setFont(mono(10))
        row.addWidget(self.prompt, 0, Qt.AlignTop)
        self.input = CommandInput()
        self.input.setFont(mono(9))
        self.input.setFrameShape(QFrame.NoFrame)
        self.input.submitted.connect(self._run)
        self.input.complete.connect(self._complete)
        self.input.clearScreen.connect(self.clear)
        self.input.escape.connect(self.stop)
        row.addWidget(self.input, 1)
        lay.addLayout(row)
        self.setStyleSheet(
            f"QFrame#terminal {{ background: {BG}; border: 1px solid {style.LINE}; border-radius: 4px; }}"
            f"QFrame#terminalRule {{ background: #1e1e1e; border: none; }}"
            f"QPlainTextEdit#commandLog {{ background: transparent; border: none; color: {OUT}; }}"
            f"QPlainTextEdit#commandInput {{ background: transparent; border: none; color: {TEXT}; padding: 0; }}"
            f"QLabel#terminalPrompt {{ color: {style.ACCENT}; background: transparent; padding-top: 1px; }}")
        self._say("info", tr("XM5 指令 · help() 看用法"))

    def sizeHint(self) -> QSize:
        h = self.match.sizeHint().height() if self.match is not None else 420
        return QSize(super().sizeHint().width(), h)

    def minimumSizeHint(self) -> QSize:
        return QSize(super().minimumSizeHint().width(), 160)

    def attach(self, console, prefs=None) -> None:
        self.console, self.prefs = console, prefs
        console.line.connect(self._say)
        console.busyChanged.connect(self._busy)
        if prefs is not None:
            saved = prefs.get("console_history") or []
            self.input.history = [str(x) for x in saved][-HISTORY:]
            self.input._pos = len(self.input.history)

    def _busy(self, on: bool) -> None:
        self.prompt.setText("…" if on else "›")
        self.busyChanged.emit(on)

    # ---- running
    def _run(self, text: str) -> None:
        if self.console is None:
            return
        self.input.remember(text)
        if self.prefs is not None:
            self.prefs.set("console_history", list(self.input.history))
        if self.console.run(text):
            self.input.clear()

    def stop(self) -> None:
        if self.console is not None:
            self.console.stop()

    def clear(self) -> None:
        self.log.clear()

    def _complete(self, word: str) -> None:
        if self.console is None:
            return
        names = sorted(self.console.registry.commands)
        hits = [n for n in names if n.startswith(word.casefold())]
        live = [n for n in hits if self.console.connected(n)]
        hits = live or hits
        if len(hits) == 1:
            self.input.replace_word(word, hits[0] + "(")
        elif hits:
            common = hits[0]
            for n in hits[1:]:
                while not n.startswith(common):
                    common = common[:-1]
            if len(common) > len(word):
                self.input.replace_word(word, common)
            self._say("info", "  ".join(hits[:40]))

    def _say(self, kind: str, text: str) -> None:
        def span(color, body):
            body = html.escape(body).replace("\n", "<br>")
            return f'<span style="color:{color}; white-space:pre-wrap">{body}</span>'
        if kind == "input":
            if self.log.document().characterCount() > 1:
                self.log.appendPlainText("")                     # a blank line between commands
            lines = text.splitlines() or [""]
            html_ = "<br>".join(span(style.ACCENT, ("› " if i == 0 else "  ")) + span(TEXT, l) for i, l in enumerate(lines))
        elif kind == "error":
            first, _, rest = text.partition("\n")
            html_ = span(ERR, "✗ ") + span("#e8b4b0", first.removeprefix("✗ ")) + ("<br>" + span(OUT, rest) if rest else "")
        elif kind == "info":
            html_ = span(DIM, text)
        else:
            html_ = span(OUT, text)
        self.log.appendHtml(html_)
        self.log.moveCursor(QTextCursor.End)
        bar = self.log.verticalScrollBar()
        QTimer.singleShot(0, lambda: bar.setValue(bar.maximum()))     # after the wrapped lines are laid out


__all__ = ["ConsolePage", "CommandInput"]
