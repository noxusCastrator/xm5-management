"""Compile the actual reader with host GCC and test owned, synthetic cards.

Run on Linux/WSL: python3 tools/run_native_directory_tests.py
Only Python's standard library and GCC are required. No hardware is opened.
"""
from pathlib import Path
import struct
import subprocess
import tempfile

REPO = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix='xm5-native-directory-') as temporary:
        root = Path(temporary)
        empty = b'fLaC' + bytes([128, 0, 0, 34]) + bytes(34)
        def prepared(art):
            block = bytearray(143372)
            block[:4] = b'XM5I'
            for side, offset in ((330, 4), (160, 98308), (80, 131076)):
                if art:
                    picture = (REPO / f'tools/runtime-source/test-inputs/cover-{side}.jpg').read_bytes()
                    struct.pack_into('<I', block, offset, len(picture))
                    block[offset + 4:offset + 4 + len(picture)] = picture
            struct.pack_into('<ii', block, 143364, -2147483648, -2147483648)
            return b'fLaC' + bytes([0, 0, 0, 34]) + bytes(34) + bytes([130]) + len(block).to_bytes(3, 'big') + block
        for case, hint, last, zeros in (('noart', 0, False, False), ('late', 1, True, False),
                                       ('stalezero', 0, True, False), ('zeroslot', 1, False, True)):
            card = root / case
            music, meta = card / 'xm5/music/Album', card / 'xm5/Struct'
            music.mkdir(parents=True); meta.mkdir(parents=True)
            for i in range(129):
                data = prepared(True) if last and i == 128 else prepared(False) if zeros else empty
                (music / f'{i:05d}.flac').write_bytes(data)
            lines = ''.join(f'{hint}\txm5/music/Album/{i:05d}.flac\n' for i in range(129))
            (meta / 'tracks.tsv').write_text(lines)
            (card / 'replacement.tsv').write_text(lines)
            (card / 'prepared.flac').write_bytes(prepared(True))
        binary = root / 'directory-test'
        subprocess.run(['gcc', '-std=gnu11', '-O2', '-Wall', '-Wextra', '-Werror',
                        '-Wno-misleading-indentation', '-Wno-parentheses',
                        str(REPO/'tests/native/test_directory.c'), '-Wl,--wrap=openat', '-o', str(binary)], check=True)
        subprocess.run([str(binary), str(root)], check=True)


if __name__ == '__main__':
    main()
