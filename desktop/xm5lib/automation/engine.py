"""One backend/application owner with durable async job and export-plan records."""
import concurrent.futures
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from ..application import Application, DomainError
from .script_actions import ScriptActions
from .storage_actions import StorageActions
from .device_state_actions import DeviceStateActions


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


class Engine:
    def __init__(self, home, *, music_roots=(), export_roots=(), import_roots=(),mpv_path=None,allow_null_playback=False):
        self.home = Path(home); self.home.mkdir(parents=True, exist_ok=True)
        self.application = Application(self.home)
        self.music_roots = [Path(p).resolve() for p in music_roots]
        self.export_roots = [Path(p).resolve() for p in export_roots]
        self.import_roots = [Path(p).resolve() for p in import_roots]
        self.lock = threading.RLock()
        self.card_lock = threading.RLock()
        self.lyrics_lock = threading.RLock()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix='xm5-job')
        self.cancels = {}
        self.mpv_path=mpv_path;self.allow_null_playback=allow_null_playback
        self._lyrics_actions=None
        self.playback=None;self.instance=str(uuid.uuid4());self.playback_epoch=str(uuid.uuid4())
        self.db = self.home / 'automation.sqlite3'
        with self.connect() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, key TEXT UNIQUE,
                digest TEXT, status TEXT, result TEXT, error TEXT, progress TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY,digest TEXT,body TEXT);
                CREATE TABLE IF NOT EXISTS command_keys(key TEXT PRIMARY KEY,digest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS playback_receipts(key TEXT PRIMARY KEY,instance TEXT,response TEXT);
                CREATE TABLE IF NOT EXISTS sync_baselines(build TEXT,playlist TEXT,body TEXT,PRIMARY KEY(build,playlist));''')
            c.execute("UPDATE jobs SET status='interrupted',error='Backend stopped before completion' WHERE status IN ('queued','running','cancel_requested')")

    def storage(self):
        # Trusted retained-input regeneration; public JSON cannot supply code.
        from ..core.information_repair import InformationRepairGenerator
        return StorageActions(self,repair_generator=InformationRepairGenerator(peak_cache_dir=self.home/'cache'/'sample-peaks-v1'))

    def lyrics(self):
        if self._lyrics_actions is None:
            from .lyrics_actions import LyricsActions
            self._lyrics_actions = LyricsActions(self)
        return self._lyrics_actions

    @contextmanager
    def connect(self):
        connection=sqlite3.connect(self.db, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _under(path, roots):
        from ..core.restore_card import no_links
        path = no_links(path).resolve()
        if not any(path == r or r in path.parents for r in roots):
            raise DomainError('PATH_NOT_ALLOWED', 'Path is outside configured roots', {'path': str(path)})
        return path

    def query(self, name, args):
        if name == 'device_state.inspect':
            return DeviceStateActions(self).dispatch(name, args)
        if name in ('scripts.capture','scripts.format','scripts.convert'):
            return ScriptActions(self).dispatch(name,args)
        if name in StorageActions.QUERY_NAMES:
            return self.storage().query(name,args)
        if name == 'capabilities':
            return {'application': self.application.capabilities(), 'cards': True, 'playback': bool(self.mpv_path),
                    'inline_information_format': 'XM5I', 'inline_storage': ['export','organize','rebuild'],
                    'device_favorites_return': True, 'device_state_version': 1, 'firmware_write': False,'card_layout_version':3,'information_format':'XM5I','legacy_information_format':'XMI1','peak_correction':'original_sample_peak','script_source_writes':'explicit_exact_source_plan','lyrics':True,'lyrics_online_provider':'LRCLIB','source_writes':False}
        if name in ('lyrics.get','lyrics.list'):
            return self.lyrics().query(name,args)
        if name=='playback.status':
            return self.playback.status() if self.playback else {'connected':False,'available':bool(self.mpv_path)}
        if name=='playback.outputs':
            if not self.mpv_path:raise DomainError('UNSUPPORTED_CAPABILITY','No mpv executable was configured',{})
            if self.playback:return self.playback.output_inventory()
            # Explicit inventory request uses an owned NULL sink, never the default speaker.
            from ..core.mpv_session import Session
            with Session(self.mpv_path,test_null=True) as session:return session.output_inventory()
        if name=='peace.inspect':
            from ..core import peace
            directory=self._under(args['directory'],self.import_roots+self.music_roots)
            rows=peace.read_folder(directory)
            return {'profiles':[{'name':r.name,'ok':r.ok,'reasons':r.reasons,'notes':r.notes,
                                  'sha256':r.sha256,'preset':r.preset.to_dict() if r.ok else None} for r in rows]}
        if name == 'jobs.get':
            with self.connect() as c:
                row = c.execute('SELECT id,status,result,error,progress FROM jobs WHERE id=?', (args['id'],)).fetchone()
            if not row:
                raise DomainError('NOT_FOUND', 'Job not found', {})
            return dict(zip(('id','status','result','error','progress'),
                            (row[0],row[1],json.loads(row[2]) if row[2] else None,row[3],
                             json.loads(row[4]) if row[4] else None)))
        if name == 'plans.get':
            with self.connect() as c:
                row = c.execute('SELECT digest,body FROM plans WHERE id=?', (args['id'],)).fetchone()
            if not row:
                raise DomainError('NOT_FOUND', 'Plan not found', {})
            return {'id': args['id'], 'digest': row[0], 'plan': json.loads(row[1])}
        return self.application.query(name, args)

    def command(self, name, args, revision, key):
        if not isinstance(key, str) or not 1 <= len(key) <= 200:
            raise DomainError('INVALID_ARGUMENT', 'A bounded idempotency key is required', {})
        if name=='playlists.cover' and args.get('path'):
            self._under(args['path'],self.music_roots+self.import_roots)
        if name=='library.refresh':                     # frontend addition: only files inside the music roots
            for p in args.get('paths') or []:
                self._under(p,self.music_roots)
        with self.lock,self.connect() as c:
            fingerprint=digest([name,args,revision])
            previous=c.execute('SELECT digest FROM command_keys WHERE key=?',(key,)).fetchone()
            if previous and previous[0]!=fingerprint:
                raise DomainError('IDEMPOTENCY_CONFLICT','Key belongs to another command',{})
            c.execute('INSERT OR IGNORE INTO command_keys VALUES(?,?)',(key,fingerprint))
        if name.startswith('playback.'):
            with self.lock:
                with self.connect() as c:
                    old=c.execute('SELECT instance,response FROM playback_receipts WHERE key=?',(key,)).fetchone()
                    if old:
                        if old[0]!=self.playback_epoch:
                            raise DomainError('PLAYBACK_SESSION_CHANGED','Do not retry playback across sessions',{})
                        if old[1] is None:
                            raise DomainError('PLAYBACK_OUTCOME_UNKNOWN','Inspect current playback before another action',{})
                        return json.loads(old[1])
                    # Durable intent precedes IPC. If acknowledgement/receipt persistence
                    # fails, the same request must never send next/enqueue twice.
                    c.execute('INSERT INTO playback_receipts VALUES(?,?,NULL)',(key,self.playback_epoch))
                try:
                    result=self._playback(name,args)
                except DomainError:
                    raise
                except Exception as exc:
                    raise DomainError(getattr(exc,'code','PLAYBACK_FAILED').upper(),str(exc),getattr(exc,'details',{})) from exc
                with self.connect() as c:
                    c.execute('UPDATE playback_receipts SET instance=?,response=? WHERE key=?',
                              (self.playback_epoch,json.dumps(result),key))
                return result
        if name == 'jobs.cancel':
            event = self.cancels.get(args['id'])
            if event:
                event.set()
                with self.connect() as c:
                    c.execute("UPDATE jobs SET status='cancel_requested' WHERE id=? AND status IN ('queued','running')", (args['id'],))
            return self.query('jobs.get', args)
        if name in ('scripts.plan','scripts.execute','device_state.plan','device_state.apply') or name in StorageActions.SUBMIT_NAMES:
            return self._submit(name,args,revision,key)
        if name in ('library.scan', 'library.relink_root', 'library.import_legacy') or name.startswith('cards.') or name.startswith('lyrics.'):
            return self._submit(name, args, revision, key)
        if name=='presets.import':
            from ..core import peq
            try:
                preset,notes=peq.parse_apo_text(args['text'],args['name'])
                if notes:raise ValueError('Preset contains ignored commands')
            except (ValueError,KeyError) as exc:
                raise DomainError('UNSUPPORTED_PRESET',str(exc),{}) from exc
            return self.application.execute('presets.save',{'preset':preset.to_dict()},
                                            expected_revision=revision,idempotency_key=key)
        return self.application.execute(name, args, expected_revision=revision, idempotency_key=key)

    def _playback(self,name,args):
        if name=='playback.connect':
            if not self.mpv_path:
                raise DomainError('UNSUPPORTED_CAPABILITY','No mpv executable was configured',{})
            if self.playback:
                raise DomainError('PLAYBACK_CONNECTED','Disconnect the current session first',{})
            if args.get('test_null') and not self.allow_null_playback:
                raise DomainError('UNSUPPORTED_CAPABILITY','NULL audio mode was not enabled',{})
            from ..core.mpv_session import Session
            self.playback=Session(self.mpv_path,output_device=args.get('output_device'),test_null=args.get('test_null',False))
            self.playback_epoch=str(uuid.uuid4())
            return self.playback.status()
        if name=='playback.disconnect':
            if self.playback:self.playback.close()
            self.playback=None;self.playback_epoch=str(uuid.uuid4());return {'connected':False}
        if not self.playback:
            raise DomainError('PLAYBACK_UNAVAILABLE','Connect an explicit output session first',{})
        arguments=dict(args.get('arguments') or {})
        action=args['action']
        if action == 'test_tone':
            from .playback_test_tone import test_tone
            return test_tone(self, arguments)
        if action in ('play_tracks','queue.replace','queue.enqueue'):
            ids=arguments.pop('track_ids',None)
            if ids is not None:
                tracks = [self.application.query('tracks.get', {'id': tid})['track'] for tid in ids]
                arguments['paths'] = [track['path'] for track in tracks if not track.get('unresolved')]
            for path in arguments.get('paths',[]):self._under(path,self.music_roots)
        if action=='peq.set' and 'preset_name' in arguments:
            presets=self.application.query('presets.list',{})['presets']
            name=arguments.pop('preset_name')
            preset=next((p for p in presets if p['name']==name),None)
            if preset is None:raise DomainError('NOT_FOUND','Preset not found',{})
            arguments['preset']=preset
        return self.playback.command(action,arguments)

    def _submit(self, name, args, revision, key):
        fingerprint = digest([name, args, revision])
        with self.lock, self.connect() as c:
            row = c.execute('SELECT id,digest FROM jobs WHERE key=?', (key,)).fetchone()
            if row:
                if row[1] != fingerprint:
                    raise DomainError('IDEMPOTENCY_CONFLICT', 'Key belongs to another request', {})
                return {'job_id': row[0]}
            job_id = str(uuid.uuid4())
            c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)',
                      (job_id,key,fingerprint,'queued',None,None,None,time.time()))
        cancel = self.cancels[job_id] = threading.Event()
        self.pool.submit(self._run, job_id, name, args, revision, key, cancel)
        return {'job_id': job_id}

    def _run(self, job_id, name, args, revision, key, cancel):
        def progress(*values):
            with self.connect() as c:
                c.execute('UPDATE jobs SET progress=? WHERE id=?', (json.dumps(values[0] if len(values)==1 else values), job_id))
        try:
            with self.connect() as c:
                c.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
            if cancel.is_set():
                raise InterruptedError('Cancelled before execution')
            if name == 'library.scan':
                for root in args.get('roots', []):
                    self._under(root, self.music_roots)
                result = self.application.execute(name, args, expected_revision=revision, idempotency_key=key,cancel=cancel,progress=progress)
            elif name in ('scripts.plan','scripts.execute'):
                result=ScriptActions(self).dispatch(name,args,revision,key)
            elif name in ('device_state.plan','device_state.apply'):
                with self.card_lock:
                    if cancel.is_set():raise InterruptedError('Cancelled before state intake admission')
                    result=DeviceStateActions(self).dispatch(name,args,revision,key)
            elif name in StorageActions.SUBMIT_NAMES:
                with self.card_lock:
                    if cancel.is_set():raise InterruptedError('Cancelled before storage admission')
                    result=self.storage().submit(name,args,progress,cancel)
                if result.get('returns'):
                    with self.connect() as c:
                        c.execute('CREATE TABLE IF NOT EXISTS storage_returns(job TEXT PRIMARY KEY,body TEXT)')
                        c.execute('INSERT OR REPLACE INTO storage_returns VALUES(?,?)',(job_id,json.dumps(result['returns'])))
                    result['return_intake']='retained_in_job_and_transaction; explicit library import required'
            elif name.startswith('lyrics.'):
                with self.lyrics_lock:
                    result = self.lyrics().command(name,args,progress,cancel)
            elif name == 'library.relink_root':
                self._under(args['new_root'], self.music_roots)
                result = self.application.execute(name, args, expected_revision=revision, idempotency_key=key, cancel=cancel)
            elif name == 'library.import_legacy':
                self._under(args['path'], self.import_roots)
                result = self.application.execute(name, args, expected_revision=revision, idempotency_key=key)
            else:
                with self.card_lock:
                    if cancel.is_set():raise InterruptedError('Cancelled before card admission')
                    if name in ('cards.plan','cards.execute'):
                        with self.application._write_lock:
                            result = self._cards(name, args, progress, cancel,revision,key)
                    else:
                        result = self._cards(name, args, progress, cancel,revision,key)
            with self.connect() as c:
                status='cancelled' if result.get('status')=='cancelled' else 'failed' if (result.get('status') in ('failed','partial','incomplete','interrupted','recovery_required') or result.get('recovery_required') or (name=='scripts.execute' and result.get('status')=='rolled_back')) else 'succeeded'
                c.execute("UPDATE jobs SET status=?,result=? WHERE id=?", (status,json.dumps(result),job_id))
        except Exception as exc:
            with self.connect() as c:
                c.execute('UPDATE jobs SET status=?,error=? WHERE id=?',
                          ('cancelled' if isinstance(exc, InterruptedError) or getattr(exc,'code','').upper()=='CANCELLED' else 'failed',
                           f'{getattr(exc,"code",type(exc).__name__)}: {exc}',job_id))
        finally:
            self.cancels.pop(job_id, None)

    def _cards(self, name, args, progress, cancel,revision=None,key=None):
        from ..core import card_library
        from ..core.inline_card import library as inline_library
        if name == "cards.recover_update":
            control = self._under(args["control_directory"], self.export_roots)
            if (control / inline_library.ACTIVATION).is_file():
                return inline_library.recover_activation(control)
        inline = name == "cards.plan" and args.get("format", "XM5I") == "XM5I"
        if name == "cards.plan" and args.get("format", "XM5I") not in ("XM5I", "XMI1"):
            raise DomainError("INVALID_ARGUMENT", "Card format must be XM5I or the legacy XMI1 profile", {})
        if name == "cards.execute":
            inline = "inline_plan" in self.query("plans.get", {"id": args["plan_id"]})["plan"]
        elif name != "cards.plan" and args.get("root"):
            selected = Path(args["root"])
            inline = (selected / inline_library.MANIFEST).is_file() or (selected / "Struct/library.json").is_file()
        if inline:
            from .inline_library_actions import InlineLibraryActions
            return InlineLibraryActions(self).dispatch(name, args, progress, cancel, revision, key)
        if name == 'cards.plan':
            args=dict(args)
            args.pop("format", None)
            if 'acknowledged_device_state' in args:
                raise DomainError('INVALID_ARGUMENT','Device-state acknowledgement is private',{})
            for root in args['roots']:
                self._under(root, self.music_roots)
            self._under(args['destination'], self.export_roots)
            for source in args.get('peq_roots',[]):
                self._under(source,self.import_roots+self.music_roots)
            for image in (args.get('playlist_covers') or {}).values():
                self._under(image, self.music_roots+self.import_roots)
            snapshot=self.application.query('tracks.all',{})
            args['track_ids']={loc['path']:t['id'] for t in snapshot['tracks'] for loc in t.get('locations',[]) if loc.get('present')}
            args['track_ids'].update({t['path']:t['id'] for t in snapshot['tracks'] if t.get('path')})
            selected=args.pop('playlist_ids',[])
            if selected:
                args['playlists']=[self.application.query('playlists.get',{'id':pid})['playlist'] for pid in selected]
                covers=dict(args.get('playlist_covers') or {})
                for playlist in args['playlists']:
                    if playlist.get('cover') and playlist['id'] not in covers:
                        covers[playlist['id']]=playlist['cover']['path']
                args['playlist_covers']=covers
            preset_names=args.pop('preset_names',[])
            if preset_names:
                from ..core import peq
                available={p['name']:p for p in self.application.query('presets.list',{})['presets']}
                args['presets']={name:peq.to_apo_text(peq.Preset.from_dict(available[name])) for name in preset_names}
            from ..core.preset_sources import collect_presets
            converted=collect_presets(args.get('peq_roots',[]),args.get('peq_source_ids'),cancel)
            args['presets']=converted['presets'] | (args.get('presets') or {})
            args['preset_metadata']=converted['metadata']
            args['source_warnings']=converted['warnings']
            selected_roots=[Path(p).resolve() for p in args['roots']]
            selected_tracks=[]
            for track in snapshot['tracks']:
                locations=[loc['path'] for loc in track.get('locations',[]) if loc.get('present')]
                if track.get('path'):locations.insert(0,track['path'])
                path=next((path for path in locations if any(Path(path).resolve().is_relative_to(root) for root in selected_roots)),None)
                if path:selected_tracks.append(dict(track,path=path))
            auto=args.pop('lyrics_auto_search',False)
            budget=args.pop('lyrics_network_budget',20)
            with self.lyrics_lock:
                entries,lyric_warnings,_=self.lyrics().for_export(selected_tracks,auto=False,budget=budget,progress=progress,cancel=cancel)
                lyrics_revision=self.lyrics().store.state_fingerprint()
            args['lyrics']=entries
            args['source_warnings']+=lyric_warnings
            if auto:args['source_warnings'].append('Information-file export uses local retained lyrics; run explicit lyric search before exporting.')
            selected_ids={track['id'] for track in selected_tracks}
            args['favorites']=[tid for tid in self.application.query('favorites.list',{})['track_ids'] if tid in selected_ids]
            if args.get('existing')=='update' and Path(args['destination']).exists():
                build=card_library.load_manifest(args['destination'])['build_id']
                with self.connect() as c:
                    receipts=[json.loads(row[0]) for row in c.execute('SELECT body FROM sync_baselines WHERE build=?',(build,))]
                receipts.sort(key=lambda value:value.get('imported_at',0),reverse=True)
                acknowledged={}
                for receipt in receipts:
                    if receipt.get('input_path') and receipt.get('input_sha256'):
                        acknowledged.setdefault(receipt['input_path'],{'path':receipt['input_path'],'sha256':receipt['input_sha256']})
                args['acknowledged_playlists']=list(acknowledged.values())
            for private in ('peak_cache_dir','peak_scanner','ffmpeg_path','cancel'):
                if private in args:raise DomainError('INVALID_ARGUMENT','Internal export option is not public',{'field':private})
            args.setdefault('midpoint_dbfs',0.0)
            args.setdefault('correction_limit_db',4.0)
            args.setdefault('peak_correction_enabled',True)
            args.setdefault('native_only_art',True)
            args['peak_cache_dir']=self.home/'cache'/'sample-peaks-v1'
            args['cancel']=cancel
            if args.get('existing') == 'update':
                target=Path(args['destination'])
                if (target/'Struct/device/state.xms').exists() or (target/'Struct/device/state.pending').exists():
                    args['acknowledged_device_state']=DeviceStateActions(self).export_acknowledgement(target)
            plan = card_library.plan_export(**args)
            record={'card_plan':plan,'application_revision':snapshot['revision'],'lyrics_revision':lyrics_revision}
            plan_id, hashed = str(uuid.uuid4()), digest(record)
            with self.connect() as c:
                c.execute('INSERT INTO plans VALUES(?,?,?)', (plan_id,hashed,json.dumps(record)))
            return {'plan_id':plan_id,'digest':hashed,'plan':plan}
        if name == 'cards.execute':
            record = self.query('plans.get', {'id':args['plan_id']})
            if record['digest'] != args['digest']:
                raise DomainError('PLAN_STALE', 'Plan digest does not match', {})
            frozen=record['plan']
            if self.application.query('tracks.search',{'limit':1})['revision'] != frozen['application_revision']:
                raise DomainError('PLAN_STALE','Library changed after planning',{})
            self._under(frozen['card_plan']['destination'], self.export_roots)
            for root in frozen['card_plan']['roots']:
                self._under(root['source'],self.music_roots)
            for source in frozen['card_plan']['files']+frozen['card_plan']['playlist_covers']+frozen['card_plan'].get('lyric_inputs',[]):
                self._under(source['source'],self.music_roots+self.import_roots+[self.home/'originals',self.home/'lyrics'])
            if frozen.get('lyrics_revision')!=self.lyrics().store.state_fingerprint():
                raise DomainError('PLAN_STALE','Lyrics changed after planning',{})
            acknowledgement=(frozen['card_plan'].get('activation') or {}).get('acknowledged_device_state')
            if acknowledgement is not None:
                current=DeviceStateActions(self).export_acknowledgement(frozen['card_plan']['activation']['active_root'])
                if current != acknowledgement:
                    raise DomainError('STATE_RECONCILIATION_REQUIRED','Native state acknowledgement changed after export planning',{})
            return card_library.execute_export(frozen['card_plan'], progress=progress, cancel=cancel)
        if name == 'cards.recover_update':
            control=self._under(args['control_directory'],self.export_roots)
            return card_library.recover_update(control)
        if name in ('cards.verify','cards.inbox'):
            root = self._under(args['root'], self.export_roots)
            return (card_library.verify_export(root) if name == 'cards.verify' else card_library.inspect_inbox(root))
        if name == 'cards.restore':
            root = self._under(args['root'], self.export_roots)
            destination = self._under(args['destination'], self.export_roots)
            return card_library.restore_export(root, destination)
        if name in ('cards.reconcile','cards.import'):
            root = self._under(args['root'], self.export_roots)
            source=self._under(args['m3u'], [root])
            before=hashlib.sha256(source.read_bytes()).hexdigest()
            proposal=card_library.reconcile_m3u(root,source,playlist_id=args.get('playlist_id'))
            if hashlib.sha256(source.read_bytes()).hexdigest()!=before:
                raise DomainError('SOURCE_CHANGED','Playlist changed during inspection',{})
            pid=proposal['playlist_id']
            current=None
            if pid:
                try:current=self.application.query('playlists.get',{'id':pid})['playlist']
                except DomainError as exc:
                    if exc.code!='NOT_FOUND':raise
            with self.connect() as c:
                stored=c.execute('SELECT body FROM sync_baselines WHERE build=? AND playlist=?',
                                 (proposal['build_id'],pid)).fetchone()
            base=json.loads(stored[0]) if stored else proposal['base_snapshot']
            returned=proposal['proposed_tracks'];now=current['track_ids'] if current else None
            conflict=bool(current and (not base or (now!=base['tracks'] and returned!=now and returned!=base['tracks'])))
            proposal.update(input_sha256=before,current_snapshot={'tracks':now,'revision':current['revision']} if current else None,
                            base_snapshot=base,conflict={'reason':'both_pc_and_device_changed'} if conflict else None)
            proposal['status']='unresolved' if proposal['unresolved'] else 'conflict' if conflict else 'new_playlist' if current is None else 'unchanged' if returned==now else 'pc_only' if base and returned==base['tracks'] else 'changed'
            if name=='cards.reconcile':return proposal
            if args.get('input_sha256')!=before:
                raise DomainError('PLAN_STALE','Inspect this playlist before importing its exact bytes',{})
            if proposal['unresolved']:
                raise DomainError('UNRESOLVED_ENTRIES','Playlist has unresolved entries',proposal)
            if conflict and args.get('decision') not in ('device','copy'):
                raise DomainError('IMPORT_CONFLICT','Both copies changed; explicitly choose a result',proposal)
            if proposal['status'] in ('unchanged','pc_only') and args.get('decision') not in ('device','copy'):
                return {'applied':False,'reason':proposal['status'],'proposal':proposal}
            if current is None or args.get('decision')=='copy':
                name=args.get('new_name') or (proposal['name'] if current is None else None)
                if not name:raise DomainError('INVALID_ARGUMENT','A name for the conflict copy is required',{})
                receipt=self.application.execute('playlists.create',{'name':name,'track_ids':returned},
                                                 expected_revision=revision,idempotency_key=key)
            else:
                receipt=self.application.execute('playlists.replace',{'id':pid,'track_ids':returned},
                                                 expected_revision=revision,idempotency_key=key)
            baseline={'tracks':returned,'revision':receipt['revision'],'input_path':source.relative_to(Path(root)).as_posix(),'input_sha256':before,'imported_at':time.time()}
            with self.connect() as c:
                c.execute('INSERT OR REPLACE INTO sync_baselines VALUES(?,?,?)',
                          (proposal['build_id'],receipt['result']['id'],json.dumps(baseline)))
            return {'applied':True,'receipt':receipt,'proposal':proposal}
        raise DomainError('UNKNOWN_COMMAND', 'Unsupported card operation', {})

    def close(self):
        self.pool.shutdown(wait=True)
        try:
            if self.playback:self.playback.close()
        finally:
            self.application.close()
