"""Export unmodified stock AWTK inputs and verbatim widget subtrees.

This tool supplies no widget layout, styles, callbacks, or invented UI text.
All visual properties come from the original compiled resource.
"""
import hashlib
import json
import struct
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE.parents[1] / 'roundtrip-v2.43/unpacked/rootfs/release/assets/default/raw'
OUT = HERE / 'stock'
MAGIC = b'\x12\x12\x22\x11'


def cstr(buf, pos):
    end = buf.index(b'\0', pos)
    return buf[pos:end].decode('utf-8'), end + 1


def parse(buf, pos):
    start = pos
    kind = buf[pos:pos+32].split(b'\0')[0].decode('ascii')
    rect = list(struct.unpack_from('<4i', buf, pos+32))
    pos += 48
    props = []
    while buf[pos]:
        key, pos = cstr(buf, pos)
        value, pos = cstr(buf, pos)
        props.append([key, value])
    pos += 1
    children = []
    while buf[pos]:
        child, pos = parse(buf, pos)
        children.append(child)
    pos += 1
    return dict(type=kind, rect=rect, props=props, children=children,
                byte_start=start, byte_end=pos), pos


def encode(node):
    data = node['type'].encode('ascii').ljust(32, b'\0')
    data += struct.pack('<4i', *node['rect'])
    for key, value in node['props']:
        data += key.encode('utf-8') + b'\0' + value.encode('utf-8') + b'\0'
    return data + b'\0' + b''.join(map(encode, node['children'])) + b'\0'


def walk(node):
    yield node
    for child in node['children']:
        yield from walk(child)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    manifest = []

    def copy(rel, dest=None):
        data = (RAW / rel).read_bytes()
        path = OUT / 'assets/default/raw' / (dest or rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        assert path.read_bytes() == data
        manifest.append(dict(source=rel, output=path.relative_to(HERE).as_posix(),
                             bytes=len(data), sha256=sha(data), unchanged=True))

    inputs = ['ui/playset/equalizer_page.bin', 'ui/dialog/preseteq_dialog.bin',
              'ui/dialog/confirminfo_dialog.bin']
    images = set()
    trees = {}
    for rel in inputs:
        data = (RAW / rel).read_bytes()
        assert data[:4] == MAGIC
        tree, end = parse(data, 4)
        assert end == len(data)
        assert MAGIC + encode(tree) == data, f'Lossless roundtrip failed: {rel}'
        trees[rel] = tree
        copy(rel)
        for node in walk(tree):
            for key, value in node['props']:
                if value and (key == 'image' or key.endswith(':bg_image') or key.endswith(':icon')):
                    images.add(value)
            name = dict(node['props']).get('name')
            if rel == inputs[0] and name in ('view_on', 'view_off'):
                # The subtree is sliced verbatim; only the format magic is prepended.
                payload = MAGIC + data[node['byte_start']:node['byte_end']]
                path = OUT / f'equalizer_{name}.bin'
                path.write_bytes(payload)
                assert payload == MAGIC + encode(node)
                manifest.append(dict(source=rel, source_span=[node['byte_start'], node['byte_end']],
                                     output=path.relative_to(HERE).as_posix(), bytes=len(payload),
                                     sha256=sha(payload), unchanged_subtree=True))
    for name in sorted(images):
        candidates = list((RAW / 'images/xx').glob(name + '.*'))
        assert len(candidates) == 1, (name, candidates)
        copy(candidates[0].relative_to(RAW).as_posix())
    for rel in ['styles/default.bin', 'strings/zh_CN.bin', 'strings/en_US.bin', 'fonts/default.ttf']:
        copy(rel)
    # Upstream preview_ui's default font identifier is default_full. Its bytes
    # are still the original font; this alias avoids modifying the viewer's GUI.
    copy('fonts/default.ttf', 'fonts/default_full.ttf')
    (HERE / 'stock-trees.json').write_text(json.dumps(trees, ensure_ascii=False, indent=2), encoding='utf-8')
    (HERE / 'stock-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(f'Exported {len(manifest)} files; all full UI binaries roundtrip byte-for-byte.')


if __name__ == '__main__':
    main()
