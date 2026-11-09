"""Home and the local-music pages, as the firmware builds them (``home_page``, ``allmusic_page``,
``localclass_page``, ``album_page``, ``albuminfo_page``, ``artistinfo_page``, ``folder_page``, ``playlist_page``,
``addplaylist_page``, ``search_page``, ``playerjumpinfo_page``, ``updatemusic_page``, ``specfolder_page``).

Everything plays through the window's own player (``win.play_rows``); nothing here deletes a file: where the
player would delete songs, the desktop removes them from the list it can change (favourites, a playlist,
the recent list) and otherwise says "不支持移除".
"""
from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import QTimer

from ...core import library as corelib
from .. import catalog, i18n
from ..i18n import tr
from . import awtk
from .awtk import make
from .core import GREY, ROW, Dialog, Page, RowSource, label, right_arrow

# ---------------------------------------------------------------------- the firmware's tables
ALL_HOME = ("hifi", "tidal", "upmusic", "allmusic", "folder", "songer", "album", "genre", "fav", "frequentplay",
            "recentplay", "recentadd", "playlist", "book", "playset", "sysset", "hr", "cdripper",
            "baidu_netdisk")                                       # get_home_showinfo
DEFAULT_HOME = ("hifi", "baidu_netdisk", "tidal", "upmusic", "allmusic", "folder", "album", "songer", "genre", "hr",
                "fav", "frequentplay", "recentplay", "recentadd", "playlist", "playset", "sysset")   # main_allmenu
MENU_ORDER = DEFAULT_HOME + ("book", "cdripper")
NETWORK = ("hifi", "tidal", "baidu_netdisk")
SORTS = ("sort_alphabet", "sort_time_desc", "sort_time_asc", "sort_filename", "sort_format")
FORMAT_ICON = {"flac": "icon_flac", "mp3": "icon_mp3", "aac": "icon_aac", "m4a": "icon_m4a", "alac": "icon_alac",
               "ape": "icon_ape", "dff": "icon_dff", "dsf": "icon_dsf", "dts": "icon_dts", "iso": "icon_iso",
               "mp2": "icon_mp2", "ogg": "icon_ogg", "vorbis": "icon_ogg", "opus": "icon_opus", "tak": "icon_tak",
               "wav": "icon_wav", "wma": "icon_wma", "aiff": "icon_aiff", "aif": "icon_aiff"}


def default_home(dev) -> list[str]:
    """The stock home: main_allmenu; HiFi云曲库 only in Chinese."""
    return [k for k in DEFAULT_HOME if not (k == "hifi" and i18n.LANG == "en")]


def home_items(dev) -> list[str]:
    saved = dev.cfg("HOMEMENU")
    items = [k for k in (saved or []) if k in ALL_HOME]
    return items or default_home(dev)


def ext(t: dict) -> str:
    e = Path(t.get("path", "")).suffix.lower().lstrip(".")
    codec = (t.get("codec") or "").lower()
    return "alac" if codec == "alac" else (e or codec)


def title_of(t: dict) -> str:
    return t.get("title") or Path(t.get("path", "")).stem


def artist_of(t: dict) -> str:
    return t.get("artist") or t.get("albumartist") or ""


