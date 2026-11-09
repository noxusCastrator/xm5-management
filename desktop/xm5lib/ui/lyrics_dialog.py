"""Optional lyric association/search controls using the existing backend lanes."""
from pathlib import Path
import uuid
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QListWidget,QListWidgetItem,QPlainTextEdit,QFileDialog)
from .i18n import tr

class LyricsDialog(QDialog):
    def __init__(self,win,track_id):
        super().__init__(win)
        self.win=win;self.backend=win.backend;self.track_id=track_id;self.job_id=None;self.closed=False;self.busy=False;self.preview_sequence=0
        self.setWindowTitle(tr("歌词管理"));self.resize(760,570)
        layout=QVBoxLayout(self)
        track=win.state.track(track_id) or {}
        self.heading=QLabel(str(track.get('title') or Path(track.get('path') or '').stem));layout.addWidget(self.heading)
        row=QHBoxLayout();layout.addLayout(row)
        for text,action in [(tr("读取已有歌词"),self.inspect),(tr("在线搜索"),self.search),(tr("导入歌词文件…"),self.import_file)]:
            button=QPushButton(text);button.clicked.connect(action);row.addWidget(button)
        self.status=QLabel(tr("保留原有歌词；在线搜索只发送曲目信息。"));self.status.setWordWrap(True);layout.addWidget(self.status)
        self.candidates=QListWidget();self.candidates.setMaximumHeight(140);self.candidates.currentItemChanged.connect(self.preview);layout.addWidget(self.candidates)
        self.text=QPlainTextEdit();self.text.setReadOnly(True);layout.addWidget(self.text,1)
        row=QHBoxLayout();layout.addLayout(row)
        self.apply=QPushButton(tr("使用选中歌词"));self.apply.setObjectName('primary');self.apply.setEnabled(False);self.apply.clicked.connect(self.attach);row.addWidget(self.apply)
        self.remove=QPushButton(tr("解除歌词关联"));self.remove.clicked.connect(lambda:self.request('lyrics.remove',{}));row.addWidget(self.remove)
        self.cancel=QPushButton(tr("取消查找"));self.cancel.clicked.connect(self.cancel_job);self.cancel.setEnabled(False);row.addWidget(self.cancel)
        self.finished.connect(self.finish);self.inspect()

    def finish(self,*args):
        self.closed=True;self.cancel_job()

    def cancel_job(self):
        if self.job_id:self.backend.command('jobs.cancel',{'id':self.job_id},lane='jobs')

    def request(self,name,args):
        if self.busy:return
        self.busy=True
        self.status.setText(tr("正在处理…"));self.cancel.setEnabled(True)
        def accepted(result):
            job=result.get('job_id')
            if self.closed:
                if job:self.backend.command('jobs.cancel',{'id':job},lane='jobs')
                return
            if job:
                self.job_id=job;self.backend.watch(job,done=self.completed,failed=self.failed)
            else:self.completed(result)
        self.backend.command(name,{'track_id':self.track_id,**args},ok=accepted,err=self.failed,lane='jobs')

    def failed(self,error):
        self.job_id=None;self.busy=False
        if not self.closed:self.cancel.setEnabled(False);self.status.setText(str(getattr(error,'message',error)))

    def completed(self,result):
        self.job_id=None;self.busy=False
        if self.closed:return
        self.cancel.setEnabled(False)
        if result.get('status')=='succeeded' and 'result' in result:result=result['result']
        self.status.setText(tr("处理完成"))
        records=result.get('candidates') if 'candidates' in result else result.get('sources')
        if records is not None:
            self.candidates.clear()
            for value in records:
                source=value.get('preferred_source_id') or value.get('source_id') or value.get('id')
                label=value.get('title') or value.get('label') or value.get('provider') or value.get('kind') or source or ''
                details=[label,value.get('artist'),value.get('album'),value.get('language'),value.get('kind')]
                duration=value.get('duration')
                if isinstance(duration,(float,int)):details.append(str(round(duration,2))+' s')
                if value.get('strict_match'):details.append(tr('严格匹配'))
                item=QListWidgetItem(' · '.join(str(x) for x in details if x));item.setData(Qt.UserRole,{'candidate_id':value.get('candidate_id') or source,'source_id':source});self.candidates.addItem(item)
            if self.candidates.count():
                selected=(result.get('selected') or {}).get('source_id')
                preferred=next((i for i in range(self.candidates.count()) if self.candidates.item(i).data(Qt.UserRole).get('source_id')==selected),0)
                self.candidates.setCurrentRow(preferred)
            else:self.status.setText(tr("没有找到匹配歌词"));self.text.clear()
        else:self.load_current()

    def inspect(self):self.request('lyrics.inspect',{})
    def search(self):self.request('lyrics.search',{})
    def load_current(self):
        self.preview_sequence+=1;sequence=self.preview_sequence
        self.backend.query('lyrics.get',{'track_id':self.track_id},ok=lambda value:self.show_text(value) if sequence==self.preview_sequence else None,err=lambda error:self.preview_error(error,sequence))
    def preview(self,item,previous=None):
        self.preview_sequence+=1;sequence=self.preview_sequence
        self.apply.setEnabled(item is not None)
        self.text.clear()
        if item is None:return
        value=item.data(Qt.UserRole)
        if value.get('source_id'):self.backend.query('lyrics.get',{'track_id':self.track_id,'source_id':value['source_id']},ok=lambda result:self.show_text(result) if sequence==self.preview_sequence else None,err=lambda error:self.preview_error(error,sequence))
    def preview_error(self,error,sequence):
        if not self.closed and sequence==self.preview_sequence:self.status.setText(str(getattr(error,"message",error)))
    def show_text(self,value):
        if not self.closed:self.text.setPlainText(value.get('normalized_text') or value.get('synced_text') or value.get('plain_text') or value.get('text') or '')
    def attach(self):
        item=self.candidates.currentItem()
        if item:self.request('lyrics.attach',item.data(Qt.UserRole))
    def import_file(self):
        selected,_=QFileDialog.getOpenFileName(self,tr("选择歌词文件"),'',tr("歌词文件 (*.lrc *.srt *.txt)"))
        if not selected:return
        try:
            from ..core.restore_card import no_links
            path=no_links(selected)
            if path.is_symlink() or not path.is_file() or path.stat().st_size>1024*1024:raise ValueError(tr("歌词文件不可读取或超过大小限制"))
            data=path.read_bytes();folder=self.win.state.staging/'lyrics'/uuid.uuid4().hex;folder.mkdir(parents=True)
            target=folder/path.name
            with target.open('xb') as output:output.write(data)
        except (OSError,ValueError) as exc:self.failed(exc);return
        self.request('lyrics.import',{'path':str(target)})

def install_lyrics(win):
    win._lyrics_dialogs=[]
    def open_dialog(tracks):
        if len(tracks)!=1:return
        track=tracks[0];key=win.state.id_for(track.get('path')) or track.get('id')
        if not key:return
        dialog=LyricsDialog(win,key);win._lyrics_dialogs.append(dialog)
        dialog.finished.connect(lambda *_:win._lyrics_dialogs.remove(dialog) if dialog in win._lyrics_dialogs else None)
        dialog.show()
    win.inspector.lyrics_requested.connect(open_dialog)
