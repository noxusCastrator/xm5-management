#!/usr/bin/env python3
"""Build the reproducible x64 Windows GUI launcher using native or WSL MinGW."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def wsl_path(path):
    return subprocess.check_output(["wsl", "--exec", "wslpath", "-a", str(Path(path).resolve())], text=True).strip()


def build(output: Path, icon: Path, *, wsl=False, cc="x86_64-w64-mingw32-gcc", windres="x86_64-w64-mingw32-windres"):
    output, icon = output.resolve(), icon.resolve()
    if output.exists() or not icon.is_file() or icon.is_symlink():
        raise ValueError("Choose a new output executable and a regular .ico input")
    output.parent.mkdir(parents=True, exist_ok=True)
    source = ROOT / "src/desktop_launcher"
    prefix = ["wsl", "--exec"] if wsl else []
    convert = wsl_path if wsl else lambda value: str(Path(value).resolve())
    version = subprocess.check_output(prefix + [cc, "--version"], text=True).splitlines()[0]
    with tempfile.TemporaryDirectory(prefix="launcher-", dir=output.parent) as temporary:
        stage = Path(temporary)
        shutil.copyfile(icon, stage / "app.ico")
        for name in ("launcher.rc", "launcher.manifest"):
            shutil.copyfile(source / name, stage / name)
        subprocess.run(prefix + [windres, "-I", convert(stage), "-i", convert(stage / "launcher.rc"),
                                 "-o", convert(stage / "resource.o")], check=True)
        subprocess.run(prefix + [cc, "-std=c11", "-Os", "-Wall", "-Wextra", "-Werror", "-municode", "-mwindows", "-static-libgcc",
                                 "-Wl,--no-insert-timestamp", "-s", convert(source / "main.c"), convert(stage / "resource.o"),
                                 "-lshell32", "-lole32", "-o", convert(output)], check=True)
    receipt = {"schema": "xm5.launcher-build.v1", "compiler": version, "architecture": "Windows x64",
               "icon_sha256": sha(icon), "output_sha256": sha(output),
               "sources": {p.name: sha(p) for p in sorted(source.iterdir()) if p.is_file()}}
    output.with_suffix(".build.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8", newline="\n")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--icon", type=Path, default=ROOT / "desktop/xm5lib/ui/assets/studio/app.ico")
    parser.add_argument("--wsl", action="store_true")
    parser.add_argument("--cc", default="x86_64-w64-mingw32-gcc")
    parser.add_argument("--windres", default="x86_64-w64-mingw32-windres")
    args = parser.parse_args()
    print(json.dumps(build(args.output, args.icon, wsl=args.wsl, cc=args.cc, windres=args.windres)))


if __name__ == "__main__":
    main()
