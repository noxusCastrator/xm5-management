"""Rebuild and run the public MIPS boundary tests with explicit local tools."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--toolchain', type=Path, required=True)
    parser.add_argument('--qemu', type=Path, required=True)
    parser.add_argument('--peq08-demo', type=Path, required=True, help='Hash-pinned PEQ08 demo; read-only')
    args = parser.parse_args()
    toolchain = args.toolchain.resolve()
    source = REPO / 'src/native_storage'
    environment = dict(os.environ, LD_LIBRARY_PATH=str(toolchain / 'usr/lib/x86_64-linux-gnu'))
    flags = [f'-B{toolchain}/usr/lib/gcc-cross/mipsel-linux-gnu/12/', '-DXM5_FREESTANDING',
             '-march=mips32r2', '-mabi=32', '-EL', '-mhard-float', '-mfpxx', '-mno-odd-spreg',
             '-mnan=2008', '-G0', '-mno-abicalls', '-fno-pic', '-fno-pie', '-fno-stack-protector',
             '-ffreestanding', '-fno-builtin', '-ffunction-sections', '-fdata-sections', '-Os',
             '-Wall', '-Wextra', '-Werror', '-Wno-misleading-indentation', '-Wno-parentheses']
    gcc, linker = toolchain/'usr/bin/mipsel-linux-gnu-gcc-12', toolchain/'usr/bin/mipsel-linux-gnu-ld'
    with tempfile.TemporaryDirectory(prefix='xm5-mips-boundary-') as temporary:
        root = Path(temporary)
        for name in ('extract_paint.py', 'extract_owner.py'):
            subprocess.run([sys.executable, str(REPO/'tools/native_tests'/name), '--peq08-demo',
                            str(args.peq08_demo.resolve()), '--output', str(root)], check=True)
        thunks = (source/'hook_thunks.S').read_text()
        bridge = thunks.split('.globl pa_call_foreign_close', 1)[1].split('THUNK', 1)[0]
        (root/'close-bridge.S').write_text('.set noreorder\n.option pic0\n.text\n.globl pa_call_foreign_close'+bridge)
        (root/'gp-thunk.S').write_text(thunks.split('.endm', 1)[0]+'.endm\nTHUNK gp_probe,0x20001000\n')
        objects = {}
        cases = {'managed': ['test_managed.c', 'managed_image.c', 'minic.c', 'start.S', 'build/paint.S'],
                 'bitmap': ['test_bitmap.c', 'bitmap_safe.c', 'prepared_io.c', 'minic.c', 'start.S', 'build/owner.S'],
                 'hooks': ['test_hooks.c', 'player_hooks.c', 'minic.c', 'start.S', 'test_globals.S', 'close-bridge.S'],
                 'gp': ['test_gp.c', 'test_gp_stubs.S', 'start.S', 'gp-thunk.S']}
        for case, names in cases.items():
            for name in names:
                if name not in objects:
                    output = root / (Path(name).name + '.o')
                    input_path = source/name if (source/name).exists() else root/name
                    subprocess.run([str(gcc), *flags, '-c', str(input_path), '-o', str(output)],
                                   cwd=root, env=environment, check=True)
                    objects[name] = output
            binary = root / case
            subprocess.run([str(linker), '-m', 'elf32ltsmip', *(['--gc-sections'] if case == 'hooks' else []),
                            '-T', str(source/(case+'_test.ld')), *[str(objects[n]) for n in names],
                            '-o', str(binary)], env=environment, check=True)
            subprocess.run([str(args.qemu.resolve()), '-cpu', 'P5600', str(binary)], check=True)


if __name__ == '__main__':
    main()
