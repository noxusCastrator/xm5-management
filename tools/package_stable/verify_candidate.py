#!/usr/bin/env python3
"""B2 independent candidate verifier (unreviewed, offline only).  Run as root inside WSL.

Compares a candidate update.bin against the pinned stock V2.43 package and an overlay manifest.
Implemented separately from build_candidate.py: raw 512-byte tar header parsing, raw squashfs superblock,
`readelf` for ELF facts, and its own unsquashfs extraction of BOTH images into /var/tmp/xm5-b2-verify.*
(removed afterwards).  Every rootfs difference (path set, type, mode, owner, mtime, size, content,
symlink target, device number, hardlink grouping) must be exactly an overlay entry changing only
content/size/mtime, and every overlay entry must be used.  Writes only --out (JSON).  Exit 0 = PASS.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile

STOCK_SHA256 = '8b3f05155840c7b821f3bcbcd4690eaf64a00e867c7c3cef68f7be1959368be4'
ROOTFS_MAX = 50954240
MODEL = 'ONIX XM5'
ORDER = ['firmware_v20.info', 'recovery-update/', 'recovery-update/xImage', 'recovery-update/rootfs.squashfs']
VERIFY_RE = re.compile(r'^/var/tmp/xm5-b2-verify\.[A-Za-z0-9_]{8}$')


class Rep:
    def __init__(self):
        self.checks = []

    def add(self, cid, ok, observed=None, expected=None):
        self.checks.append({'id': cid, 'result': 'PASS' if ok else 'FAIL', 'observed': observed, 'expected': expected})
        return ok


def h(data, algo='sha256'):
    return hashlib.new(algo, data).hexdigest()


def hfile(path):
    d = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            d.update(b)
    return d.hexdigest()


def tar_members(path):
    """Raw GNU/ustar header walk; returns list of dicts and the raw bytes of each payload."""
    data = open(path, 'rb').read()
    out, pos = [], 0
    while pos + 512 <= len(data):
        hd = data[pos:pos + 512]
        if hd == b'\0' * 512:
            break
        name = hd[0:100].rstrip(b'\0').decode('ascii', 'replace')
        num = lambda a, b: int(hd[a:b].rstrip(b'\0 ').decode() or '0', 8)
        size = num(124, 136)
        chk = sum(hd[:148]) + 32 * 8 + sum(hd[156:])
        out.append({'name': name, 'mode': '%04o' % num(100, 108), 'uid': num(108, 116), 'gid': num(116, 124),
                    'size': size, 'mtime': num(136, 148), 'type': hd[156:157].decode('ascii', 'replace'),
                    'magic': hd[257:265].hex(), 'uname': hd[265:297].rstrip(b'\0').decode(),
                    'gname': hd[297:329].rstrip(b'\0').decode(), 'linkname': hd[157:257].rstrip(b'\0').decode(),
                    'checksum_ok': chk == num(148, 156), 'offset': pos + 512})
        pos += 512 + (size + 511) // 512 * 512
    rest = data[pos:]
    return data, out, {'size': len(data), 'record_multiple': len(data) % 10240 == 0, 'tail_zero': rest == b'\0' * len(rest) and len(rest) >= 1024}


def payload(data, m):
    return data[m['offset']:m['offset'] + m['size']]


def superblock(b):
    v = struct.unpack('<IIIIIHHHHHHQ', b[:40])
    return {'magic': hex(v[0]), 'inodes': v[1], 'mkfs_time': v[2], 'block_size': v[3], 'compressor': v[5],
            'flags': hex(v[7]), 'ids': v[8], 'version': '%d.%d' % (v[9], v[10]), 'bytes_used': struct.unpack('<Q', b[40:48])[0]}


def lzo_level9_options(blob):
    """Only the exact uncompressed 8-byte options emitted by our encoder.

    Squashfs-tools 4.6.1 lzo_wrapper.h defines LZO1X_999 as algorithm 4;
    lzo_comp_opts contains little-endian algorithm and compression_level.
    Its options metadata immediately follows the 96-byte superblock.
    """
    if len(blob) < 106 or blob[96:98] != b'\x08\x80':
        return None
    algorithm, level = struct.unpack_from('<II', blob, 98)
    if (algorithm, level) != (4, 9):
        return None
    return {'metadata_bytes': 8, 'uncompressed': True,
            'algorithm': algorithm, 'compression_level': level}


def allowed_superblock(stock, candidate, blob):
    """Keep every original guard; add one explicit LZO encoder profile."""
    if stock['flags'] != '0xc0':
        return False
    keys = ('magic', 'mkfs_time', 'block_size', 'compressor', 'ids', 'version')
    if not all(stock[k] == candidate[k] for k in keys):
        return False
    if stock['flags'] == candidate['flags']:
        return True                 # Original byte-equivalent flag policy.
    return (stock['flags'] == '0xc0' and candidate['flags'] == '0x4c0' and
            candidate['magic'] == '0x73717368' and
            candidate['version'] == '4.0' and
            candidate['block_size'] == 131072 and candidate['compressor'] == 3 and
            lzo_level9_options(blob) is not None)


def readelf(path):
    p = subprocess.run(['readelf', '-h', '-l', '-W', path], capture_output=True, text=True, env=dict(os.environ, LC_ALL='C'))
    if p.returncode != 0:
        return None
    t = p.stdout
    g = lambda pat: (re.search(pat, t, re.M).group(1).strip() if re.search(pat, t, re.M) else None)
    loads = [(int(m.group(1), 16), int(m.group(2), 16)) for m in
             re.finditer(r'^\s+LOAD\s+0x[0-9a-f]+\s+(0x[0-9a-f]+)\s+0x[0-9a-f]+\s+0x[0-9a-f]+\s+(0x[0-9a-f]+)\s+(R?\s?W?\s?E)', t, re.M)
             if 'E' in m.group(3)]
    entry = int(g(r'Entry point address:\s+(\S+)'), 16)
    return {'class': g(r'Class:\s+(.*)$'), 'data': g(r'Data:\s+(.*)$'), 'type': (g(r'Type:\s+(\S+)') or ''),
            'machine': g(r'Machine:\s+(.*)$'), 'flags': g(r'Flags:\s+(.*)$'),
            'interp': g(r'Requesting program interpreter:\s*([^\]]+)\]'), 'entry': hex(entry),
            'entry_in_exec_load': any(lo <= entry < lo + sz for lo, sz in loads)}


def walk(root):
    rows = {}
    for dp, dns, fns in os.walk(root):
        dns.sort()
        for name in [None] + sorted(dns) + sorted(fns):
            p = dp if name is None else os.path.join(dp, name)
            if name is None and dp != root:
                continue
            rel = os.path.relpath(p, root)
            st = os.lstat(p)
            r = {'fmt': stat.S_IFMT(st.st_mode), 'perm': stat.S_IMODE(st.st_mode), 'owner': (st.st_uid, st.st_gid),
                 'mtime': int(st.st_mtime), 'ino': st.st_ino}
            if stat.S_ISREG(st.st_mode):
                r['size'], r['sha256'] = st.st_size, hfile(p)
            elif stat.S_ISLNK(st.st_mode):
                r['link'] = os.readlink(p)
            elif stat.S_ISCHR(st.st_mode) or stat.S_ISBLK(st.st_mode):
                r['rdev'] = st.st_rdev
            rows[rel] = r
    groups = {}
    for rel, r in rows.items():
        if r['fmt'] == stat.S_IFREG:
            groups.setdefault(r['ino'], set()).add(rel)
    return rows, sorted(sorted(g) for g in groups.values() if len(g) > 1)


def load_manifest(path):
    ents = {}
    for line in open(path, encoding='utf-8').read().split('\n'):
        f = line.split('\t')
        if f[0] == 'file':
            ents[f[1]] = {'old': f[2], 'new': f[3], 'perm': int(f[4], 8), 'owner': tuple(int(x) for x in f[5].split(':'))}
    return ents


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stock', required=True)
    ap.add_argument('--candidate', required=True)
    ap.add_argument('--overlay', required=True)
    ap.add_argument('--expect-tag', required=True)
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    rep, info = Rep(), {'candidate': os.path.realpath(a.candidate), 'overlay': os.path.realpath(a.overlay), 'expect_tag': a.expect_tag}
    tmp = None
    try:
        sdata, smem, _ = tar_members(a.stock)
        rep.add('stock.sha256_pinned', h(sdata) == STOCK_SHA256, h(sdata), STOCK_SHA256)
        cdata, cmem, ctail = tar_members(a.candidate)
        info['candidate_sha256'] = h(cdata)
        rep.add('outer.member_names_exact_order', [m['name'] for m in cmem] == ORDER, [m['name'] for m in cmem], ORDER)
        rep.add('outer.gnu_records_and_end', ctail['record_multiple'] and ctail['tail_zero'], ctail)
        if [m['name'] for m in cmem] != ORDER:
            raise StopIteration
        for s, c in zip(smem, cmem):
            keys = ('mode', 'uid', 'gid', 'uname', 'gname', 'mtime', 'type', 'magic', 'linkname')
            rep.add('outer.header_meta:' + c['name'], all(s[k] == c[k] for k in keys) and c['checksum_ok'],
                    {k: c[k] for k in keys}, {k: s[k] for k in keys})
        sp = {m['name']: payload(sdata, m) for m in smem}
        cp = {m['name']: payload(cdata, m) for m in cmem}
        kern, rfs, inf = cp['recovery-update/xImage'], cp['recovery-update/rootfs.squashfs'], cp['firmware_v20.info']
        rep.add('kernel.byte_identical', kern == sp['recovery-update/xImage'], h(kern), h(sp['recovery-update/xImage']))
        lines = inf.split(b'\n')
        rep.add('info.ascii_lf_4_lines', b'\r' not in inf and inf.endswith(b'\n') and len(lines) == 5 and all(b < 128 for b in inf),
                inf.decode('ascii', 'replace'))
        rep.add('info.model', lines[0] == MODEL.encode(), lines[0].decode('ascii', 'replace'))
        tag = lines[1].decode('ascii', 'replace') if len(lines) > 1 else None
        rep.add('info.tag', tag == a.expect_tag, tag, a.expect_tag)
        rep.add('info.tag_rule', tag == 'V2.43' or bool(re.match(r'^V2\.43-[A-Za-z0-9._-]{1,25}$', tag or '')), tag)
        want3 = ('%s  recovery-update/xImage' % h(kern, 'md5')).encode()
        want4 = ('%s  recovery-update/rootfs.squashfs' % h(rfs, 'md5')).encode()
        rep.add('info.md5_lines', lines[2:4] == [want3, want4], [l.decode('ascii', 'replace') for l in lines[2:4]])
        rep.add('rootfs.size_bound', len(rfs) <= ROOTFS_MAX, len(rfs), '<= %d' % ROOTFS_MAX)
        info['rootfs_size'], info['rootfs_headroom'] = len(rfs), ROOTFS_MAX - len(rfs)
        ssb, csb = superblock(sp['recovery-update/rootfs.squashfs']), superblock(rfs)
        rep.add('rootfs.superblock_params', allowed_superblock(ssb, csb, rfs),
                csb, ssb)
        if ssb['flags'] != csb['flags']:
            rep.add('rootfs.exact_lzo1x999_level9_options', lzo_level9_options(rfs) is not None,
                    lzo_level9_options(rfs), {'algorithm': 4, 'compression_level': 9,
                                             'metadata_bytes': 8, 'uncompressed': True})

        tmp = tempfile.mkdtemp(prefix='xm5-b2-verify.', dir='/var/tmp')
        assert VERIFY_RE.match(tmp) and os.path.realpath(tmp) == tmp
        trees = {}
        for label, blob in (('stock', sp['recovery-update/rootfs.squashfs']), ('cand', rfs)):
            img = os.path.join(tmp, label + '.squashfs')
            open(img, 'wb').write(blob)
            p = subprocess.run(['unsquashfs', '-no-progress', '-d', os.path.join(tmp, label), img], capture_output=True, text=True)
            if p.returncode != 0:
                rep.add('rootfs.unsquashfs:' + label, False, p.stderr[-300:])
                raise StopIteration
            trees[label] = walk(os.path.join(tmp, label))
        (srows, sgroups), (crows, cgroups) = trees['stock'], trees['cand']
        info['stock_names'], info['candidate_names'] = len(srows), len(crows)
        ents = load_manifest(a.overlay)
        info['overlay_paths'] = sorted(ents)
        diffs = {}
        for rel in sorted(set(srows) | set(crows)):
            if rel not in srows or rel not in crows:
                diffs[rel] = ['added' if rel in crows else 'removed']
                continue
            f = [k for k in sorted(set(srows[rel]) | set(crows[rel])) if k != 'ino' and srows[rel].get(k) != crows[rel].get(k)]
            if f:
                diffs[rel] = f
        info['rootfs_diff'] = diffs
        unexpected = {k: v for k, v in diffs.items() if k not in ents or not set(v) <= {'sha256', 'size', 'mtime'}}
        rep.add('rootfs.no_unlisted_or_metadata_changes', not unexpected, unexpected, 'only overlay paths; only sha256/size/mtime')
        rep.add('rootfs.every_overlay_entry_used', set(ents) <= set(diffs), sorted(set(ents) - set(diffs)))
        rep.add('rootfs.hardlink_groups_unchanged', sgroups == cgroups, len(cgroups), len(sgroups))
        for rel, e in sorted(ents.items()):
            s, c = srows.get(rel), crows.get(rel)
            ok = bool(s and c) and s['fmt'] == stat.S_IFREG and s['sha256'] == e['old'] and c['sha256'] == e['new'] \
                and c['perm'] == s['perm'] == e['perm'] and c['owner'] == s['owner'] == e['owner']
            rep.add('overlay.hash_mode_owner:' + rel, ok, c and {'sha256': c.get('sha256'), 'perm': '%04o' % c['perm'], 'owner': c['owner']},
                    {'old': e['old'], 'new': e['new'], 'perm': '%04o' % e['perm'], 'owner': e['owner']})
            if s and c:
                if rel == 'etc/init.d/S90play':   # RX2: script, not ELF
                    body = open(os.path.join(tmp, 'cand', rel), 'rb').read()
                    rep.add('overlay.script_text:' + rel, body.startswith(b'#!/bin/sh\n') and b'\r' not in body and b'\x00' not in body, len(body), 'LF /bin/sh text')
                elif rel == 'release/assets/default/raw/ui/playset/equalizer_page.bin':
                    from resource_guard import guard
                    try:
                        result = guard(open(os.path.join(tmp,'stock',rel),'rb').read(),open(os.path.join(tmp,'cand',rel),'rb').read())
                        rep.add('overlay.stock_ui_preserved', True, result)
                    except Exception as ex:
                        rep.add('overlay.stock_ui_preserved', False, str(ex))
                else:
                    es, ec = readelf(os.path.join(tmp, 'stock', rel)), readelf(os.path.join(tmp, 'cand', rel))
                    same = es and ec and all(es[k] == ec[k] for k in ('class', 'data', 'type', 'machine', 'flags', 'interp'))
                    rep.add('overlay.elf_consistent:' + rel, bool(same and ec['entry_in_exec_load']), ec, es)
                parts = rel.split('/')
                parents = ['.'] + ['/'.join(parts[:i]) for i in range(1, len(parts))]
                rep.add('overlay.parent_mtimes:' + rel, all(srows[q]['mtime'] == crows[q]['mtime'] for q in parents),
                        {q: crows[q]['mtime'] for q in parents})
    except StopIteration:
        pass
    except Exception as x:
        rep.add('verifier.internal', False, repr(x))
    finally:
        if tmp and VERIFY_RE.match(tmp) and os.path.realpath(tmp) == tmp:
            shutil.rmtree(tmp)
    fails = [c['id'] for c in rep.checks if c['result'] == 'FAIL']
    result = {'tool': 'B2 verify_candidate.py', 'verdict': 'FAIL' if fails else 'PASS', 'failed': fails,
              'hardware_verified': False, 'info': info, 'checks': rep.checks}
    with open(a.out, 'w', newline='\n') as f:
        json.dump(result, f, indent=1, default=str)
        f.write('\n')
    print('%s %s failed=%s' % (result['verdict'], a.candidate, fails))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
