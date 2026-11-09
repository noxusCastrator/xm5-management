#!/usr/bin/env python3
"""B2 guarded candidate packager for ONIX XM5 V2.43 rootfs-only update.bin (unreviewed, offline only).

Run as root inside WSL Ubuntu.  Reads the pinned stock update.bin and an explicit overlay manifest,
stages on WSL ext4 (/var/tmp/xm5-b2-stage.*), and writes update.bin ONLY into a fresh output
directory below <task>/out/.  Never touches devices, the original rootfs, roundtrip-v2.43 or releases.

Overlay manifest (UTF-8, LF, no BOM, TAB separated):
    xm5-overlay-manifest<TAB>1
    model<TAB>ONIX XM5
    base_version<TAB>V2.43
    base_sha256<TAB><stock update.bin sha256>
    file<TAB><rootfs path><TAB><old sha256><TAB><new sha256><TAB><mode 0NNN><TAB><uid>:<gid><TAB><source>
`source` is relative to the manifest directory and must live in its overlay/ subdirectory.
Lines starting with '#' are comments.  An empty overlay has no `file` lines.

Exit 0 only on success.  Any refusal writes REFUSED.json (when the output dir could be created) and exits 1;
usage/output-path errors exit 2.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile

TASK_ROOT = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
OUT_ROOT = os.path.join(TASK_ROOT, 'out')

STOCK_SHA256 = '8b3f05155840c7b821f3bcbcd4690eaf64a00e867c7c3cef68f7be1959368be4'
STOCK_SIZE = 56371200
XIMAGE_SHA256 = '088be0f3844d5cec4d0ce699c3a784912839e36e039cc0cffff07468f9415eaf'
INFO_SHA256 = '7f347da148c743c185a41f1d350dc08645ed71bcfa3678c4f57e0babffa303f6'
ROOTFS_MD5 = 'ed725b650cd7716b135206db14a22ec8'
ROOTFS_MAX = 50954240
MKFS_TIME = 1769566313
SOURCE_DATE_EPOCH = MKFS_TIME          # mtime given to overlaid files
MODEL = 'ONIX XM5'
BASE_VERSION = 'V2.43'
TAG_RE = re.compile(r'^V2\.43-[A-Za-z0-9._-]{1,25}$')
DEFAULT_TAG = 'V2.43-PEQ01'
# Explicit per-path policy; not a pattern.  Changing this list needs lead review.
ALLOWED_OVERLAY_PATHS = ('release/bin/demo', 'usr/bin/hciplayer', 'etc/init.d/S90play', 'release/assets/default/raw/ui/playset/equalizer_page.bin')
# RX2: S90play carries the TF rescue hook; it is a shell script, so the ELF guard is replaced by a script guard
ELF_PATHS = ('release/bin/demo', 'usr/bin/hciplayer')
SCRIPT_PATHS = ('etc/init.d/S90play',)
MEMBERS = [('firmware_v20.info', tarfile.REGTYPE, 0o664),
           ('recovery-update/', tarfile.DIRTYPE, 0o775),
           ('recovery-update/xImage', tarfile.REGTYPE, 0o777),
           ('recovery-update/rootfs.squashfs', tarfile.REGTYPE, 0o777)]
OWNER_ID, OWNER_NAME = 1004, 'chenyou'
PATH_RE = re.compile(r'^[A-Za-z0-9_+-][A-Za-z0-9._+-]*(/[A-Za-z0-9_+-][A-Za-z0-9._+-]*)*$')
HEX64 = re.compile(r'^[0-9a-f]{64}$')
STAGE_RE = re.compile(r'^/var/tmp/xm5-b2-stage\.[A-Za-z0-9_]{8}$')
MKSQUASHFS_ARGS = ['-comp', 'lzo', '-Xalgorithm', 'lzo1x_999', '-Xcompression-level', '9', '-b', '131072', '-noappend', '-reproducible',
                   '-mkfs-time', str(MKFS_TIME), '-processors', '1', '-no-progress']


class Refuse(Exception):
    def __init__(self, errors):
        super().__init__('; '.join(errors))
        self.errors = errors


def log(msg):
    print('[build] ' + msg, flush=True)


def sha256_file(path, algo='sha256'):
    h = hashlib.new(algo)
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def tool_versions():
    out = {}
    for cmd in (['unsquashfs', '-version'], ['mksquashfs', '-version'], ['tar', '--version']):
        p = subprocess.run(cmd, capture_output=True, text=True)
        out[cmd[0]] = (p.stdout or p.stderr).splitlines()[0]
    out['python3'] = sys.version.split()[0]
    p = subprocess.run(['dpkg-query', '-W', 'squashfs-tools', 'tar', 'python3', 'coreutils'], capture_output=True, text=True)
    out['dpkg'] = p.stdout.strip().splitlines()
    out['build_candidate.py_sha256'] = sha256_file(os.path.abspath(__file__))
    return out


# ---------------------------------------------------------------- arguments and manifest

def check_out_dir(out, roundtrip_test):
    out = os.path.abspath(out)
    parent = os.path.realpath(os.path.dirname(out))
    base = os.path.basename(out)
    if not (parent == OUT_ROOT or parent.startswith(OUT_ROOT + os.sep)):
        return None, 'E_OUT_PATH: output parent %s is not below %s' % (parent, OUT_ROOT)
    if not re.match(r'^[A-Za-z0-9._-]{1,80}$', base) or base in ('.', '..'):
        return None, 'E_OUT_PATH: unsafe output name %r' % base
    if roundtrip_test != base.startswith('roundtrip-test-'):
        return None, 'E_OUT_PATH: --roundtrip-test output names must start with roundtrip-test- (and only those)'
    if os.path.lexists(out):
        return None, 'E_OUT_EXISTS: %s already exists; output directory must be fresh' % out
    return os.path.join(parent, base), None


def check_tag(tag, roundtrip_test):
    if tag == BASE_VERSION:
        return None if roundtrip_test else 'E_TAG: %s is the stock tag (installed V2.43 rejects it); only --roundtrip-test may use it' % tag
    if not TAG_RE.match(tag):
        return 'E_TAG: %r does not match %s' % (tag, TAG_RE.pattern)
    return None


def parse_manifest(path):
    errors, entries = [], []
    raw = open(path, 'rb').read()
    if raw.startswith(b'\xef\xbb\xbf') or b'\r' in raw or not raw.endswith(b'\n'):
        raise Refuse(['E_MANIFEST_ENCODING: BOM, CR or missing final LF'])
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise Refuse(['E_MANIFEST_ENCODING: not UTF-8'])
    lines = [l for l in text.split('\n')[:-1] if l and not l.startswith('#')]
    header = [l.split('\t') for l in lines[:4]]
    want = [['xm5-overlay-manifest', '1'], ['model', MODEL], ['base_version', BASE_VERSION], ['base_sha256', STOCK_SHA256]]
    if header != want:
        raise Refuse(['E_MANIFEST_HEADER: expected %r, got %r' % (want, header)])
    mdir = os.path.realpath(os.path.dirname(path))
    overlay_dir = os.path.join(mdir, 'overlay')
    seen, used_sources = set(), set()
    for n, line in enumerate(lines[4:], 5):
        f = line.split('\t')
        if f[0] != 'file' or len(f) != 7:
            errors.append('E_MANIFEST_RECORD: line %d: %r' % (n, line))
            continue
        _, rel, old, new, mode, owner, src = f
        if not PATH_RE.match(rel) or any(c in ('.', '..') for c in rel.split('/')):
            errors.append('E_PATH_SYNTAX: %r (absolute, traversal or unsafe characters)' % rel)
            continue
        if rel in seen:
            errors.append('E_DUPLICATE: %s' % rel)
        seen.add(rel)
        if rel not in ALLOWED_OVERLAY_PATHS:
            errors.append('E_PATH_NOT_ALLOWED: %s not in %s' % (rel, ALLOWED_OVERLAY_PATHS))
        if not (HEX64.match(old) and HEX64.match(new)):
            errors.append('E_MANIFEST_RECORD: %s: sha256 fields must be 64 lowercase hex' % rel)
            continue
        if old == new:
            errors.append('E_NOOP: %s old and new sha256 are equal' % rel)
        if not re.match(r'^0[0-7]{3,4}$', mode) or not re.match(r'^\d+:\d+$', owner):
            errors.append('E_MANIFEST_RECORD: %s: mode/owner format' % rel)
            continue
        if not PATH_RE.match(src) or any(c in ('.', '..') for c in src.split('/')):
            errors.append('E_SOURCE_PATH: %r' % src)
            continue
        spath = os.path.join(mdir, src)
        if os.path.realpath(spath) != spath or not spath.startswith(overlay_dir + os.sep):
            errors.append('E_SOURCE_PATH: %s must be a non-symlink path inside %s' % (src, overlay_dir))
            continue
        st = os.lstat(spath) if os.path.lexists(spath) else None
        if st is None or not stat.S_ISREG(st.st_mode):
            errors.append('E_SOURCE_PATH: %s missing or not a regular file' % src)
            continue
        actual = sha256_file(spath)
        if actual != new:
            errors.append('E_NEW_HASH: %s source sha256 %s != manifest new %s' % (rel, actual, new))
        used_sources.add(spath)
        entries.append({'path': rel, 'old_sha256': old, 'new_sha256': new, 'mode': int(mode, 8),
                        'uid': int(owner.split(':')[0]), 'gid': int(owner.split(':')[1]), 'source': spath,
                        'new_size': st.st_size})
    if os.path.isdir(overlay_dir):
        for dp, dns, fns in os.walk(overlay_dir):
            for name in fns + [d for d in dns if os.path.islink(os.path.join(dp, d))]:
                p = os.path.join(dp, name)
                if p not in used_sources:
                    errors.append('E_EXTRA_FILE: %s is in overlay/ but not listed' % p)
    return entries, errors, sha256_file(path)


# ---------------------------------------------------------------- ELF

def elf_info(path):
    with open(path, 'rb') as f:
        h = f.read(64)
        if len(h) < 52 or h[:4] != b'\x7fELF' or h[4] not in (1, 2) or h[5] not in (1, 2):
            return None
        e = '<' if h[5] == 1 else '>'
        if h[4] == 1:
            e_type, mach, _, entry, phoff, _, flags, _, phentsize, phnum = struct.unpack(e + 'HHIIIIIHHH', h[16:46])
        else:
            e_type, mach, _, entry, phoff, _, flags, _, phentsize, phnum = struct.unpack(e + 'HHIQQQIHHH', h[16:58])
        interp, exec_loads = None, []
        for i in range(phnum):
            f.seek(phoff + i * phentsize)
            ph = f.read(phentsize)
            if h[4] == 1:
                p_type, off, vaddr, _, filesz, memsz, pflags, _ = struct.unpack(e + 'IIIIIIII', ph[:32])
            else:
                p_type, pflags, off, vaddr, _, filesz, memsz, _ = struct.unpack(e + 'IIQQQQQQ', ph[:56])
            if p_type == 3:
                f.seek(off)
                interp = f.read(filesz).rstrip(b'\0').decode('ascii', 'replace')
            if p_type == 1 and pflags & 1:
                exec_loads.append((vaddr, vaddr + memsz))
    return {'class': h[4], 'data': h[5], 'type': e_type, 'machine': mach, 'flags': '0x%08x' % flags,
            'entry': '0x%x' % entry, 'interp': interp,
            'entry_in_exec_load': any(lo <= entry < hi for lo, hi in exec_loads)}


def elf_errors(rel, old, new):
    if old is None:
        return ['E_ELF: %s stock file is not ELF' % rel]
    if new is None:
        return ['E_ELF: %s new file is not ELF' % rel]
    errs = ['E_ELF: %s %s %r != stock %r' % (rel, k, new[k], old[k])
            for k in ('class', 'data', 'type', 'machine', 'flags', 'interp') if new[k] != old[k]]
    if not new['entry_in_exec_load']:
        errs.append('E_ELF: %s entry %s outside executable PT_LOAD' % (rel, new['entry']))
    return errs


# ---------------------------------------------------------------- tree snapshot

def snapshot(root):
    rows, cache = {}, {}

    def visit(path, rel):
        st = os.lstat(path)
        row = {'kind': stat.filemode(st.st_mode)[0], 'mode': '%04o' % stat.S_IMODE(st.st_mode), 'uid': st.st_uid,
               'gid': st.st_gid, 'mtime': int(st.st_mtime), 'ino': st.st_ino, 'nlink': st.st_nlink}
        if stat.S_ISREG(st.st_mode):
            row['size'] = st.st_size
            if st.st_ino not in cache:
                cache[st.st_ino] = sha256_file(path)
            row['sha256'] = cache[st.st_ino]
        elif stat.S_ISLNK(st.st_mode):
            row['target'] = os.readlink(path)
        elif stat.S_ISCHR(st.st_mode) or stat.S_ISBLK(st.st_mode):
            row['rdev'] = st.st_rdev
        rows[rel] = row
        if stat.S_ISDIR(st.st_mode):
            for name in sorted(os.listdir(path)):
                visit(os.path.join(path, name), name if rel == '.' else rel + '/' + name)

    visit(root, '.')
    return rows


def link_groups(rows):
    g = {}
    for rel, r in rows.items():
        if r['kind'] == '-':
            g.setdefault(r['ino'], []).append(rel)
    return sorted(tuple(sorted(v)) for v in g.values() if len(v) > 1)


def diff_trees(a, b):
    diffs = {}
    for rel in sorted(set(a) | set(b)):
        if rel not in a or rel not in b:
            diffs[rel] = ['added' if rel in b else 'removed']
            continue
        fields = [k for k in sorted(set(a[rel]) | set(b[rel])) if k != 'ino' and a[rel].get(k) != b[rel].get(k)]
        if fields:
            diffs[rel] = fields
    return diffs


# ---------------------------------------------------------------- stage helpers

def make_stage():
    stage = tempfile.mkdtemp(prefix='xm5-b2-stage.', dir='/var/tmp')
    if not STAGE_RE.match(stage) or os.path.realpath(stage) != stage:
        raise Refuse(['E_STAGE: unexpected stage path %s' % stage])
    fstype = subprocess.run(['stat', '-f', '-c', '%T', stage], capture_output=True, text=True).stdout.strip()
    if fstype not in ('ext2/ext3', 'ext4'):
        raise Refuse(['E_STAGE: %s is on %s, need WSL ext4' % (stage, fstype)])
    return stage


def remove_stage(stage):
    if stage and STAGE_RE.match(stage) and os.path.realpath(stage) == stage and os.path.isdir(stage):
        shutil.rmtree(stage)
        log('removed own stage %s' % stage)
    elif stage:
        log('REFUSING to remove unexpected stage path %r' % stage)


def run(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if p.returncode != 0:
        raise Refuse(['E_TOOL: %s exit %d: %s' % (cmd[0], p.returncode, (p.stderr or p.stdout)[-500:])])
    return p.stdout


def check_outer(path):
    errs, mtimes = [], {}
    with tarfile.open(path, 'r:') as t:
        members = t.getmembers()
    if [m.name for m in members] != [m[0].rstrip('/') for m in MEMBERS]:
        return ['E_OUTER_MEMBERS: %r' % [m.name for m in members]], mtimes
    for m, (name, typ, mode) in zip(members, MEMBERS):
        if m.type != typ or m.mode != mode or m.uid != OWNER_ID or m.gid != OWNER_ID or \
                m.uname != OWNER_NAME or m.gname != OWNER_NAME:
            errs.append('E_OUTER_MEMBER_META: %s type=%r mode=%o %d/%d %s/%s' % (
                name, m.type, m.mode, m.uid, m.gid, m.uname, m.gname))
        mtimes[m.name] = m.mtime
    with open(path, 'rb') as f:
        head = f.read(512)
        f.seek(-1024, 2)
        tail = f.read()
    if head[257:265] != b'ustar  \x00' or os.path.getsize(path) % 10240 or tail != b'\0' * 1024:
        errs.append('E_OUTER_FORMAT: not GNU tar with 10240-byte records and zero end blocks')
    return errs, mtimes


# ---------------------------------------------------------------- main build

def build(a, out, report):
    entries, errors, manifest_sha = parse_manifest(a.overlay)
    report['overlay_manifest'] = {'path': os.path.realpath(a.overlay), 'sha256': manifest_sha,
                                  'entries': entries}
    if a.roundtrip_test and a.tag == BASE_VERSION and (entries or errors):
        errors.append('E_TEST_MODE: tag V2.43 is only for the EMPTY overlay exact-baseline experiment')
    inp = os.path.realpath(a.input)
    if os.path.islink(a.input) or not os.path.isfile(inp) or os.path.getsize(inp) != STOCK_SIZE or sha256_file(inp) != STOCK_SHA256:
        errors.append('E_INPUT_HASH: %s is not the pinned stock V2.43 update.bin (%s)' % (a.input, STOCK_SHA256))
    if any(e.startswith(('E_MANIFEST', 'E_PATH_SYNTAX', 'E_INPUT', 'E_TEST_MODE', 'E_SOURCE')) for e in errors):
        raise Refuse(errors)   # nothing further is meaningful; stage-dependent checks still run otherwise
    report['input'] = {'path': inp, 'sha256': STOCK_SHA256, 'size': STOCK_SIZE}

    stage = make_stage()
    report['stage'] = stage
    try:
        log('stage %s' % stage)
        outer_errs, stock_mtimes = check_outer(inp)
        if outer_errs:
            raise Refuse(outer_errs)
        pkg = os.path.join(stage, 'stock')
        os.mkdir(pkg)
        run(['tar', '-xpf', inp, '-C', pkg, '--numeric-owner'])
        s_info = os.path.join(pkg, 'firmware_v20.info')
        s_kernel = os.path.join(pkg, 'recovery-update', 'xImage')
        s_rootfs = os.path.join(pkg, 'recovery-update', 'rootfs.squashfs')
        info_lines = open(s_info, 'rb').read().split(b'\n')
        if sha256_file(s_info) != INFO_SHA256 or info_lines[0] != MODEL.encode() or info_lines[1] != BASE_VERSION.encode():
            raise Refuse(['E_INPUT_MODEL: stock info mismatch (model/version)'])
        if sha256_file(s_kernel) != XIMAGE_SHA256 or sha256_file(s_rootfs, 'md5') != ROOTFS_MD5:
            raise Refuse(['E_INPUT_PAYLOAD: stock xImage/rootfs hash mismatch'])

        rootfs = os.path.join(stage, 'rootfs')
        log('unsquashfs stock rootfs -> %s' % rootfs)
        run(['unsquashfs', '-no-progress', '-d', rootfs, s_rootfs])
        before = snapshot(rootfs)
        report['stock_tree'] = {'names': len(before) - 1, 'link_groups': len(link_groups(before))}

        # per-entry checks against the staged stock tree (all collected before any change)
        for e in entries:
            rel, p = e['path'], os.path.join(rootfs, e['path'])
            parts = rel.split('/')
            bad_parent = False
            for i in range(1, len(parts)):
                q = os.path.join(rootfs, *parts[:i])
                if not os.path.lexists(q) or os.path.islink(q) or not os.path.isdir(q):
                    errors.append('E_SYMLINK_PARENT: %s component %s is missing, a symlink or not a dir' % (rel, '/'.join(parts[:i])))
                    bad_parent = True
                    break
            if bad_parent:
                continue
            if not os.path.lexists(p):
                errors.append('E_MISSING: %s does not exist in stock rootfs (adds are not supported)' % rel)
                continue
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                errors.append('E_SYMLINK: %s is a symlink -> %s' % (rel, os.readlink(p)))
                continue
            if not stat.S_ISREG(st.st_mode):
                errors.append('E_NOT_REGULAR: %s' % rel)
                continue
            if st.st_nlink != 1:
                errors.append('E_HARDLINK: %s has %d links; overlay would change a hardlink group' % (rel, st.st_nlink))
            if before[rel]['sha256'] != e['old_sha256']:
                errors.append('E_OLD_HASH: %s stock sha256 %s != manifest old %s' % (rel, before[rel]['sha256'], e['old_sha256']))
            if stat.S_IMODE(st.st_mode) != e['mode']:
                errors.append('E_MODE: %s stock mode %04o != manifest %04o' % (rel, stat.S_IMODE(st.st_mode), e['mode']))
            if (st.st_uid, st.st_gid) != (e['uid'], e['gid']):
                errors.append('E_OWNER: %s stock %d:%d != manifest %d:%d' % (rel, st.st_uid, st.st_gid, e['uid'], e['gid']))
            if rel in ELF_PATHS:
                old_elf, new_elf = elf_info(p), elf_info(e['source'])
                e['elf_old'], e['elf_new'] = old_elf, new_elf
                errors += elf_errors(rel, old_elf, new_elf)
            elif rel in SCRIPT_PATHS:
                with open(e['source'], 'rb') as f:
                    body = f.read()
                if not body.startswith(b'#!/bin/sh\n') or b'\r' in body or b'\x00' in body:
                    errors.append('E_SCRIPT: %s must be LF-only /bin/sh text' % rel)
            else:
                from resource_guard import guard
                try: guard(open(p,'rb').read(),open(e['source'],'rb').read())
                except Exception as ex: errors.append('E_UI_RESOURCE: '+str(ex))
        if errors:
            raise Refuse(errors)

        # apply in place (same inode), restore metadata, then restore every ancestor mtime
        for e in entries:
            p = os.path.join(rootfs, e['path'])
            with open(e['source'], 'rb') as src, open(p, 'r+b') as dst:
                dst.truncate(0)
                shutil.copyfileobj(src, dst)
            os.chown(p, e['uid'], e['gid'], follow_symlinks=False)
            os.chmod(p, e['mode'])
            os.utime(p, (SOURCE_DATE_EPOCH, SOURCE_DATE_EPOCH), follow_symlinks=False)
            parts = e['path'].split('/')
            for i in range(len(parts) - 1, -1, -1):
                rel = '/'.join(parts[:i]) or '.'
                os.utime(os.path.join(rootfs, rel) if rel != '.' else rootfs,
                         (before[rel]['mtime'], before[rel]['mtime']), follow_symlinks=False)
            log('overlaid %s' % e['path'])

        after = snapshot(rootfs)
        diffs = diff_trees(before, after)
        expected = {e['path'] for e in entries}
        unexpected = {k: v for k, v in diffs.items() if k not in expected or not set(v) <= {'sha256', 'size', 'mtime'}}
        stale = sorted(expected - set(diffs))
        if unexpected or stale or link_groups(before) != link_groups(after):
            raise Refuse(['E_UNEXPECTED_DIFF: unexpected=%r stale=%r' % (unexpected, stale)])
        report['staged_diff'] = diffs

        log('mksquashfs %s' % ' '.join(MKSQUASHFS_ARGS))
        new_rootfs = os.path.join(stage, 'rootfs.squashfs')
        report['mksquashfs_report_tail'] = run(['mksquashfs', rootfs, new_rootfs] + MKSQUASHFS_ARGS).splitlines()[-12:]
        size = os.path.getsize(new_rootfs)
        report['rootfs'] = {'size': size, 'max': ROOTFS_MAX, 'headroom_bytes': ROOTFS_MAX - size,
                            'byte_identical_to_stock': open(new_rootfs, 'rb').read() == open(s_rootfs, 'rb').read()}
        if size > ROOTFS_MAX:
            raise Refuse(['E_ROOTFS_OVERSIZE: %d > %d bytes, gap %d bytes' % (size, ROOTFS_MAX, size - ROOTFS_MAX)])

        pkg2 = os.path.join(stage, 'package')
        os.makedirs(os.path.join(pkg2, 'recovery-update'))
        k2 = os.path.join(pkg2, 'recovery-update', 'xImage')
        r2 = os.path.join(pkg2, 'recovery-update', 'rootfs.squashfs')
        i2 = os.path.join(pkg2, 'firmware_v20.info')
        shutil.copyfile(s_kernel, k2)
        shutil.copyfile(new_rootfs, r2)
        if sha256_file(k2) != XIMAGE_SHA256:
            raise Refuse(['E_KERNEL: copied xImage differs from stock'])
        md5k, md5r = sha256_file(k2, 'md5'), sha256_file(r2, 'md5')
        with open(i2, 'wb') as f:
            f.write(('%s\n%s\n%s  recovery-update/xImage\n%s  recovery-update/rootfs.squashfs\n' % (
                MODEL, a.tag, md5k, md5r)).encode('ascii'))
        for name, _, mode in MEMBERS:
            p = os.path.join(pkg2, name.rstrip('/'))
            os.chmod(p, mode)
        for name, _, _ in reversed(MEMBERS):
            p = os.path.join(pkg2, name.rstrip('/'))
            os.utime(p, (stock_mtimes[name.rstrip('/')], stock_mtimes[name.rstrip('/')]))
        staged_bin = os.path.join(stage, 'update.bin')
        run(['tar', '--format=gnu', '--owner=%s:%d' % (OWNER_NAME, OWNER_ID), '--group=%s:%d' % (OWNER_NAME, OWNER_ID),
             '--no-recursion', '-cf', staged_bin] + [m[0] for m in MEMBERS], cwd=pkg2, env=dict(os.environ, LC_ALL='C'))

        # self-check of the produced archive (the independent verifier runs separately)
        outer_errs, new_mtimes = check_outer(staged_bin)
        if outer_errs or new_mtimes != stock_mtimes:
            raise Refuse(outer_errs or ['E_OUTER_MTIMES: %r != %r' % (new_mtimes, stock_mtimes)])
        exact = sha256_file(staged_bin) == STOCK_SHA256
        report['update_bin_byte_identical_to_stock'] = exact
        if a.roundtrip_test and a.tag == BASE_VERSION and not exact:
            raise Refuse(['E_ROUNDTRIP_NOT_EXACT: empty-overlay V2.43 rebuild is not byte-identical to stock'])

        dest = os.path.join(out, 'update.bin')
        shutil.copyfile(staged_bin, dest)
        outputs = {}
        for name, p in (('update.bin', dest), ('firmware_v20.info', i2)):
            if name != 'update.bin':
                shutil.copyfile(p, os.path.join(out, name))
            q = os.path.join(out, name)
            outputs[name] = {'size': os.path.getsize(q), 'sha256': sha256_file(q), 'md5': sha256_file(q, 'md5')}
        if outputs['update.bin']['sha256'] != sha256_file(staged_bin):
            raise Refuse(['E_PUBLISH: copied update.bin hash differs'])
        report['outputs'] = outputs
        report['payload'] = {'xImage_md5': md5k, 'rootfs_md5': md5r, 'xImage_sha256': XIMAGE_SHA256,
                             'rootfs_sha256': sha256_file(r2)}
        with open(os.path.join(out, 'SHA256SUMS.txt'), 'w', newline='\n') as f:
            for name, o in outputs.items():
                f.write('%s  %s\n' % (o['sha256'], name))
    finally:
        remove_stage(stage)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--input', required=True, help='pinned stock V2.43 update.bin (read only)')
    ap.add_argument('--overlay', required=True, help='overlay manifest TSV')
    ap.add_argument('--out', required=True, help='fresh directory below <task>/out/')
    ap.add_argument('--tag', default=DEFAULT_TAG)
    ap.add_argument('--roundtrip-test', action='store_true',
                    help='test build: output name must start with roundtrip-test-; tag V2.43 allowed only with an empty overlay')
    a = ap.parse_args()
    if os.geteuid() != 0 or not sys.platform.startswith('linux'):
        print('E_ENV: run as root inside WSL Linux', file=sys.stderr)
        return 2
    out, err = check_out_dir(a.out, a.roundtrip_test)
    if err:
        print(err, file=sys.stderr)
        return 2
    os.mkdir(out)
    report = {'tool': 'B2 build_candidate.py', 'status': 'unreviewed; offline archive only; NOT hardware verified',
              'started_utc': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
              'tag': a.tag, 'roundtrip_test': a.roundtrip_test, 'out': out, 'allowed_overlay_paths': ALLOWED_OVERLAY_PATHS,
              'mksquashfs_args': MKSQUASHFS_ARGS, 'tools': tool_versions()}
    rc = 0
    try:
        err = check_tag(a.tag, a.roundtrip_test)
        if err:
            raise Refuse([err])
        build(a, out, report)
        report['verdict'] = 'BUILT'
        log('BUILT %s (rootfs headroom %d bytes)' % (os.path.join(out, 'update.bin'), report['rootfs']['headroom_bytes']))
    except Refuse as r:
        report['verdict'], report['errors'] = 'REFUSED', r.errors
        for e in r.errors:
            print('REFUSED ' + e, file=sys.stderr)
        rc = 1
    except Exception as x:   # unexpected crash is also a refusal, never a silent success
        report['verdict'], report['errors'] = 'REFUSED', ['E_INTERNAL: %r' % x]
        print('REFUSED E_INTERNAL: %r' % x, file=sys.stderr)
        rc = 1
    if rc:
        bin_path = os.path.join(out, 'update.bin')
        if os.path.lexists(bin_path):
            os.remove(bin_path)   # never leave a partial archive next to a refusal
    name = 'BUILD-MANIFEST.json' if rc == 0 else 'REFUSED.json'
    with open(os.path.join(out, name), 'w', newline='\n') as f:
        json.dump(report, f, indent=1, default=str)
        f.write('\n')
    return rc


if __name__ == '__main__':
    sys.exit(main())
