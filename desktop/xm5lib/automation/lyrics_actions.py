"""Lyrics command implementation; no network or scan starts at module import."""
from .engine import DomainError

class LyricsActions:
    def __init__(self, engine):
        from ..core.lyrics import LyricsStore
        self.engine = engine
        self.store = LyricsStore(engine.home)

    def track(self, key, *, inspect=False):
        track = self.engine.application.query('tracks.get', {'id':key})['track']
        if inspect and track.get('path'):
            self.engine._under(track['path'],self.engine.music_roots)
        return track

    def query(self, name, args):
        if name == 'lyrics.get':
            self.track(args['track_id'])
            return self.store.get(args['track_id'],args.get('source_id'))
        if name == 'lyrics.list':
            return {'records':self.store.list(args.get('track_id'))}
        raise DomainError('UNKNOWN_QUERY','Unknown lyrics query',{})

    def command(self, name, args, progress, cancel):
        key=args.get('track_id')
        if name == 'lyrics.fetch_missing':
            ids=args.get('track_ids') or ([key] if key else [])
            if not isinstance(ids,list) or not 1<=len(ids)<=200:
                raise DomainError('INVALID_ARGUMENT','Choose1..200 tracks per lyrics job',{})
            rows=[]
            for i,tid in enumerate(ids):
                if cancel.is_set():raise InterruptedError('Lyrics job cancelled')
                progress({'phase':'lyrics','current':i,'total':len(ids)})
                rows.append(self.store.fetch_missing(self.track(tid,inspect=True),enabled=bool(args.get('enabled',False)),cancel=cancel))
            return {'results':rows,'count':len(rows)}
        if not isinstance(key,str):raise DomainError('INVALID_ARGUMENT','track_id is required',{})
        track=self.track(key,inspect=name=='lyrics.inspect')
        if cancel.is_set():raise InterruptedError('Lyrics job cancelled')
        if name=='lyrics.inspect':return self.store.inspect_track(track,cancel=cancel)
        if name=='lyrics.search':return self.store.search(track,cancel=cancel)
        if name=='lyrics.attach':return self.store.attach(key,args['candidate_id'],source_id=args.get('source_id'),explicit=True)
        if name=='lyrics.remove':return self.store.remove_association(key,args.get('source_id'))
        if name=='lyrics.import':
            source=self.engine._under(args['path'],self.engine.music_roots+self.engine.import_roots)
            return self.store.import_file(key,source,language=args.get('language','und'),encoding=args.get('encoding'))
        raise DomainError('UNKNOWN_COMMAND','Unknown lyrics command',{})

    def for_export(self, tracks, *, auto=False,budget=20,progress=None,cancel=None):
        if type(auto) is not bool or type(budget) is not int or not 0<=budget<=200:
            raise DomainError('INVALID_ARGUMENT','Invalid automatic lyrics search options',{})
        reports=[];searched=0
        for i,track in enumerate(tracks):
            if cancel is not None and cancel.is_set():raise InterruptedError('Lyrics collection cancelled')
            if progress:progress({'phase':'lyrics','current':i,'total':len(tracks),'path':track.get('path')})
            inspected=self.store.inspect_track(track,cancel=cancel)
            if auto and not inspected.get('selected') and searched<budget:
                searched+=1
                reports.append(self.store.fetch_missing(track,enabled=True,cancel=cancel))
        entries=self.store.plan_export_entries([t['id'] for t in tracks])
        warnings=[]
        if auto and searched>=budget:
            warnings.append({'code':'lyrics_search_budget','message':'Automatic lyrics search reached its per-export budget; existing lyrics are retained. Use the lyrics search action for further tracks.'})
        return entries,warnings,reports
