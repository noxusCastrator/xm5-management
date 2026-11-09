"""Cross-platform, JSON-only external command entry point."""
import argparse
import json
import sys
import uuid
from pathlib import Path
from .client import Client


class Parser(argparse.ArgumentParser):
    def error(self,message):
        raise ValueError(message)


def main(argv=None):
    try:
        parser=Parser(prog='xm5ctl',description='XM5 local commands; GUI/backend must be running.')
        parser.add_argument('--home',type=Path)
        sub=parser.add_subparsers(dest='verb',required=True)
        for name in ('status','capabilities','schema','stop'):
            sub.add_parser(name)
        for kind in ('query','command'):
            p=sub.add_parser(kind);p.add_argument('name');p.add_argument('--input',type=Path)
            if kind=='command':
                p.add_argument('--expected-revision',type=int)
                p.add_argument('--key',required=True,help='Keep the same key and input when retrying')
        args=parser.parse_args(argv)
        body=json.loads(args.input.read_text(encoding='utf-8-sig')) if getattr(args,'input',None) else {}
        if not isinstance(body,dict):
            raise ValueError('Input must be a JSON object')
        client=Client(args.home)
        if args.verb in ('status','capabilities','schema'):
            result=client.request('GET','/v1/'+args.verb)
        elif args.verb=='stop':
            result=client.request('POST','/v1/shutdown')
        elif args.verb=='query':
            result=client.query(args.name,body)
        else:
            result=client.command(args.name,body,expected_revision=args.expected_revision,idempotency_key=args.key)
        code=0
        if not result.get('ok'):
            error=result.get('error',{}).get('code','FAILED')
            code=3 if error=='UNAUTHORIZED' else 4 if 'CONFLICT' in error or error=='PLAN_STALE' else 5 if 'UNSUPPORTED' in error else 6
    except (ValueError,json.JSONDecodeError) as exc:
        result={'ok':False,'error':{'code':'INVALID_ARGUMENT','message':str(exc)}};code=2
    except (OSError,RuntimeError) as exc:
        # No URLs/headers or token-containing diagnostics from transport internals.
        result={'ok':False,'error':{'code':'SERVICE_UNAVAILABLE','message':type(exc).__name__}};code=3
    except Exception as exc:
        result={'ok':False,'error':{'code':'SERVICE_UNAVAILABLE','message':type(exc).__name__}};code=3
    print(json.dumps(result,ensure_ascii=False,default=str))
    return code


if __name__=='__main__':
    sys.exit(main())
