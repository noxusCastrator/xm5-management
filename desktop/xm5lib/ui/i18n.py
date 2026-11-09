"""Chinese / English labels for the window.

Chinese is the source text in the code: ``tr("文件夹")``, ``tr("{0} 首", n)``. English comes from
``lang_en.EN``, written after the XM5 firmware's own en_US string table (Folder, Album, Artist, Genres,
My Fav, All Songs, Playlist, Update Library, List Play, Repeat All ...): the window imitates the player,
so the player's wording wins. The language is chosen in Settings and applies when the window starts.
"""
from __future__ import annotations

LANG = "zh"
LANGUAGES = [("zh", "简体中文"), ("en", "English")]      # shown in their own language, like the XM5


def set_language(lang: str) -> None:
    global LANG
    LANG = lang if lang in dict(LANGUAGES) else "zh"


def tr(text: str, *args, **kw) -> str:
    if LANG == "en":
        from .lang_en import EN
        text = EN.get(text, text)
    return text.format(*args, **kw) if (args or kw) else text


__all__ = ["tr", "set_language", "LANG", "LANGUAGES"]
