"""设置: look, language, the player's output and 更新曲库. Music folders are managed on the 文件夹 page,
the Peace folder on the PEQ page."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QPushButton, QRadioButton, QVBoxLayout, QWidget)

from . import i18n
from . import player as player_mod
from . import style
from .library_pages import header
from .i18n import tr


class SettingsPage(QWidget):
    def __init__(self, ctl):
        super().__init__()
        self.ctl = ctl
        self.setObjectName("page")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        h, _, _ = header(tr("系统设置"))
        lay.addWidget(h)
        body = QVBoxLayout()
        body.setContentsMargins(24, 0, 24, 16)
        lay.addLayout(body, 1)

        look = QGroupBox(tr("外观"))
        ll = QHBoxLayout(look)
        self.finish = {}
        self._finish_group = QButtonGroup(self)
        for key, label in (("titanium", tr("钛版外壳")), ("aluminum", tr("铝版外壳（黑）"))):
            r = QRadioButton(label)
            r.setObjectName(f"finish_{key}")
            r.toggled.connect(lambda on, k=key: on and ctl.apply_theme(k, save=True))
            self.finish[key] = r
            self._finish_group.addButton(r)
            ll.addWidget(r)
        ll.addStretch(1)
        body.addWidget(look)

        lang = QGroupBox(tr("语言"))
        gl = QHBoxLayout(lang)
        self.language = {}
        self._lang_group = QButtonGroup(self)
        current = ctl.settings_value("language") or "zh"
        for code, label in i18n.LANGUAGES:                  # each in its own language, like the XM5
            r = QRadioButton(label)
            r.setObjectName(f"lang_{code}")
            r.setChecked(code == current)
            r.toggled.connect(lambda on, c=code: on and self._language(c))
            self.language[code] = r
            self._lang_group.addButton(r)
            gl.addWidget(r)
        gl.addSpacing(16)
        self.lang_note = QLabel("")
        self.lang_note.setProperty("muted", True)
        gl.addWidget(self.lang_note)
        gl.addStretch(1)
        body.addWidget(lang)

        pg = QGroupBox(tr("播放器"))
        pf = QFormLayout(pg)
        self.output = QComboBox()
        self.output.setObjectName("playerOutput")
        self.output.setMinimumWidth(320)
        self.output.activated.connect(self._choose_output)
        self.tone = QPushButton(tr("播放测试音"))
        self.tone.setObjectName("playerTone")
        self.tone.clicked.connect(self._test_tone)
        again = QPushButton(tr("刷新设备"))
        again.clicked.connect(self._refresh_devices)
        row = QHBoxLayout()
        row.addWidget(self.output, 1)
        row.addWidget(again)
        row.addWidget(self.tone)
        pf.addRow(tr("输出选择"), row)
        self.exclusive = QCheckBox(tr("独占模式"))
        self.exclusive.setToolTip(tr("WASAPI exclusive：绕过 Windows 混音器，采样率跟着文件走；其他程序这时不能出声"))
        self.exclusive.setObjectName("playerExclusive")
        self.exclusive.setChecked(bool(ctl.settings_value("player_exclusive")))
        self.exclusive.toggled.connect(self._exclusive)
        pf.addRow("", self.exclusive)
        body.addWidget(pg)

        lib = QGroupBox(tr("曲库"))
        lb = QVBoxLayout(lib)
        row = QHBoxLayout()
        self.rescan = QPushButton(tr("更新曲库"))
        self.rescan.setObjectName("primary")
        self.rescan.setCursor(Qt.PointingHandCursor)
        self.rescan.setToolTip("F5")
        self.rescan.clicked.connect(lambda: ctl.start_scan())
        row.addWidget(self.rescan)
        row.addSpacing(12)
        self.scan_info = QLabel("")
        self.scan_info.setObjectName("scanInfo")
        self.scan_info.setWordWrap(True)
        row.addWidget(self.scan_info, 1)
        lb.addLayout(row)
        self.ignore_artist = QCheckBox(tr("专辑无视歌手"))
        self.ignore_artist.setObjectName("albumsIgnoreArtist")
        self.ignore_artist.setToolTip(tr("同名专辑不再按歌手分开，一张专辑可以有多位歌手"))
        self.ignore_artist.setChecked(bool(ctl.settings_value("albums_ignore_artist")))
        self.ignore_artist.toggled.connect(ctl.set_albums_ignore_artist)
        lb.addWidget(self.ignore_artist)
        body.addWidget(lib)
        body.addStretch(1)

        engine = ctl.player.backend
        if hasattr(engine, "outputsChanged"):
            engine.outputsChanged.connect(self.refresh_player)
        ctl.state.busyChanged.connect(lambda _k: self.refresh_library())
        ctl.state.jobProgress.connect(self._progress)
        ctl.state.tracksChanged.connect(self.refresh_library)

    # ---- language
    def _language(self, code: str) -> None:
        if code == (self.ctl.settings_value("language") or "zh"):
            self.lang_note.setText("")
            return
        self.ctl.set_setting("language", code)
        self.lang_note.setText(tr("重新打开程序后生效"))
        if not self.ctl.quiet and self.ctl.ask(tr("语言"), tr("语言会在程序重新打开后生效。现在重新打开吗？"), tr("重新打开"), tr("稍后")):
            self.ctl.restart_app()

    # ---- library
    def refresh_library(self) -> None:
        st = self.ctl.state
        roots = self.ctl.music_roots()
        self.rescan.setText(tr("取消扫描") if st.busy == "scan" else tr("更新曲库"))
        self.rescan.setEnabled(bool(roots) and st.busy in ("", "scan"))
        if st.busy == "scan":
            self.scan_info.setText(tr("扫描中…"))
        elif not roots:
            self.scan_info.setText(tr("还没有音乐文件夹（在「文件夹」页面添加）"))
        else:
            self.scan_info.setText(tr("{0} 个音乐文件夹 · {1} 首 · {2} 张专辑", len(roots), len(self.ctl.view.tracks), len(self.ctl.view.albums)))

    def _progress(self, p) -> None:
        if self.ctl.state.busy == "scan" and isinstance(p, dict) and p.get("total"):
            self.scan_info.setText(tr("正在更新第 {0} 首 / 共 {1} 首", p.get('current', 0), p['total']))

    # ---- player
    def refresh_player(self) -> None:
        pl = self.ctl.player
        self.output.clear()
        outs = pl.outputs()
        current = str(self.ctl.settings_value("player_device") or "")
        backend = pl.engine == "backend"
        if backend and not current:
            self.output.addItem(tr("（还没有选：第一次播放时会问）"), "")
        default = next((name for _, name, d in outs if d), "")
        self.output.addItem(tr("跟随 Windows 默认设备（现在：{0}）", default) if default else tr("跟随 Windows 默认设备"),
                            "auto" if backend else "")
        for dev_id, name, _ in outs:
            self.output.addItem(name, dev_id)
        i = self.output.findData(current)
        if i < 0 and current and current != "auto":
            self.output.addItem(tr("{0}（现在不在列表里）", current), current)
            i = self.output.count() - 1
        self.output.setCurrentIndex(i if i >= 0 else 0)
        self.output.setEnabled(pl.available)
        self.output.setToolTip("" if pl.available else pl.reason)
        self.exclusive.setEnabled(pl.engine in ("mpv", "backend"))
        self.tone.setVisible(True)                 # the backend owns a bounded test-tone operation

    def _exclusive(self, on: bool) -> None:
        self.ctl.set_setting("player_exclusive", bool(on))
        self.ctl.player.set_exclusive(on)

    def _refresh_devices(self) -> None:
        engine = self.ctl.player.backend
        if hasattr(engine, "refresh_outputs"):
            engine.refresh_outputs()
        self.refresh_player()

    def _choose_output(self, _i) -> None:
        dev = self.output.currentData() or ""
        if not dev and self.ctl.player.engine == "backend":
            return
        self.ctl.set_setting("player_device", dev)
        self.ctl.player.set_output(dev)

    def _test_tone(self) -> None:
        if not self.ctl._player_ready():
            return
        path = player_mod.write_tone(self.ctl.home / "logs" / "test-tone.wav")
        self.ctl.player.play_tracks([{"path": str(path), "title": tr("测试音 660 Hz"), "artist": "XM5 Library",
                                      "duration": 1.2}], 0)

    def refresh(self) -> None:
        engine = self.ctl.player.backend
        if hasattr(engine, "refresh_outputs") and not engine.devices:
            engine.refresh_outputs()            # a silent mpv lists the outputs; only when Settings is opened
        self.refresh_player()
        self.refresh_library()
        self.ignore_artist.blockSignals(True)
        self.ignore_artist.setChecked(bool(self.ctl.settings_value("albums_ignore_artist")))
        self.ignore_artist.blockSignals(False)

    def show_theme(self, theme: str) -> None:
        for key, r in self.finish.items():
            r.blockSignals(True)
            r.setChecked(key == theme)
            r.blockSignals(False)
