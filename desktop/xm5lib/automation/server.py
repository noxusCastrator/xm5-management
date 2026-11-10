"""Authenticated loopback transport. Run explicitly; importing never starts it."""
import argparse
import json
import os
import secrets
import socket
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field

from .engine import Engine, DomainError
from .runtime import ProfileLock, default_home, write_discovery, remove_discovery, runtime_path
from .http_guard import LocalGuard

READS = ['tracks.search','tracks.get','playlists.list','playlists.get','favorites.list','presets.list',
         'capabilities','jobs.get','plans.get','playback.status','playback.outputs','receipts.get','settings.get','peace.inspect','device_state.inspect','lyrics.get','lyrics.list','scripts.capture','scripts.format','scripts.convert','storage.health','storage.transactions','storage.plan']
WRITES = ['library.scan','library.refresh','library.relink_root','library.import_legacy','playlists.create','playlists.rename','playlists.replace',
          'playlists.delete','playlists.cover','favorites.set','tracks.annotate','presets.save','presets.import','presets.delete','undo','device_state.plan','device_state.apply',
          'cards.recover_update','cards.plan','cards.execute','cards.verify','cards.restore','cards.inbox','cards.reconcile','cards.import','jobs.cancel',
          'playback.connect','playback.command','playback.disconnect','lyrics.inspect','lyrics.search','lyrics.attach','lyrics.import','lyrics.remove','lyrics.fetch_missing','scripts.plan','scripts.execute','storage.sync.plan','storage.repair.plan','storage.sync.apply','storage.recover']
WRITES += ["storage.inline.export", "storage.inline.organize", "storage.inline.rebuild"]
WRITES += ["favorites.replace"]


class Query(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    name: str = Field(max_length=80)
    arguments: dict = Field(default_factory=dict)


class Command(Query):
    expected_revision: int | None = Field(default=None, ge=0)
    idempotency_key: str = Field(min_length=1, max_length=200)


def failure(code, message, status=400, details=None):
    return JSONResponse({'ok':False,'api_version':'1','error':{'code':code,'message':message,
                                                            'details':details or {}}},status_code=status)


def create_app(engine, token, authority, instance_id, shutdown=None):
    app = FastAPI(title='XM5 Local Commands', version='1.0', docs_url=None, redoc_url=None, openapi_url=None)

    app.add_middleware(LocalGuard, token=token, authority=authority, failure=failure)

    @app.exception_handler(DomainError)
    async def domain_error(_, exc):
        status = 409 if 'CONFLICT' in exc.code or exc.code == 'PLAN_STALE' else 400
        return failure(exc.code,str(exc),status,getattr(exc,'details',{}))

    @app.exception_handler(RequestValidationError)
    async def validation_error(_, exc):
        return failure('INVALID_ARGUMENT','Request does not match the command schema',422)

    @app.exception_handler(Exception)
    async def unexpected(_, exc):
        return failure('INTERNAL_ERROR', 'Operation failed; inspect backend diagnostics',500)

    def ok(result):
        return {'ok':True,'api_version':'1','instance_id':instance_id,'result':result}

    @app.get('/v1/status')
    def status():
        return ok({'instance_id':instance_id,'state':'ready'})

    @app.get('/v1/capabilities')
    def capabilities():
        return ok(engine.query('capabilities',{}))

    @app.get('/v1/schema')
    def schema():
        return ok({'queries':READS,'commands':WRITES,'query':Query.model_json_schema(),
                   'command':Command.model_json_schema(),'openapi':app.openapi()})

    @app.post('/v1/query')
    def query(body: Query):
        if body.name not in READS:
            raise DomainError('UNKNOWN_QUERY','Query is not exposed',{})
        return ok(engine.query(body.name,body.arguments))

    @app.post('/v1/commands')
    def command(body: Command):
        if body.name not in WRITES:
            raise DomainError('UNKNOWN_COMMAND','Command is not exposed',{})
        return ok(engine.command(body.name,body.arguments,body.expected_revision,body.idempotency_key))

    @app.post('/v1/shutdown')
    def stop():
        if shutdown:
            shutdown()
        return ok({'stopping':True})

    return app


def serve(home, **options):
    import uvicorn
    home = home.resolve()
    runtime = runtime_path(home)
    lock = ProfileLock(runtime)
    instance = str(uuid.uuid4()); token = secrets.token_urlsafe(32)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    engine = None
    try:
        sock.bind(('127.0.0.1',0)); sock.listen(128)
        port = sock.getsockname()[1]
        engine = Engine(home, **options)
        holder = {}
        app = create_app(engine,token,f'127.0.0.1:{port}',instance,
                         shutdown=lambda: setattr(holder['server'],'should_exit',True))
        @asynccontextmanager
        async def lifespan(_):
            write_discovery(runtime,{'api_version':'1','instance_id':instance,'pid':os.getpid(),
                                     'url':f'http://127.0.0.1:{port}','token':token})
            try:
                yield
            finally:
                remove_discovery(runtime,instance)
        app.router.lifespan_context = lifespan
        holder['server'] = uvicorn.Server(uvicorn.Config(app,access_log=False,proxy_headers=False,
                                                        log_level='warning',workers=1))
        holder['server'].run(sockets=[sock])
    finally:
        remove_discovery(runtime,instance)
        try:
            if engine:
                engine.close()
        finally:
            sock.close(); lock.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--home',type=__import__('pathlib').Path,default=default_home())
    for flag in ('music-root','export-root','import-root'):
        parser.add_argument('--'+flag,action='append',default=[])
    parser.add_argument('--mpv')
    parser.add_argument('--allow-null-playback',action='store_true')
    args=parser.parse_args()
    serve(args.home,music_roots=args.music_root,export_roots=args.export_root,import_roots=args.import_root,
          mpv_path=args.mpv,allow_null_playback=args.allow_null_playback)


if __name__ == '__main__':
    main()
