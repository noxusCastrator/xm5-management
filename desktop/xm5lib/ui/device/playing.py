"""The playing page (``playing_page``) with its queue (``playerqueue_dialog``) and ⋯ (``playermore_dialog``).

Cover, lyrics and information slide under the title; the bar below has the time, the slider (with the AB
marks), 播放模式, 收藏, ⏮ ⏯ ⏭, ⋯ and the queue. The VU meter (the bar's top-right button) is the stock
dial; the desktop player hands out no levels, so its needle rests.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPixmap

from ...core import covers
from .. import catalog
from ..i18n import tr
from ..player import format_ms
from . import awtk
from .awtk import make
from .core import ROW, Dialog, Page, RowSource, background
from .library import (FORMAT_ICON, ArtistInfoPage, JumpInfoPage, PlaylistsPage, artist_of, ext, human_size,
                      title_of)
from .screen import font

MODE_IMAGE = {"order": "btn_listplay", "repeat_all": "btn_repeatall", "repeat_one": "btn_repeattrack",
              "shuffle": "btn_shuffle"}
SPEEDS = ("1.0x", "1.25x", "1.5x", "2.0x")


class PlayingPage(Page):
    ui = "playing_page"
    fullscreen = False

    def build(self) -> None:
        r, p = self.root, self.win.player
        r["img_dlna"].visible = False
        r["btn_pulldown"].on_click = self.dev.back
        r["btn_album"].on_click = self.toggle_uv
        r["view_uv"].visible = False
        slide = r["slide_view"]
        slide.page = 0
        ind = r["slide_indicator"]
        ind.count = 3
        ind.props["size"] = "10"
        slide.on_page = lambda i: (setattr(ind, "value", float(i)), self.dev.reset_idle())
        r["img_mode"].on_click = self.cycle_mode
        r["btn_fav"].on_click = self.toggle_fav
        r["btn_perv"].on_click = p.previous
        r["btn_next"].on_click = p.next
        r["btn_pause"].on_click = p.toggle
        r["btn_more"].on_click = lambda: self.dev.show_dialog(PlayerMoreDialog(self.dev, self))
        r["btn_playlists"].on_click = lambda: self.dev.show_dialog(QueueDialog(self.dev))
        r["image_wait"].visible = False
        r["slider_play"].on_change = self.seek
        lrc = r["view_lrc"]                                  # the firmware's lyric list, drawn by hand here
        self.lyric = make("view", lrc.raw, "lyric_lines", parent=r["view_lyric"])
        lrc.visible = False
        self.lyric.paint_hook = self._paint_lyrics
        r["gauge"].paint_hook = self._paint_gauge
        r["gauge_pointer"].visible = False
        self.cues: list[tuple[int, str]] = []
        self.lyric_track = None
        self.cover_for = None
        self.big_cover = None
        self.seeking = None

    def lyric_mode(self) -> bool:
        """显示 › 歌词模式: the screen stays on while the lyrics are shown."""
        return int(getattr(self.root["slide_view"], "page", 0)) == 1 and bool(int(self.dev.cfg("LYRICMODE")))

    def toggle_uv(self) -> None:
        v = self.root["view_uv"]
        v.visible = not v.visible
        self.dev.screen.update()

    def seek(self, value: float, final: bool) -> None:
        p = self.win.player
        if not p.current:
            return
        self.seeking = None if final else value
        if final:
            p.seek(int(value))
        self.root["label_playtime"].text = format_ms(int(value))

    def cycle_mode(self) -> None:
        self.win.player.cycle_mode()

    def toggle_fav(self) -> None:
        t = self.win.player.current
        if t is None:
            return
        on = self.win.favorites.toggle([t["path"]])
        self.root["btn_fav"].style = "btn_faved_style" if on else "btn_fav_style"
        self.dev.screen.update()

    def changed(self, what: str) -> None:
        super().changed(what)
        r, p = self.root, self.win.player
        t = p.current
        if t is None:
            if what in ("track", "queue"):
                QTimer.singleShot(0, self._leave)
            return
        if what in ("track", "library", "covers", "favorites", "mode", "state", "queue"):
            r["scrlabel_title"].text = title_of(t)
            r["scrlabel_artist"].text = artist_of(t) or self.s("player_unknown")
            r["scrlabel_album"].text = t.get("album") or self.s("player_unknown")
            r["hscroll_uvalbum"].text = t.get("album") or ""
            r["hscroll_uvsong"].text = title_of(t)
            e = ext(t)
            info = r["img_info"]
            info.image = FORMAT_ICON.get(e, "")
            info.visible = bool(info.image)
            sr, bits, br = t.get("sample_rate") or 0, t.get("bits") or 0, t.get("bitrate") or 0
            parts = []
            if sr:
                parts.append(f"{sr / 1000:g}kHz")
            if br:
                parts.append(f"{int(br) // 1000 if br > 10000 else int(br)}kbps")
            r["scrlabel_info"].text = "  ".join(parts)
            hires = catalog.is_hires(t)
            r["img_hr"].visible = hires
            lb = r["label_bit"]
            lb.text = f"{bits}bit" if bits else ""
            r["img_mqa"].visible = False
            awtk.layout(r["list_item_info"], r["list_item_info"].w, r["list_item_info"].h, own=False)
            awtk.layout(r["list_item_pic"], r["list_item_pic"].w, r["list_item_pic"].h, own=False)
            unknown = self.s("player_unknown")
            r["label_album"].text = f"{self.s('album')}: {t.get('album') or unknown}"
            r["label_size"].text = f"{self.s('player_filesize')}: {human_size(t.get('size')) or unknown}"
            r["label_encode"].text = f"{self.s('player_encoding')}: {(t.get('codec') or e).upper()}"
            r["label_genre"].text = f"{self.s('genre')}: {t.get('genre') or unknown}"
            r["label_year"].text = f"{self.s('player_era')}: {str(t.get('date') or '')[:4] or unknown}"
            loc = r["label_location"]
            loc.text = f"{self.s('player_location')}: {t.get('path', '')}"
            loc.props["line_wrap"] = "true"
            r["img_mode"].image = MODE_IMAGE.get(p.mode, "btn_listplay")
            r["btn_fav"].style = "btn_faved_style" if self.win.favorites.is_fav(t["path"]) else "btn_fav_style"
            r["btn_pause"].style = "playing_playing_style" if p.state == "playing" else "playing_stop_style"
            order = p.order or list(range(len(p.queue)))
            pos = order.index(p.index) + 1 if p.index in order else p.index + 1
            r["label_index"].text = f"{pos}/{len(p.queue)}"
            self._cover(t)
            self._lyrics(t)
        if what in ("position", "track", "state"):
            sl = r["slider_play"]
            sl.max = max(1, p.duration)
            if self.seeking is None and not sl.pressed:
                sl.value = min(p.position, sl.max)
                r["label_playtime"].text = format_ms(p.position)
            r["label_playlen"].text = format_ms(p.duration)
            self._ab_marks()

    def _leave(self) -> None:
        if self.win.player.current is None and self.dev.stack and self.dev.stack[-1] is self:
            self.dev.back()

    # ---- AB repeat marks on the slider
    def _ab_marks(self) -> None:
        r = self.root
        sl = r["slider_play"]
        ab = self.dev.ab
        for name, k in (("img_repeata", 0), ("img_repeatb", 1)):
            img = r[name]
            ms = ab[k] if ab and len(ab) > k and ab[k] is not None else None
            img.visible = ms is not None
            if ms is not None:
                img.x = sl.x + sl.w * min(1.0, ms / max(1, sl.max)) - 1
                img.raw = (img.x, img.raw[1], img.raw[2], img.raw[3])

    # ---- cover: the full-size picture from the file (the library keeps small thumbnails)
    def _cover(self, t: dict) -> None:
        img = self.root["img_cover"]
        if self.cover_for == t.get("path"):
            img.pixmap = self.big_cover or self.dev.track_cover(t)
            return
        self.cover_for = t.get("path")
        self.big_cover = None
        img.pixmap = self.dev.track_cover(t)
        path = t.get("path")
        album = self.dev.view.album_of(t)
        paths = [x["path"] for x in album.tracks] if album is not None else [path]
        if path in paths:
            paths.remove(path)
        paths.insert(0, path)

        def load():
            data, _src = covers.source_art(paths)
            return path, data

        def done(res):
            if not res or res[0] != self.cover_for or not res[1]:
                return
            pix = QPixmap()
            if pix.loadFromData(res[1]):
                self.big_cover = (pix.scaled(660, 660, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                                  if pix.width() > 660 else pix)
                self.root["img_cover"].pixmap = self.big_cover
                self.dev.screen.update()
        background(load, done)

    # ---- lyrics: what the library has for the song (the desktop's 歌词 tools attach them)
    def _lyrics(self, t: dict) -> None:
        if self.lyric_track == t.get("path"):
            return
        self.lyric_track = t.get("path")
        self.cues = []
        msg = self.root["label_lyricmsg"]
        msg.text = self.s("player_nolyric")
        msg.visible = True
        tid = self.win.state.id_for(t.get("path"))
        backend = self.win.backend
        if not tid or not getattr(backend, "ready", False):
            return
        want = self.lyric_track

        def got(value):
            if want != self.lyric_track or not isinstance(value, dict):
                return
            cues = value.get("cues")
            if not cues:
                text = value.get("normalized_text") or value.get("synced_text") or value.get("plain_text") or ""
                try:
                    from ...core.lyrics import parse_lyrics
                    parsed = parse_lyrics(text.encode("utf-8"), "lrc") if text else {}
                    cues = parsed.get("cues") or [{"time_ms": -1, "text": l}
                                                  for l in (parsed.get("plain_text") or "").splitlines() if l.strip()]
                except Exception:
                    cues = [{"time_ms": -1, "text": l} for l in text.splitlines() if l.strip()]
            self.cues = [(int(c.get("time_ms", -1)), str(c.get("text", ""))) for c in cues if c.get("text") is not None]
            msg.visible = not self.cues
            self.dev.screen.update()
        try:
            backend.query("lyrics.get", {"track_id": tid}, ok=got, err=lambda _e: None)
        except Exception:
            pass

    def _paint_lyrics(self, p, n, rect, st) -> bool:
        if not self.cues:
            return True
        pos = self.seeking if self.seeking is not None else self.win.player.position
        synced = any(t >= 0 for t, _ in self.cues)
        cur = 0
        if synced:
            for i, (ms, _) in enumerate(self.cues):
                if 0 <= ms <= pos:
                    cur = i
        line_h = 46
        p.save()
        p.setClipRect(rect, Qt.IntersectClip)
        p.setFont(font(28))
        top = rect.center().y() - line_h / 2 - cur * line_h if synced else rect.y()
        for i, (_, text) in enumerate(self.cues):
            y = top + i * line_h
            if y + line_h < rect.y() or y > rect.bottom():
                continue
            edge = min(y - rect.y(), rect.bottom() - (y + line_h))
            p.setOpacity(max(0.0, min(1.0, 0.25 + edge / line_h)))
            p.setPen(QColor(255, 219, 149) if synced and i == cur else QColor(255, 255, 255))
            p.drawText(QRectF(rect.x(), y, rect.width(), line_h), int(Qt.AlignCenter | Qt.TextSingleLine), text)
        p.restore()
        return True

    def _paint_gauge(self, p, n, rect, st) -> bool:
        """The VU dial: uv_bg, the uv_table scale and the uv_pointer needle (at rest)."""
        img = self.dev.screen.img
        bg = img.get("uv_bg")
        if bg is not None:
            p.drawPixmap(rect, bg, QRectF(bg.rect()))
        scale = img.get("uv_table")
        if scale is not None:
            p.drawPixmap(QRectF(rect.x() + (rect.width() - scale.width()) / 2, rect.y(), scale.width(),
                                scale.height()), scale, QRectF(scale.rect()))
        needle = img.get("uv_pointer")
        ptr = n.find("gauge_pointer")
        if needle is not None and ptr is not None:
            cx, base = rect.x() + ptr.x + ptr.w / 2, rect.y() + ptr.y + ptr.h
            p.save()
            p.translate(QPointF(cx, base))
            p.rotate(-45)
            p.drawPixmap(QRectF(-needle.width() / 2, -needle.height(), needle.width(), needle.height()), needle,
                         QRectF(needle.rect()))
            p.restore()
        return True


class QueueDialog(Dialog):
    """playerqueue_dialog: the queue from the bottom, the playing song in gold; tap to play."""
    ui = "dialog/playerqueue_dialog"

    def build(self) -> None:
        self.list = self.root["table_client"]
        self.list.bar = self.root["scroll_bar_m"]
        p = self.win.player
        self.order = p.order if p.order and len(p.order) == len(p.queue) else list(range(len(p.queue)))
        if self.list.rows is None:
            self.list.rows = RowSource(len(self.order), 96, self.row)
        else:
            self.list.rows.reset(len(self.order))
        try:
            at = self.order.index(p.index)
        except ValueError:
            at = 0
        self.list.scroll_y = max(0.0, at * 96 - 96 * 2)
        QTimer.singleShot(0, lambda: self.dev.screen.scroll_by(self.list, 0))

    def row(self, i: int) -> awtk.Node:
        p = self.win.player
        qi = self.order[i]
        t = p.queue[qi]
        w = self.list.w
        r = make("button", (0, 0, w, 96), "row", "btn_queuetablerow_style")
        playing = qi == p.index
        style = "srlabel_leftyellow32_style" if playing else "srlabel_leftwhite32_style"
        make("hscroll_label", (30, 14, w - 160, 40), "title", style, parent=r, text=title_of(t), only_focus="true",
             ellipses="true")
        make("hscroll_label", (30, 56, w - 160, 30), "sub", "srlabel_leftgray26_style", parent=r,
             text=artist_of(t) or self.s("player_unknown"), only_focus="true", ellipses="true")
        if playing:
            make("gif", (w - 88, 24, 48, 48), "playing", parent=r, image="playing_gif")
        r.on_click = lambda: self._play(qi)
        return r

    def _play(self, qi: int) -> None:
        p = self.win.player
        self.close()
        if qi == p.index:
            self.dev.playing()
            return
        p.play_tracks(list(p.queue), qi)

    def changed(self, what: str) -> None:
        if what in ("track", "queue"):
            self.build()


class PlayerMoreDialog(Dialog):
    """playermore_dialog: AB复读, 倍速, 添加到歌单, 动态获取封面与歌词, 查看专辑, 查看歌手, 删除歌曲, 取消."""
    ui = "dialog/playermore_dialog"

    def __init__(self, dev, page):
        self.page = page
        super().__init__(dev)

    def build(self) -> None:
        r = self.root
        labels = {"label_ab": "player_abrepeat", "label_forward": "player_multiple",
                  "label_smallforward": "player_msgmul", "label_addplaylists": "batch_addplaylist",
                  "label_coverlyric": "player_onlinesearch", "scrlabel_albuminfo": "player_albuminfo",
                  "scrlabel_songerinfo": "player_songerinfo", "label_delete": "player_delete"}
        for name, key in labels.items():
            r[name].text = self.s(key)
        r["item_ab"].on_click = self.ab
        for k in range(4):
            img = r[f"img_forward{k + 1}"]
            img.on_click = lambda k=k: self.speed(k)
        r["item_addplaylists"].on_click = self.add_to_playlist
        r["item_coverlyric"].on_click = lambda: (self.close(), self.dev.stoast("msg_notnetwork"))
        r["item_albuminfo"].on_click = self.album
        r["item_songerinfo"].on_click = self.artist
        r["item_delete"].on_click = self.delete
        r["button_return"].text = self.s("msg_no")
        r["button_return"].on_click = self.close
        self.fill()

    def fill(self) -> None:
        r = self.root
        r["image_ab"].image = "switch_on" if self.dev.ab else "switch_off"
        sp = int(self.dev.cfg("PLAYSPEED") or 0)
        for k in range(4):
            r[f"img_forward{k + 1}"].image = f"forward_{k + 1}" if k == sp else f"forward_un{k + 1}"
        self.dev.screen.update()

    def ab(self) -> None:
        """First tap marks A, the second B (then A..B repeats), the third ends it."""
        p = self.win.player
        if p.current is None:
            return
        ab = self.dev.ab
        if not ab:
            self.dev.ab = [p.position, None]
        elif ab[1] is None:
            if p.position <= ab[0] + 500:
                return
            self.dev.ab = [ab[0], p.position]
            p.seek(ab[0])
        else:
            self.dev.ab = None
        self.page._ab_marks()
        self.fill()

    def speed(self, k: int) -> None:
        self.dev.set_cfg("PLAYSPEED", k)
        self.fill()

    def add_to_playlist(self) -> None:
        t = self.win.player.current
        self.close()
        if t is None:
            self.dev.stoast("msg_addfailed")
            return
        self.dev.open(PlaylistsPage(self.dev, pick=[t["path"]]))

    def album(self) -> None:
        self.close()
        t = self.win.player.current
        a = self.dev.view.album_of(self.dev.view.by_path.get(t["path"], t)) if t else None
        if a is None:
            self.dev.stoast("msg_notalbuminfo")
            return
        self.dev.open(JumpInfoPage(self.dev, a))

    def artist(self) -> None:
        self.close()
        t = self.win.player.current
        name = artist_of(t) if t else ""
        if not name:
            self.dev.stoast("msg_notsongerinfo")
            return
        tracks = [x for x in self.dev.view.tracks if artist_of(x).casefold() == name.casefold()]
        self.dev.open(ArtistInfoPage(self.dev, name, tracks))

    def delete(self) -> None:
        """删除歌曲 takes the song out of the play queue; the file stays where it is."""
        self.close()
        p = self.win.player

        def go():
            if p.current is None:
                return
            rest = [t for k, t in enumerate(p.queue) if k != p.index]
            at = min(p.index, len(rest) - 1)
            if rest:
                p.play_tracks(rest, max(0, at))
            else:
                p.stop()
            self.dev.toast(tr("已从播放队列移除（文件没有删除）"))
        self.dev.confirm(self.s("msg_delsong"), go)


__all__ = ["PlayingPage", "QueueDialog", "PlayerMoreDialog", "SPEEDS"]
