"""Native client; session tokens never appear in command lines or public results."""
import json
import os
import stat
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from .runtime import default_home,check_private,runtime_path


class Client:
    def __init__(self, home=None):
        self.home = Path(home or default_home())
        target = runtime_path(self.home) / 'connection.json'
        check_private(target.parent);check_private(target)
        try:
            record=json.loads(target.read_text(encoding='utf-8'))
            if not isinstance(record,dict) or any(not isinstance(record.get(k),str) or not record[k]
                      for k in ('url','token','instance_id','api_version')):
                raise ValueError('Malformed discovery')
            if len(record['token'])>256 or len(record['instance_id'])>128:
                raise ValueError('Oversized discovery')
        except (ValueError,TypeError) as exc:
            raise RuntimeError('Invalid service discovery') from exc
        url=urlsplit(record['url'])
        if url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port or url.path or url.username or url.query or url.fragment:
            raise ValueError('Discovery must name a literal loopback endpoint')
        if record.get('api_version') != '1':
            raise ValueError('Unsupported API version')
        self.url, self.token, self.instance = record['url'],record['token'],record['instance_id']
        # Disable environment proxies and redirects; do not forward token elsewhere.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):
                return None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        result=self.request('GET','/v1/status')
        if result.get('instance_id') != self.instance:
            raise ValueError('Stale backend instance')

    def request(self, method, route, body=None):
        if not route.startswith('/v1/') or '://' in route:
            raise ValueError('Invalid local route')
        data=json.dumps(body or {}).encode() if method=='POST' else None
        request=urllib.request.Request(self.url+route,data=data,method=method,
                                       headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
        try:
            response=self.opener.open(request,timeout=10)
        except urllib.error.HTTPError as exc:
            response=exc
        with response:
            try:
                result=json.load(response)
            except (ValueError,TypeError) as exc:
                raise RuntimeError('Invalid backend response') from exc
            if not isinstance(result,dict) or not isinstance(result.get('ok'),bool):
                raise RuntimeError('Invalid backend envelope')
            return result

    def query(self,name,arguments=None):
        return self.request('POST','/v1/query',{'name':name,'arguments':arguments or {}})

    def command(self,name,arguments=None,*,expected_revision=None,idempotency_key):
        return self.request('POST','/v1/commands',{'name':name,'arguments':arguments or {},
                            'expected_revision':expected_revision,'idempotency_key':idempotency_key})