def human_size(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return ""


def added_at(t: dict) -> float:
    for k in ("added", "added_at", "first_seen", "scanned_at", "mtime_ns", "mtime"):
        if t.get(k):
            try:
                return float(t[k])
            except (TypeError, ValueError):
                pass
    return 0.0


def sort_tracks(tracks: list[dict], order: str) -> list[dict]:
    if order == "sort_time_desc":
        return sorted(tracks, key=added_at, reverse=True)
    if order == "sort_time_asc":
        return sorted(tracks, key=added_at)
    if order == "sort_filename":
        return sorted(tracks, key=lambda t: Path(t.get("path", "")).name.casefold())
    if order == "sort_format":
        return sorted(tracks, key=lambda t: (ext(t), title_of(t).casefold()))
    return sorted(tracks, key=lambda t: title_of(t).casefold())


def initial(text: str) -> str:
    c = (text or "#").strip()[:1].upper()
    return c if "A" <= c <= "Z" else "#"


# ---------------------------------------------------------------------- rows (table_row_create & co.)
def thumb(row: awtk.Node, pix, fallback: str, x: float = 20, size: float = 80) -> awtk.Node:
    img = make("image", (x, (row.h - size) / 2, size, size), "img_icon", parent=row, draw_type="scale_auto",
               image=fallback)
    img.pixmap = pix
    return img


def two_lines(row: awtk.Node, left: float, right: float, title: str, sub: str = "", yellow: bool = False) -> None:
    w = row.w - left - right
    style = "srlabel_leftyellow32_style" if yellow else "srlabel_leftwhite32_style"
    if sub:
        make("hscroll_label", (left, 22, w, 40), "labeltrackname", style, parent=row, text=title, only_focus="true",
             ellipses="true")
        make("hscroll_label", (left, 66, w, 32), "labeltrackartist", "srlabel_leftgray26_style", parent=row,
             text=sub, only_focus="true", ellipses="true")
    else:
        make("hscroll_label", (left, 0, w, row.h), "labeltrackname", style, parent=row, text=title,
             only_focus="true", ellipses="true")


def choice(row: awtk.Node, on: bool, x: float = 20) -> None:
    make("image", (x, (row.h - 48) / 2, 48, 48), "img_choice", parent=row, draw_type="center",
         image="mulselected" if on else "unselect")


def table_row(w: float, h: float = ROW) -> awtk.Node:
    return make("button", (0, 0, w, h), "btn_table_row", "btn_tablerow_style")


# ====================================================================== home
class HomePage(Page):
    """home_page: tiles of six (3 x 2) on a slide view; swiping on past the last page opens 选择显示项; the
    song bar at the bottom (cover, title, ⏯, the queue)."""
    ui = "home_page"

    def build(self) -> None:
        r = self.root
        slide = r["slide_view"]
        slide.on_end = lambda: self.dev.show_dialog(_homemenu(self.dev))
        song = r["view_song"]
        song["item_bottonplay"].on_click = self.dev.playing
        song["home_btn_pause"].on_click = self._toggle
        song["btn_playlists"].on_click = self._queue
        self.rebuild()

    def rebuild(self) -> None:
        r = self.root
        slide = r["slide_view"]
        slide.children.clear()
        items = home_items(self.dev)
        pages = [items[i:i + 6] for i in range(0, len(items), 6)] or [[]]
        for pi, keys in enumerate(pages):
            view = make("view", (0, 0, 720, 518), f"home_slide{pi}", parent=slide)
            for i, key in enumerate(keys):
                c, row = i % 3, i // 3
                b = make("button", (5 + 238 * c, 254 * row, 234, 250), f"home_{key}", f"home_{key}_style",
                         parent=view)
                b.on_click = (lambda k=key: self.open_item(k))
                lab = make("label", (0, 150, 234, 80), f"label_{key}", "label_center30_style", parent=b,
                           text=self.s(key), word_wrap="true")
                lab.enabled = False
        slide.page = min(int(getattr(slide, "page", 0)), len(pages) - 1)
        slide.drag = 0.0
        ind = r["slide_indicator"]
        ind.count = len(pages)
        ind.max = max(1, len(pages))
        ind.value = float(slide.page)
        ind.props["size"] = "10"
        slide.on_page = lambda i: setattr(ind, "value", float(i))
        self.relayout()
        self.dev.screen.update()

    def _toggle(self) -> None:
        if self.win.player.current is not None:
            self.win.player.toggle()

    def _queue(self) -> None:
        from .playing import QueueDialog
        if not self.win.player.queue:
            self.dev.stoast("msg_notsonglist")
            return
        self.dev.show_dialog(QueueDialog(self.dev))

    def open_item(self, key: str) -> None:
        d = self.dev
        if key in NETWORK:
            d.stoast("msg_notnetwork")
            return
        if key == "cdripper":
            d.stoast("cdnotinserted")
            return
        if key == "book":
            d.stoast("msg_notfile")
            return
        from .settings import PlaySettingsPage, SystemSettingsPage
        pages = {"upmusic": lambda: UpdateMusicPage(d),
                 "allmusic": lambda: AllMusicPage(d),
                 "folder": lambda: FolderPage(d),
                 "songer": lambda: ArtistsPage(d),
                 "album": lambda: AlbumsPage(d),
                 "genre": lambda: GenresPage(d),
                 "fav": lambda: TrackListPage(d, "fav", favourites),
                 "frequentplay": lambda: TrackListPage(d, "frequentplay", frequent),
                 "recentplay": lambda: TrackListPage(d, "recentplay", recent),
                 "recentadd": lambda: TrackListPage(d, "recentadd", recently_added),
                 "hr": lambda: TrackListPage(d, "hr", lambda dev: [t for t in dev.view.tracks if catalog.is_hires(t)]),
                 "playlist": lambda: PlaylistsPage(d),
                 "playset": lambda: PlaySettingsPage(d),
                 "sysset": lambda: SystemSettingsPage(d)}
        make_page = pages.get(key)
        if make_page is not None:
            d.open(make_page())

    def changed(self, what: str) -> None:
        if what not in ("track", "state", "position", "covers", "library", "favorites"):
            return
        p = self.win.player
        song = self.root["view_song"]
        t = p.current
        song["home_scrlabel_title"].text = title_of(t) if t else "Established in 1979"
        song["home_scrlabel_artist"].text = (artist_of(t) or self.s("player_unknown")) if t else "ONIX"
        song["img_album"].pixmap = self.dev.track_cover(t) if t else None
        song["home_btn_pause"].style = "btn_bottonplaying_style" if p.state == "playing" else "btn_bottonstop_style"
        sl = song["slider"]
        sl.max = max(1, p.duration)
        sl.value = min(p.position, sl.max) if t else 0


def _homemenu(dev):
    from .dialogs import HomeMenuDialog
    return HomeMenuDialog(dev)


# ---- the play-history lists
def favourites(dev) -> list[dict]:
    by = dev.view.by_path
    return [by[p] for p in dev.win.favorites.paths() if p in by]


def _history(dev) -> dict:
    return dev.win.prefs.get("device_history") or {}


def recent(dev) -> list[dict]:
    by = dev.view.by_path
    return [by[p] for p in _history(dev).get("recent", []) if p in by]


def frequent(dev) -> list[dict]:
    by = dev.view.by_path
    counts = _history(dev).get("counts", {})
    return [by[p] for p in sorted((p for p in counts if p in by), key=lambda p: -counts[p])[:100]]


def recently_added(dev) -> list[dict]:
    return sorted(dev.view.tracks, key=added_at, reverse=True)[:100]


# ====================================================================== the list machinery
class ListPage(Page):
    """A firmware list: the navigation bar's buttons, and 多选 (the selection bar: 全选, 添加到歌单, 收藏, 删除,
    取消) with a check box on every row."""
    client = "scroll_view"
    row_h = ROW
    can_select = True

    def __init__(self, dev, title: str = ""):
        self.items: list = []
        self.selecting = False
        self.chosen: set[int] = set()
        super().__init__(dev, title)

    def build(self) -> None:
        self.list = self.scroller(self.client)
        self.list.rows = RowSource(0, self.row_h, self.row)
        self.set_nav()
        self.reload()

    # ---- navigation bar
    def buttons(self) -> list:
        return []

    def set_nav(self) -> None:
        if not self.selecting:
            self.navbar(self.nav_title(), self.buttons())
            return
        bar = self.root.find("navigationbar_view")
        bar.children = [c for c in bar.children if c.name not in ("btn_return", "scrlabel_title", "gif_returnplay")
                        and not c.name.startswith("navbtn")]
        every = len(self.chosen) == len(self.items) and self.items
        b = make("button", (17, 0, 88, 82), "navbtn_all", "navbar_btnselected_style" if every
                 else "navbar_btnunselect_style", parent=bar)
        b.on_click = self.select_all
        self.title_node = make("hscroll_label", (105, 0, 240, 82), "scrlabel_title", "msg_leftwhite36_style",
                               parent=bar, text=f"{self.s('navigation_all')}({len(self.chosen)})")
        self.nav_buttons = []
        for i, (style, fn) in enumerate(self.select_buttons()):
            nb = make("button", (710 - 88 * (len(self.select_buttons()) - i), 0, 88, 82), f"navbtn{i}", style,
                      parent=bar)
            nb.on_click = fn
            self.nav_buttons.append(nb)
        self.gif = None

    def nav_title(self) -> str:
        return self.title

    def select_buttons(self) -> list:
        return [("navbar_btnadd_style", self.add_chosen), ("navbar_btnaddfav_style", self.fav_chosen),
                ("navbar_btndel_style", self.del_chosen), ("navbar_btncancel_style", self.end_select)]

    # ---- data
    def collect(self) -> list:
        return []

    def reload(self) -> None:
        self.items = self.collect()
        self.chosen = {i for i in self.chosen if i < len(self.items)}
        self.list.rows.reset(len(self.items))
        self.list.scroll_y = min(self.list.scroll_y, max(0.0, self.list.rows.height() - self.list.h))
        if self.title_node is not None and not self.selecting:
            self.title_node.text = self.nav_title()
        self.empty(not self.items)
        self.dev.screen.update()

    def empty(self, on: bool) -> None:
        pass

    def row(self, i: int) -> awtk.Node:
        return table_row(self.list.w, self.row_h)

    def refresh_rows(self) -> None:
        self.list.rows.reset()
        self.dev.screen.update()

    # ---- 多选
    def start_select(self, first: int | None = None) -> None:
        """多选 (the bar's button, or a long press -- which ticks that row)."""
        if not self.items:
            return
        self.selecting = True
        self.chosen = {first} if first is not None and 0 <= first < len(self.items) else set()
        self.set_nav()
        self.refresh_rows()

    def end_select(self) -> None:
        self.selecting = False
        self.chosen = set()
        self.set_nav()
        self.refresh_rows()

    def toggle(self, i: int) -> None:
        self.chosen ^= {i}
        self.set_nav()
        self.refresh_rows()

    def select_all(self) -> None:
        self.chosen = set() if len(self.chosen) == len(self.items) else set(range(len(self.items)))
        self.set_nav()
        self.refresh_rows()

    def chosen_tracks(self) -> list[dict]:
        out = []
        for i in sorted(self.chosen):
            out.extend(self.tracks_of(self.items[i]))
        return out

    def tracks_of(self, item) -> list[dict]:
        return [item] if isinstance(item, dict) else []

    def _need_choice(self) -> list[dict] | None:
        tracks = self.chosen_tracks()
        if not tracks:
            self.dev.stoast("msg_notfile")
            return None
        return tracks

    def add_chosen(self) -> None:
        tracks = self._need_choice()
        if tracks:
            self.end_select()
            self.dev.open(PlaylistsPage(self.dev, pick=[t["path"] for t in tracks]))

    def fav_chosen(self) -> None:
        tracks = self._need_choice()
        if tracks:
            self.win.favorites.set([t["path"] for t in tracks], True)
            self.end_select()
            self.dev.stoast("msg_addfavsuccess")

    def del_chosen(self) -> None:
        if self._need_choice():
            self.dev.stoast("msg_notsupportdel")

    def back_key(self) -> bool:
        """The return button / Esc in 多选 ends it first."""
        if self.selecting:
            self.end_select()
            return True
        return False

    def changed(self, what: str) -> None:
        super().changed(what)
        if what in ("library", "favorites", "playlists"):
            self.reload()
        elif what in ("track", "covers"):
            self.refresh_rows()

    # ---- common rows
    def track_row(self, i: int, t: dict, sub: str | None = None, number: int | None = None,
                  tracks: list | None = None) -> awtk.Node:
        w = self.list.w
        r = table_row(w)
        cur = self.win.player.current
        playing = cur is not None and cur.get("path") == t.get("path")
        x = 20
        if self.selecting:
            choice(r, i in self.chosen, 20)
            x = 84
        if number is not None:
            label(r, (x, 0, 90, ROW), str(number), 32, GREY, align=1, kind="label")
        else:
            img = thumb(r, self.dev.track_cover(t), "default_track_small", x)
            if playing:
                make("gif", (img.x + 16, img.y + 16, 48, 48), "img_gifbg", parent=r, image="playing_gif_medium")
        if sub is None:
            sub = " - ".join(x for x in (artist_of(t), t.get("album") or "") if x) or self.s("player_unknown")
        left = x + 100
        two_lines(r, left, 110, title_of(t), sub, yellow=playing)
        make("image", (w - 46 - 30, (ROW - 22) / 2, 46, 22), "img_format", parent=r,
             image=FORMAT_ICON.get(ext(t), "icon_unknow"), draw_type="center")
        if self.selecting:
            r.on_click = lambda: self.toggle(i)
        else:
            src = tracks if tracks is not None else self.items
            r.on_click = lambda: self.play_from(src, t)
            r.on_long = (lambda: self.start_select(i)) if self.can_select else None
        return r

    def play_from(self, tracks: list, t: dict) -> None:
        tracks = [x for x in tracks if isinstance(x, dict)]
        self.dev.play(tracks, next((k for k, x in enumerate(tracks) if x is t), 0))

    def play_all(self) -> None:
        tracks = [t for item in self.items for t in self.tracks_of(item)]
        if not tracks:
            self.dev.stoast("msg_notfile")
            return
        self.dev.play(tracks, 0)

    def search(self) -> None:
        self.dev.open(SearchPage(self.dev))


# ====================================================================== songs
class TrackListPage(ListPage):
    """localclass_page for songs: 我的最爱, 经常播放, 最近播放, 最近添加, 高解析度, a genre's songs."""
    ui = "localclass_page"

    def __init__(self, dev, key: str, source, title: str | None = None, remove=None):
        self.key, self.source, self.remove = key, source, remove
        super().__init__(dev, title or dev.s(key))

    def nav_title(self) -> str:
        return f"{self.title}({len(self.items)})"

    def buttons(self):
        return [("navbar_btnsearch_style", self.search), ("navbar_btnallplay_style", self.play_all),
                ("navbar_btnmulselect_style", self.start_select)]

    def collect(self) -> list:
        return [t for t in (self.source(self.dev) or []) if t]

    def row(self, i: int) -> awtk.Node:
        return self.track_row(i, self.items[i])

    def del_chosen(self) -> None:
        tracks = self._need_choice()
        if not tracks:
            return
        paths = [t["path"] for t in tracks]
        if self.key == "fav":
            def go():
                self.win.favorites.set(paths, False)
                self.end_select()
                self.dev.stoast("msg_uncollect")
            self.dev.confirm(self.s("msg_delsong"), go)
        elif self.key in ("recentplay", "frequentplay"):
            def go():
                h = dict(self.win.prefs.get("device_history") or {})
                h["recent"] = [p for p in h.get("recent", []) if p not in paths]
                if self.key == "frequentplay":
                    h["counts"] = {p: n for p, n in (h.get("counts") or {}).items() if p not in paths}
                self.win.prefs.set("device_history", h)
                self.end_select()
                self.reload()
                self.dev.stoast("msg_delsongsuccess")
            self.dev.confirm(self.s("msg_delsong"), go)
        elif self.remove is not None:
            self.dev.confirm(self.s("msg_delsong"), lambda: (self.remove(paths), self.end_select(),
                                                              self.dev.stoast("msg_delsongsuccess")))
        else:
            self.dev.stoast("msg_notsupportdel")


class AllMusicPage(ListPage):
    """allmusic_page: every song, sorted (排序方式), the big initial while scrolling; empty: 立即扫描音乐."""
    ui = "allmusic_page"
    client = "music_table_client"

    def build(self) -> None:
        r = self.root
        r["image_wait"].visible = False
        letter = r["view_letter"]
        letter.paint_hook = self._letter
        letter.passthrough = True                  # (drawn over the list; presses go to the list)
        v = r["view"]
        v["label_scanfolder"].text = self.s("not_allmusic")
        v["btn_upmusic"].text = self.s("scan_now")
        v["btn_upmusic"].on_click = lambda: self.dev.show_dialog(UpdateMusicDialog(self.dev))
        client = r["music_table_client"]
        client.props["self_layout"] = "default(x=0,y=0,w=710,h=100%)"
        super().build()

    def nav_title(self) -> str:
        return f"{self.s('allmusic')}({len(self.items)})"

    def buttons(self):
        return [("navbar_btnsearch_style", self.search), ("navbar_btnorder_style", self.choose_sort),
                ("navbar_btnallplay_style", self.play_all), ("navbar_btnmulselect_style", self.start_select)]

    def collect(self) -> list:
        return sort_tracks(list(self.dev.view.tracks), SORTS[int(self.dev.cfg("SORTMODE")) % len(SORTS)])

    def empty(self, on: bool) -> None:
        self.root["view"].visible = on
        self.root["table_view"].visible = not on

    def row(self, i: int) -> awtk.Node:
        return self.track_row(i, self.items[i])

    def choose_sort(self) -> None:
        from .dialogs import ChoiceDialog
        cur = SORTS[int(self.dev.cfg("SORTMODE")) % len(SORTS)]

        def chosen(k):
            self.dev.set_cfg("SORTMODE", SORTS.index(k))
            self.list.scroll_y = 0
            self.reload()
        self.dev.show_dialog(ChoiceDialog(self.dev, self.s("title_sort"), [(k, self.s(k)) for k in SORTS], cur, chosen))

    def _letter(self, p, n, r, st) -> bool:
        """The big letter (view_letter) while the list is dragged or flung."""
        lab = n["label_letter"]
        if not self.items or not self.dev.screen.scrolling(self.list) or \
                SORTS[int(self.dev.cfg("SORTMODE")) % len(SORTS)] != "sort_alphabet":
            lab.visible = False
            return True
        lab.visible = True
        i = min(len(self.items) - 1, int(self.list.scroll_y // ROW))
        lab.text = initial(title_of(self.items[i]))
        return False

    def changed(self, what: str) -> None:
        super().changed(what)
        if what == "scan":
            self.reload()


# ====================================================================== albums
class AlbumsPage(ListPage):
    """album_page: a list (cover, album, artist) or 3 covers a row (ALBUMTYPE), switched in the bar."""
    ui = "album_page"
    can_select = True

    def build(self) -> None:
        super().build()
        self._mode()

    @property
    def grid(self) -> bool:
        return bool(int(self.dev.cfg("ALBUMTYPE") or 0))

    def _mode(self) -> None:
        if self.grid:
            self.list.rows = RowSource(len(self.items), 256, self.cell, cols=3, top=10)
        else:
            self.list.rows = RowSource(len(self.items), ROW, self.row)

    def nav_title(self) -> str:
        return f"{self.s('album')}({len(self.items)})"

    def buttons(self):
        return [("btn_listalbum_style" if self.grid else "btn_covermode_style", self.switch),
                ("navbar_btnsearch_style", self.search), ("navbar_btnmulselect_style", self.start_select)]

    def switch(self) -> None:
        self.dev.set_cfg("ALBUMTYPE", 0 if self.grid else 1)
        self.set_nav()
        self.relayout()
        self._mode()
        self.list.scroll_y = 0
        self.dev.screen.update()

    def collect(self) -> list:
        return sorted(self.dev.view.albums, key=lambda a: a.title.casefold())

    def tracks_of(self, album) -> list[dict]:
        return list(album.tracks)

    def row(self, i: int) -> awtk.Node:
        a = self.items[i]
        r = table_row(self.list.w)
        x = 20
        if self.selecting:
            choice(r, i in self.chosen)
            x = 84
        thumb(r, self.dev.cover(a), "default_album_small", x)
        two_lines(r, x + 100, 80, a.title, a.artist or self.s("player_unknown"))
        right_arrow(r)
        self._bind(r, i, a)
        return r

    def cell(self, i: int) -> awtk.Node:
        a = self.items[i]
        w = self.list.w / 3
        c = make("button", (0, 0, w, 256), "cell", "btn_nottablerow_style")
        img = make("image", ((w - 200) / 2, 4, 200, 200), "cover", parent=c, draw_type="scale_auto",
                   image="default_album_big")
        img.pixmap = self.dev.cover(a)
        cur = self.win.player.current
        if cur is not None and self.dev.view.album_of(cur) is a:
            make("gif", ((w - 88) / 2, 60, 88, 88), "img_gifbg", parent=c, image="playing_gif_big")
        if self.selecting:
            choice(c, i in self.chosen, (w - 200) / 2 + 146)
            c.children[-1].y = 10
        make("hscroll_label", ((w - 200) / 2, 210, 200, 36), "title", "srlabel_centerwhite30_style", parent=c,
             text=a.title, only_focus="true", ellipses="true")
        self._bind(c, i, a)
        return c

    def _bind(self, r, i, a) -> None:
        if self.selecting:
            r.on_click = lambda: self.toggle(i)
        else:
            r.on_click = lambda: self.dev.open(AlbumInfoPage(self.dev, a))
            r.on_long = lambda: self.start_select(i)

    def del_chosen(self) -> None:
        if self._need_choice():
            self.dev.stoast("msg_notsupportdel")


class AlbumInfoPage(ListPage):
    """albuminfo_page: the cover, album, artist, 全部播放, then the songs by number."""
    ui = "albuminfo_page"

    def __init__(self, dev, album):
        self.album = album
        super().__init__(dev, dev.s("album"))

    def build(self) -> None:
        r = self.root
        r["img_albuminfo"].props["draw_type"] = "scale_auto"
        r["albuminfo_album"].text = self.album.title
        r["albuminfo_artist"].text = self.album.artist or self.s("player_unknown")
        r["albuminfo_artist"].inline["normal"] = {"font_size": 26, "text_color": GREY, "text_align_h": 2}
        r["btn_playall"].text = ""
        r["btn_playall"].on_click = self.play_all
        super().build()
        self.changed("covers")

    def buttons(self):
        return [("navbar_btnmulselect_style", self.start_select)]

    def collect(self) -> list:
        return list(self.album.tracks)

    def row(self, i: int) -> awtk.Node:
        t = self.items[i]
        return self.track_row(i, t, sub=artist_of(t) or self.s("player_unknown"),
                              number=int(t.get("tracknumber") or i + 1) if str(t.get("tracknumber") or "").isdigit()
                              else i + 1)

    def changed(self, what: str) -> None:
        super().changed(what)
        if what in ("covers", "library", "track"):
            self.root["img_albuminfo"].pixmap = self.dev.cover(self.album)


class JumpInfoPage(ListPage):
    """playerjumpinfo_page (the playing page's ⋯ › 专辑信息): the album's songs."""
    ui = "playerjumpinfo_page"
    client = "music_table_client"

    def __init__(self, dev, album):
        self.album = album
        super().__init__(dev, album.title)

    def buttons(self):
        return [("navbar_btnallplay_style", self.play_all)]

    def collect(self) -> list:
        return list(self.album.tracks)

    def row(self, i: int) -> awtk.Node:
        t = self.items[i]
        return self.track_row(i, t, sub=artist_of(t) or self.s("player_unknown"))


# ====================================================================== artists, genres
def artist_groups(dev, by_song: bool) -> list[tuple[str, list[dict]]]:
    groups: dict[str, tuple[str, list]] = {}
    unknown = dev.s("player_unknown")
    for t in dev.view.tracks:
        name = (artist_of(t) if by_song else corelib.album_artist(t)) or unknown
        groups.setdefault(name.casefold(), (name, []))[1].append(t)
    return sorted(groups.values(), key=lambda x: x[0].casefold())


class ArtistsPage(ListPage):
    """localclass_page for 歌手, by album artist or by song artist (ARTISTTYPE, the bar's switch)."""
    ui = "localclass_page"

    def __init__(self, dev):
        super().__init__(dev, dev.s("songer"))

    @property
    def by_song(self) -> bool:
        return bool(int(self.dev.cfg("ARTISTMODE") or 0))

    def nav_title(self) -> str:
        return f"{self.title}({len(self.items)})"

    def buttons(self):
        return [("bnt_songsinger_style" if self.by_song else "btn_albumsinger_style", self.switch),
                ("navbar_btnsearch_style", self.search), ("navbar_btnmulselect_style", self.start_select)]

    def switch(self) -> None:
        self.dev.set_cfg("ARTISTMODE", 0 if self.by_song else 1)
        self.set_nav()
        self.relayout()
        self.list.scroll_y = 0
        self.reload()
        self.dev.stoast("singerbysong" if self.by_song else "singerbyalbum")

    def collect(self) -> list:
        return artist_groups(self.dev, self.by_song)

    def tracks_of(self, item) -> list[dict]:
        return list(item[1])

    def row(self, i: int) -> awtk.Node:
        name, tracks = self.items[i]
        r = table_row(self.list.w)
        x = 20
        if self.selecting:
            choice(r, i in self.chosen)
            x = 84
        first = next((self.dev.view.album_of(t) for t in tracks if self.dev.view.album_of(t) is not None), None)
        thumb(r, self.dev.cover(first), "default_artist_small", x)
        albums = {self.dev.view.album_key(t) for t in tracks}
        two_lines(r, x + 100, 80, name, tr("{0} 张专辑 · {1} 首", len(albums), len(tracks)))
        right_arrow(r)
        if self.selecting:
            r.on_click = lambda: self.toggle(i)
        else:
            r.on_click = lambda: self.dev.open(ArtistInfoPage(self.dev, name, tracks))
            r.on_long = lambda: self.start_select(i)
        return r


class GenresPage(ListPage):
    ui = "localclass_page"

    def __init__(self, dev):
        super().__init__(dev, dev.s("genre"))

    def nav_title(self) -> str:
        return f"{self.title}({len(self.items)})"

    def buttons(self):
        return [("navbar_btnsearch_style", self.search), ("navbar_btnmulselect_style", self.start_select)]

    def collect(self) -> list:
        return catalog.genre_tracks(self.dev.view.tracks)

    def tracks_of(self, item) -> list[dict]:
        return list(item[1])

    def row(self, i: int) -> awtk.Node:
        name, tracks = self.items[i]
        r = table_row(self.list.w)
        x = 20
        if self.selecting:
            choice(r, i in self.chosen)
            x = 84
        thumb(r, None, "default_genre_small", x)
        two_lines(r, x + 100, 80, name, self.s("genre_statistics").replace("%d", str(len(tracks))))
        right_arrow(r)
        if self.selecting:
            r.on_click = lambda: self.toggle(i)
        else:
            r.on_click = lambda: self.dev.open(TrackListPage(self.dev, "genre", lambda dev: self._tracks(name),
                                                             title=name))
            r.on_long = lambda: self.start_select(i)
        return r

    def _tracks(self, name):
        return next((tracks for g, tracks in catalog.genre_tracks(self.dev.view.tracks) if g == name), [])


class ArtistInfoPage(ListPage):
    """artistinfo_page: the artist, 全部播放, and the 单曲 / 专辑 tabs."""
    ui = "artistinfo_page"
    client = "scroll_view_track"

    def __init__(self, dev, name, tracks):
        self.name, self.tracks = name, sorted(tracks, key=lambda t: title_of(t).casefold())
        super().__init__(dev, dev.s("songer"))

    def build(self) -> None:
        r = self.root
        r["img_albuminfo"].props["draw_type"] = "scale_auto"
        r["artistinfo_scrlabel_title"].text = self.name
        r["btn_playall"].text = ""
        r["btn_playall"].on_click = self.play_all
        keys, albums = set(), []
        for t in self.tracks:
            a = self.dev.view.album_of(t)
            if a is not None and a.key not in keys:
                keys.add(a.key)
                albums.append(a)
        self.albums = sorted(albums, key=lambda a: a.title.casefold())
        self.tabs = [r["btn_track"], r["btn_album"]]
        for i, b in enumerate(self.tabs):
            b.on_click = lambda i=i: self.tab(i)
        r["tab_button_group"].props["children_layout"] = "default(r=1,c=2)"
        self.album_list = self.scroller("scroll_view_album")
        self.album_list.rows = RowSource(len(self.albums), 256, self.album_cell, cols=3, top=10)
        super().build()
        first = self.albums[0] if self.albums else None
        r["img_albuminfo"].pixmap = self.win.cover_pixmap(first) if first is not None else None
        self.tab(0)

    def buttons(self):
        return [("navbar_btnmulselect_style", self.start_select)]

    def collect(self) -> list:
        return list(self.tracks)

    def tab(self, i: int) -> None:
        for k, b in enumerate(self.tabs):
            b.checked = k == i
        self.root["view_track"].visible = i == 0
        self.root["view_album"].visible = i == 1
        self.dev.screen.update()

    def row(self, i: int) -> awtk.Node:
        t = self.items[i]
        return self.track_row(i, t, sub=t.get("album") or self.s("player_unknown"))

    def album_cell(self, i: int) -> awtk.Node:
        a = self.albums[i]
        w = self.album_list.w / 3
        c = make("button", (0, 0, w, 256), "cell", "btn_nottablerow_style")
        img = make("image", ((w - 200) / 2, 4, 200, 200), "cover", parent=c, draw_type="scale_auto",
                   image="default_album_big")
        img.pixmap = self.dev.cover(a)
        make("hscroll_label", ((w - 200) / 2, 210, 200, 36), "title", "srlabel_centerwhite30_style", parent=c,
             text=a.title, only_focus="true", ellipses="true")
        c.on_click = lambda: self.dev.open(AlbumInfoPage(self.dev, a))
        return c

    def changed(self, what: str) -> None:
        super().changed(what)
        if what in ("covers", "track"):
            self.album_list.rows.reset()


# ====================================================================== folders
class FolderPage(ListPage):
    """folder_page: the music folders first (as the player shows TF / OTG), then folders and songs."""
    ui = "folder_page"
    client = "folder_table_client"

    def __init__(self, dev, node=None, title=None):
        self.node = node
        super().__init__(dev, title or dev.s("folder"))

    def build(self) -> None:
        self.start = self.root["view_start"]
        self.template = self.start["list_sd"]
        super().build()

    def buttons(self):
        if self.node is None:
            return []
        return [("navbar_btnsearch_style", self.search), ("navbar_btnmulselect_style", self.start_select)]

    def collect(self) -> list:
        tree = catalog.folder_tree(self.dev.view, self.win.music_roots())
        if self.node is None:
            roots = list(tree.folders.values())
            self._roots(roots)
            return [("dir", f) for f in roots] if len(roots) > 2 else []
        self.start.visible = False
        self.root["table_view"].visible = True
        node = catalog.find_folder(tree, self.node.path) or self.node
        self.node = node
        return [("dir", f) for f in sorted(node.folders.values(), key=lambda f: f.name.casefold())] + \
               [("file", t) for t in sorted(node.tracks, key=lambda t: Path(t["path"]).name.casefold())]

    def _roots(self, roots) -> None:
        table = self.root["table_view"]
        self.start.children = []
        if len(roots) > 2 or self.node is not None:
            self.start.visible = False
            table.visible = True
            return
        self.start.visible = True
        table.visible = False
        if not roots:
            msg = make("label", (0, 200, 720, 40), "label_none", parent=self.start, text=self.s("msg_notfile"))
            msg.inline["normal"] = {"font_size": 32, "text_color": GREY, "text_align_h": 1}
            return
        for k, f in enumerate(roots):
            self.start.add(self._root_item(f, k))
        self.relayout()

    def _root_item(self, f, k: int) -> awtk.Node:
        tpl = self.template
        item = awtk.Node("list_item", "list_root", (0, 0, 0, 270), dict(tpl.props))
        item.inline = tpl.inline
        img = make("image", (30, 35, 200, 200), "image", parent=item, draw_type="default")
        img.inline["normal"] = {"bg_image": "sd_bg" if k == 0 else "otg_bg", "round_radius": 100}
        used = 0
        try:
            du = shutil.disk_usage(f.root)
            used = round(du.used * 100 / du.total) if du.total else 0
        except (OSError, AttributeError):
            pass
        lab = make("label", (0, 0, 200, 200), "label_img", parent=img, text=f"{used}%")
        lab.inline["normal"] = {"font_size": 40, "text_color": (255, 255, 255, 255)}
        circ = make("progress_circle", (45, 50, 171, 173), "progress", parent=item, show_text="false",
                    line_width="14", value=str(used))
        circ.inline["normal"] = {"fg_color": (253, 113, 54, 255) if k == 0 else (228, 162, 46, 255),
                                 "bg_color": (0, 0, 0, 0), "text_color": (0, 0, 0, 0)}
        label(item, (250, 90, 406, 42), f.name, 36, kind="label")
        label(item, (250, 148, 406, 42), tr("{0} 首", f.total), 30, GREY, kind="label")
        item.on_click = lambda: self.dev.open(FolderPage(self.dev, f, f.name))
        return item

    def tracks_of(self, item) -> list[dict]:
        kind, x = item
        return [x] if kind == "file" else list(_all_tracks(x))

    def row(self, i: int) -> awtk.Node:
        kind, x = self.items[i]
        w = self.list.w
        r = table_row(w)
        left = 20
        if self.selecting:
            choice(r, i in self.chosen)
            left = 84
        if kind == "dir":
            thumb(r, None, "icon_folder", left)
            two_lines(r, left + 100, 80, x.name, tr("{0} 首", x.total))
            right_arrow(r)
            r.on_click = (lambda: self.toggle(i)) if self.selecting else \
                (lambda: self.dev.open(FolderPage(self.dev, x, x.name)))
        else:
            cur = self.win.player.current
            playing = cur is not None and cur.get("path") == x.get("path")
            thumb(r, None, "icon_music", left)
            two_lines(r, left + 100, 110, Path(x["path"]).name, "", yellow=playing)
            if playing:
                make("gif", (w - 88, (ROW - 48) / 2, 48, 48), "img_gifbg", parent=r, image="playing_gif")
            else:
                make("image", (w - 76, (ROW - 22) / 2, 46, 22), "img_format", parent=r,
                     image=FORMAT_ICON.get(ext(x), "icon_unknow"), draw_type="center")
            files = [t for k, t in self.items if k == "file"]
            r.on_click = (lambda: self.toggle(i)) if self.selecting else (lambda: self.play_from(files, x))
        if not self.selecting:
            r.on_long = lambda: self.start_select(i)
        return r

    def play_all(self) -> None:
        files = [t for k, t in self.items if k == "file"]
        if files:
            self.dev.play(files, 0)


def _all_tracks(folder):
    yield from folder.tracks
    for f in folder.folders.values():
        yield from _all_tracks(f)


# ====================================================================== playlists
class PlaylistsPage(ListPage):
    """playlist_page: 导入歌单 / 导出歌单, then the playlists (⋯ renames or deletes); "+" makes one. With
    ``pick`` (多选 › 添加到歌单) a tap adds the songs to that playlist and goes back."""
    ui = "playlist_page"

    def __init__(self, dev, pick: list[str] | None = None):
        self.pick = pick
        super().__init__(dev, dev.s("playlist"))

    def build(self) -> None:
        self.root["image_wait"].visible = False
        super().build()

    def nav_title(self) -> str:
        return f"{self.title}({len(self.lists)})" if hasattr(self, "lists") else self.title

    def buttons(self):
        b = [("navbar_btnadd_style", self.create)]
        if self.pick is None:
            b.append(("navbar_btnmulselect_style", self.start_select))
        return b

    def collect(self) -> list:
        self.lists = self.win.state.playlist_list()
        head = [] if self.pick is not None else [("import", None), ("export", None)]
        return head + [("list", pl) for pl in self.lists]

    def select_buttons(self) -> list:
        return [("navbar_btndel_style", self.del_chosen), ("navbar_btncancel_style", self.end_select)]

    def tracks_of(self, item) -> list[dict]:
        kind, pl = item
        return self._tracks(pl["id"]) if kind == "list" else []

    def row(self, i: int) -> awtk.Node:
        kind, pl = self.items[i]
        r = make("list_item", (0, 0, self.list.w, ROW), "row", "list_itemplaylist_style")
        if kind in ("import", "export"):
            make("image", (30, 36, 48, 48), "img", parent=r, image=kind, draw_type="center")
            label(r, (100, 0, 500, ROW), self.s("playlist_" + kind), 32)
            r.on_click = lambda: self.dev.confirm(self.s("msg_%splaylist" % kind), lambda: self.dev.stoast("msg_nottf"))
            return r
        x = 20
        if self.selecting:
            choice(r, i in self.chosen)
            x = 84
        rows, _ = self.win.state.playlist_rows(pl["id"])
        by = self.dev.view.by_path
        first = next((by[t["path"]] for t in rows if t.get("path") in by), None)
        thumb(r, self.dev.track_cover(first) if first else None, "icon_music", x)
        two_lines(r, x + 100, 100, pl["name"], self.s("genre_statistics").replace("%d", str(len(rows))))
        if self.selecting:
            r.on_click = lambda: self.toggle(i)
            return r
        if self.pick is not None:
            r.on_click = lambda: self._add_to(pl)
            return r
        more = make("button", (r.w - 88, 0, 88, ROW), "playlists_more", parent=r)
        more.inline["normal"] = {"bg_color": (0, 0, 0, 0), "border_color": (0, 0, 0, 0), "icon": "playlists_more"}
        more.inline["pressed"] = {"bg_color": (0, 0, 0, 0), "border_color": (0, 0, 0, 0),
                                  "icon": "playlists_more_down"}
        more.on_click = lambda: self.dev.show_dialog(PlaylistMoreDialog(self.dev, pl))
        r.on_click = lambda: self.dev.open(TrackListPage(
            self.dev, "playlist", lambda dev: self._tracks(pl["id"]), title=pl["name"],
            remove=lambda paths, pid=pl["id"]: self._remove(pid, paths)))
        r.on_long = lambda: self.start_select(i)
        return r

    def _tracks(self, pid):
        rows, _ = self.win.state.playlist_rows(pid)
        by = self.dev.view.by_path
        return [by[t["path"]] for t in rows if t.get("path") in by]

    def _remove(self, pid, paths) -> None:
        rows, _ = self.win.state.playlist_rows(pid)
        gone = set(paths)
        self.win.state.remove_rows(pid, [k for k, t in enumerate(rows) if t.get("path") in gone])

    def _add_to(self, pl) -> None:
        n = self.win.state.append(pl["id"], list(self.pick))
        self.dev.back()
        self.dev.stoast("msg_addplaylistsuccess" if n else "msg_addfailed")

    def create(self) -> None:
        self.dev.open(AddPlaylistPage(self.dev, self.pick))

    def del_chosen(self) -> None:
        lists = [self.items[i][1] for i in sorted(self.chosen) if self.items[i][0] == "list"]
        if not lists:
            self.dev.stoast("msg_notplaylist")
            return

        def go():
            for pl in lists:
                self.win.state.delete_playlist(pl["id"])
            self.end_select()
            self.dev.stoast("msg_delplaylistsuccess")
        self.dev.confirm(self.s("msg_delplaylist"), go)

    def changed(self, what: str) -> None:
        super().changed(what)
        if what == "playlists" and self.title_node is not None and not self.selecting:
            self.title_node.text = self.nav_title()


class PlaylistMoreDialog(Dialog):
    """playlistsmore_dialog: the playlist, 重命名, 删除歌单, 取消."""
    ui = "dialog/playlistsmore_dialog"

    def __init__(self, dev, pl):
        self.pl = pl
        super().__init__(dev)

    def build(self) -> None:
        r, pl = self.root, self.pl
        rows, _ = self.win.state.playlist_rows(pl["id"])
        r["scrlabel_title"].text = pl["name"]
        r["scrlabel_artist"].text = self.s("genre_statistics").replace("%d", str(len(rows)))
        r["label_rename"].text = self.s("rename")
        r["label_del"].text = self.s("msg_delplaylist").rstrip("?" + chr(0xFF1F)).removeprefix(chr(0x662F) + chr(0x5426))   # (the leading "whether")
        r["item_rename"].on_click = self.rename
        r["item_del"].on_click = self.delete
        r["button_return"].text = self.s("msg_no")
        r["button_return"].on_click = self.close

    def rename(self) -> None:
        self.close()
        self.dev.show_dialog(RenameDialog(self.dev, self.pl))

    def delete(self) -> None:
        self.close()
        self.dev.confirm(self.s("msg_delplaylist"), lambda: self.win.state.delete_playlist(
            self.pl["id"], ok=lambda _r: self.dev.stoast("msg_delplaylistsuccess")))


class RenameDialog(Dialog):
    """renameplaylist_dialog: the name in an edit (with ✕ to clear) and the keyboard; 取消 | 确定."""
    ui = "dialog/renameplaylist_dialog"

    def __init__(self, dev, pl):
        self.pl = pl
        super().__init__(dev)
        self.layer.outside = None

    def build(self) -> None:
        from .dialogs import EditField
        r = self.root
        r.props["self_layout"] = "default(x=c,y=20,w=600,h=360)"
        r["hscroll_label"].text = self.s("rename")
        self.edit = EditField(self, r["edit"], "", submit=lambda _t: self.ok())
        self.edit.text = self.pl["name"]
        r["btn_clear"].on_click = lambda: self.edit.set_text("")
        r["btn_cancel"].on_click = self.cancel
        r["btn_enter"].on_click = self.ok
        self.edit.focus()                            # typing works at once; the keyboard comes up next
        QTimer.singleShot(0, self.edit.open_keyboard)

    def cancel(self) -> None:
        self.edit.close_keyboard()
        self.close()

    def ok(self) -> None:
        name = self.edit.text.strip()
        if not name:
            self.dev.stoast("msg_playlistnameerr")
            return
        self.edit.close_keyboard()
        self.close()
        if name != self.pl["name"]:
            self.win.state.rename_playlist(self.pl["id"], name, ok=lambda _r: self.dev.stoast("renamesuccess"))


class AddPlaylistPage(Page):
    """addplaylist_page: 新建歌单 -- the name and 确定 (empty: 名字不能为空; taken: 歌单已存在)."""
    ui = "addplaylist_page"

    def __init__(self, dev, paths=None):
        self.paths = list(paths or [])
        super().__init__(dev, dev.s("playlist_create"))

    def build(self) -> None:
        from .dialogs import EditField
        self.navbar(self.title)
        r = self.root
        self.edit = EditField(self, r["ssedit"], self.s("playlist_create"), submit=lambda _t: self.ok())
        self.edit.text = self.win.state.unique_name()
        r["button"].text = self.s("msg_yes")
        r["button"].on_click = self.ok
        self.edit.focus()                            # typing works at once; the keyboard comes up next
        QTimer.singleShot(0, self.edit.open_keyboard)

    def hidden(self) -> None:
        self.edit.close_keyboard()

    def ok(self) -> None:
        name = self.edit.text.strip()
        if not name:
            self.dev.stoast("msg_addplaylist")
            return
        if any(pl["name"] == name for pl in self.win.state.playlist_list()):
            self.dev.stoast("msg_playlistexist")
            return
        self.edit.close_keyboard()
        paths = self.paths
        self.win.state.create_playlist(name, paths, ok=lambda _p: self.dev.stoast(
            "msg_addplaylistsuccess" if paths else "playlist_create"))
        self.dev.back()
        if paths:
            QTimer.singleShot(0, self.dev.back)            # out of the picking playlist page too


# ====================================================================== search
class SearchPage(Page):
    """search_page: the search field (the keyboard comes up), then 单曲 / 歌手 / 专辑 / 风格 tabs."""
    ui = "search_page"
    TABS = ("track", "artist", "album", "genre")

    def build(self) -> None:
        from .dialogs import EditField
        r = self.root
        r["btn_return"].on_click = self.dev.back
        r["btn_return"].text = ""
        self.edit = EditField(self, r["search_edit"], self.s("keyboard_keyword"), changed=self.query,
                              submit=self.query)
        r["view"].visible = False
        r["label_tip"].text = self.s("search_tips1")
        self.tabs = [r[f"btn_{k}"] for k in self.TABS]
        for i, b in enumerate(self.tabs):
            b.on_click = lambda i=i: self.tab(i)
        r["tab_button_group"].props["children_layout"] = "default(r=1,c=4)"
        self.lists = {}
        for k in self.TABS:
            n = self.root[f"scroll_view_{k}"]
            n.bar = n.parent.find("scroll_bar_m")
            n.rows = RowSource(0, ROW, lambda i, k=k: self.row(k, i))
            self.lists[k] = n
        self.results = {k: [] for k in self.TABS}
        self.current = 0
        self.query("")
        self.edit.focus()                            # typing works at once; the keyboard comes up next
        QTimer.singleShot(0, self.edit.open_keyboard)

    def hidden(self) -> None:
        self.edit.close_keyboard()

    def tab(self, i: int) -> None:
        self.current = i
        for k, b in enumerate(self.tabs):
            b.checked = k == i
        for k, name in enumerate(self.TABS):
            self.root[f"view_{name}"].visible = k == i
        self.dev.screen.update()

    def query(self, text: str) -> None:
        q = (text or "").strip().casefold()
        r = self.root
        r["view_tip"].visible = not q
        r["tab_control"].visible = bool(q)
        if not q:
            self.dev.screen.update()
            return
        tracks = self.dev.view.tracks
        self.results["track"] = [t for t in tracks if q in title_of(t).casefold()]
        self.results["artist"] = [g for g in artist_groups(self.dev, True) if q in g[0].casefold()]
        self.results["album"] = [a for a in sorted(self.dev.view.albums, key=lambda a: a.title.casefold())
                                 if q in a.title.casefold()]
        self.results["genre"] = [g for g in catalog.genre_tracks(tracks) if q in g[0].casefold()]
        for k in self.TABS:
            self.lists[k].rows.reset(len(self.results[k]))
            self.lists[k].scroll_y = 0
            r[f"label_{k}_msg"].visible = not self.results[k]
            r[f"label_{k}_msg"].text = self.s("no_search")
        self.tab(self.current)
        self.relayout()

    def row(self, kind: str, i: int) -> awtk.Node:
        x = self.results[kind][i]
        n = self.lists[kind]
        r = make("list_item", (0, 0, n.w, ROW), "row", "list_itemplaylist_style")
        if kind == "track":
            cur = self.win.player.current
            playing = cur is not None and cur.get("path") == x.get("path")
            thumb(r, self.dev.track_cover(x), "default_track_small")
            two_lines(r, 120, 40, title_of(x), artist_of(x) or self.s("player_unknown"), yellow=playing)
            r.on_click = lambda: self.dev.play(self.results["track"], i)
        elif kind == "artist":
            name, tracks = x
            thumb(r, None, "default_artist_small")
            two_lines(r, 120, 80, name, tr("{0} 首", len(tracks)))
            right_arrow(r)
            r.on_click = lambda: self.dev.open(ArtistInfoPage(self.dev, name, tracks))
        elif kind == "album":
            thumb(r, self.dev.cover(x), "default_album_small")
            two_lines(r, 120, 80, x.title, x.artist or self.s("player_unknown"))
            right_arrow(r)
            r.on_click = lambda: self.dev.open(AlbumInfoPage(self.dev, x))
        else:
            name, tracks = x
            thumb(r, None, "default_genre_small")
            two_lines(r, 120, 80, name, self.s("genre_statistics").replace("%d", str(len(tracks))))
            right_arrow(r)
            r.on_click = lambda: self.dev.open(TrackListPage(self.dev, "genre", lambda dev: list(tracks), title=name))
        return r

    def changed(self, what: str) -> None:
        super().changed(what)
        if what in ("library",):
            self.query(self.edit.text)
        elif what in ("track", "covers"):
            for n in self.lists.values():
                n.rows.reset()


# ====================================================================== 更新曲库
class UpdateMusicPage(Page):
    """updatemusic_page: 自动扫描音乐 (kept), 立即扫描音乐, 指定文件夹扫描."""
    ui = "updatemusic_page"

    def build(self) -> None:
        r = self.root
        self.navbar(self.s("upmusic"))
        texts = ("scan_automatic", "scan_now", "scan_spec")
        for i, key in enumerate(texts):
            r[f"hscroll_label_{i}"].text = self.s(key)
            r[f"list_item_{i}"].raw = (0, 0, 0, 108)
        r["list_item_0"].on_click = self.auto
        r["list_item_1"].on_click = lambda: self.dev.show_dialog(UpdateMusicDialog(self.dev))
        r["list_item_2"].on_click = lambda: self.dev.open(SpecFolderPage(self.dev))
        self.fill()

    def fill(self) -> None:
        self.root["image_switch"].image = "switch_on" if int(self.dev.cfg("AUTOSCAN")) else "switch_off"
        self.dev.screen.update()

    def auto(self) -> None:
        self.dev.set_cfg("AUTOSCAN", 0 if int(self.dev.cfg("AUTOSCAN")) else 1)
        self.fill()


class UpdateMusicDialog(Dialog):
    """updatemusic_dialog: the scanning animation, 正在更新第%d首, the folder; 取消 stops it. Runs the
    window's own 更新曲库 (the same scan as the normal window's F5)."""
    ui = "dialog/updatemusic_dialog"

    def __init__(self, dev, roots: list[str] | None = None):
        self.roots = roots
        self.started = False
        super().__init__(dev)
        self.layer.outside = None
        self.layer.dim = 0
        QTimer.singleShot(0, self.start)

    def build(self) -> None:
        r = self.root
        anim = r["img_animation"]
        anim.props.update({"start_index": "1", "end_index": "12", "interval": "80"})
        anim.data = {"running": True}
        r["btn_no"].text = self.s("msg_no")
        r["btn_no"].on_click = self.cancel
        r["label_msg"].text = self.s("scan_startscan")
        r["label_scanfolder"].text = "  ".join(Path(p).name for p in (self.roots or self.win.music_roots()))

    def start(self) -> None:
        st = self.win.state
        roots = [p for p in (self.roots or self.win.music_roots()) if Path(p).is_dir()]
        if not roots:
            self.close()
            self.dev.toast(tr("还没有音乐文件夹（在普通模式的「文件夹」页添加）"))
            return
        if getattr(st, "busy", None) and st.busy != "scan":
            self.close()
            self.dev.toast(tr("正在处理另一项任务，稍后再扫描。"))
            return
        if getattr(st, "busy", None) != "scan":
            self.win.start_scan(roots)
        self.started = True

    def cancel(self) -> None:
        st = self.win.state
        if getattr(st, "busy", None) == "scan":
            st.cancel_job()
        self.close()

    def changed(self, what: str) -> None:
        if what != "scan" or not self.started:
            return
        st = self.win.state
        prog = self.dev.scan_progress or {}
        r = self.root
        if getattr(st, "busy", None) == "scan":
            cur = prog.get("current") if isinstance(prog, dict) else None
            if cur:
                r["label_msg"].text = self.s("scan_updating").replace("%d", str(cur))
            return
        r["img_animation"].data = {"running": False}
        r["label_msg"].text = self.s("scan_updatefinish").replace("%d", str(len(self.dev.view.tracks)))
        QTimer.singleShot(1500, self.close)


class SpecFolderPage(ListPage):
    """specfolder_page: tick music folders, 开始扫描 scans just those (未指定文件夹 when none)."""
    ui = "specfolder_page"
    client = "specfolder_table_client"
    can_select = False

    def build(self) -> None:
        r = self.root
        self.selecting = True
        r["btn_startscan"].text = self.s("scan_startscan")
        r["btn_startscan"].on_click = self.scan
        super().build()

    def set_nav(self) -> None:
        bar = self.root.find("navigationbar_view")
        keep = {"navigation_btn_allchoice", "btn_cancel"}
        bar.children = [c for c in bar.children if c.name in keep]
        ret = make("button", (0, 0, 105, 82), "btn_return", "navbar_btnreturn_style", parent=bar)
        ret.on_click = self.dev.back
        self.title_node = make("hscroll_label", (118, 0, 400, 82), "scrlabel_title", "msg_leftwhite36_style",
                               parent=bar, text=self.s("scan_spec"))
        every = self.items and len(self.chosen) == len(self.items)
        bar["navigation_btn_allchoice"].style = "navbar_btnselected_style" if every else "navbar_btnunselect_style"
        bar["navigation_btn_allchoice"].on_click = self.select_all
        bar["btn_cancel"].on_click = lambda: (setattr(self, "chosen", set()), self.set_nav(), self.refresh_rows())

    def collect(self) -> list:
        return list(self.win.music_roots())

    def row(self, i: int) -> awtk.Node:
        path = self.items[i]
        r = table_row(self.list.w)
        choice(r, i in self.chosen)
        thumb(r, None, "icon_folder", 84)
        two_lines(r, 184, 40, Path(path).name or path, path)
        r.on_click = lambda: self.toggle(i)
        return r

    def end_select(self) -> None:
        self.chosen = set()
        self.set_nav()
        self.refresh_rows()

    def scan(self) -> None:
        roots = [self.items[i] for i in sorted(self.chosen)]
        if not roots:
            self.dev.stoast("msg_notspecdir")
            return
        self.dev.back()
        self.dev.show_dialog(UpdateMusicDialog(self.dev, roots))


__all__ = ["HomePage", "ALL_HOME", "DEFAULT_HOME", "MENU_ORDER", "home_items", "default_home", "AllMusicPage",
           "TrackListPage", "AlbumsPage", "AlbumInfoPage", "JumpInfoPage", "ArtistsPage", "ArtistInfoPage",
           "GenresPage", "FolderPage", "PlaylistsPage", "SearchPage", "UpdateMusicPage", "AddPlaylistPage",
           "title_of", "artist_of", "ext", "human_size", "FORMAT_ICON", "artist_groups"]
