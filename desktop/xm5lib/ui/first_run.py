"""Optional source setup after the ordinary window opens, never from setup.exe."""
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QListWidget, QPushButton, QVBoxLayout

from . import i18n


def show_first_run(win):
    def text(english, chinese):
        return english if i18n.LANG == "en" else chinese

    dialog = QDialog(win)
    dialog.setWindowTitle(text("Welcome to XM5 Studio", "欢迎使用 XM5 Studio"))
    dialog.setMinimumWidth(560)
    layout = QVBoxLayout(dialog)
    intro = QLabel(text("Choose music and optional PEQ folders. You can skip this and add folders later in Folders and PEQ.",
                        "选择音乐文件夹和可选的 PEQ 文件夹。也可以跳过，稍后在「文件夹」和「PEQ 预设」中添加。"))
    intro.setWordWrap(True)
    layout.addWidget(intro)
    lists = {}
    for kind, label in (("music", text("Music folders", "音乐文件夹")), ("peq", text("PEQ folders (optional)", "PEQ 文件夹（可选）"))):
        layout.addWidget(QLabel(label))
        listing = QListWidget()
        listing.setAccessibleName(label)
        layout.addWidget(listing)
        lists[kind] = listing
        row = QHBoxLayout()
        add = QPushButton(text("Add folder…", "添加文件夹…"))
        remove = QPushButton(text("Remove selected", "移除所选"))

        def choose(checked=False, listing=listing, label=label):
            folder = QFileDialog.getExistingDirectory(dialog, label)
            if folder and folder not in [listing.item(n).text() for n in range(listing.count())]:
                listing.addItem(folder)

        add.clicked.connect(choose)
        remove.clicked.connect(lambda checked=False, listing=listing: listing.takeItem(listing.currentRow()))
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch()
        layout.addLayout(row)
    buttons = QDialogButtonBox()
    save = buttons.addButton(text("Save folders", "保存文件夹"), QDialogButtonBox.AcceptRole)
    buttons.addButton(text("Skip for now", "暂时跳过"), QDialogButtonBox.RejectRole)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    save.setDefault(True)
    layout.addWidget(buttons)
    if dialog.exec() == QDialog.Accepted:
        # Registration is metadata only. Normal library controls initiate scans.
        for kind, listing in lists.items():
            for n in range(listing.count()):
                win.prefs.register_source(kind, listing.item(n).text())
        win.state.reindex()
        win.pages["peq"]._show_peace_dir()
        win.pages["export"].refresh_sources()
        win.restart_backend()
        win.show_page("folders")
