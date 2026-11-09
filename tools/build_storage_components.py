"""Build storage components offline from source and explicit pinned inputs.

This creates separate ELF copies. It never repackages firmware or touches a
device/card. Existing output is refused. See docs/BUILD_STABLE.md.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

REPO=Path(__file__).resolve().parents[1]
HCI_PIN="edec6c420a792bd4b4e47df3b34a5bf5fbb518a07cb1c8509ec1e50cb87e1f2d"
DEMO_PIN="e3596659301a04e61882506c0ff2c48013866ca800ceff5e67c531486feb7df3"
BUILD_LOG=None

def sha(p):
    with p.open("rb") as f:return hashlib.file_digest(f,"sha256").hexdigest()

def run(args,*,cwd=None,env=None,capture=False):
    command=[str(x) for x in args]
    if capture:return subprocess.run(command,cwd=cwd,env=env,check=True,text=True,stdout=subprocess.PIPE).stdout
    with BUILD_LOG.open("a") as log:
        log.write("\nCOMMAND "+repr(command)+"\n");log.flush()
        completed=subprocess.run(command,cwd=cwd,env=env,text=True,stdout=log,stderr=subprocess.STDOUT)
    if completed.returncode:
        print("\n".join(BUILD_LOG.read_text(errors="replace").splitlines()[-22:]),file=sys.stderr)
        raise RuntimeError(f"Component build failed ({completed.returncode}); see {BUILD_LOG}")

def main():
    global BUILD_LOG
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workspace",type=Path,help="Optional existing input workspace; read-only")
    ap.add_argument("--stock-rootfs",type=Path)
    ap.add_argument("--peq08-demo",type=Path)
    ap.add_argument("--toolchain",type=Path)
    ap.add_argument("--binutils",type=Path)
    ap.add_argument("--output",type=Path,required=True)
    a=ap.parse_args()
    if sys.platform!="linux":ap.error("Run in Linux/WSL.")
    ws=a.workspace.resolve() if a.workspace else None
    stock=(a.stock_rootfs or (ws/"roundtrip-v2.43/unpacked/rootfs" if ws else Path("/missing"))).resolve()
    baseline=(a.peq08_demo or (ws/"peq-lab/tasks/U9-four-row-peq-ui/out/demo" if ws else Path("/missing"))).resolve()
    tr=(a.toolchain or (ws/"peq-lab/tasks/T1-toolchain/toolchain/root" if ws else Path("/missing"))).resolve()
    bins=(a.binutils or tr/"usr/bin").resolve(); output=a.output.resolve()
    if output.exists():ap.error("Output exists; preserve it and choose a new directory.")
    if not (stock/"usr/bin/hciplayer").is_file() or sha(stock/"usr/bin/hciplayer")!=HCI_PIN:ap.error("Stock hciplayer pin mismatch.")
    if not baseline.is_file() or sha(baseline)!=DEMO_PIN:ap.error("PEQ08 demo pin mismatch.")
    gcc=tr/"usr/bin/mipsel-linux-gnu-gcc-12"
    for p in [gcc,*[bins/("mipsel-linux-gnu-"+n) for n in ("ld","objcopy","objdump","nm")]]:
        if not p.is_file():ap.error(f"Missing tool: {p}")
    output.mkdir(parents=True)
    BUILD_LOG=output/"BUILD.log"
    # Existing adapters derive their paths from a directory named workspace.
    # This private layout provides that contract without accessing the original.
    private=output/"workspace"; stage=private/"peq-lab/phase2/stable-20261108"
    stage.mkdir(parents=True)
    for src,dest in (("native_storage","native"),("audio_storage","audio"),("organizer","organizer")):
        shutil.copytree(REPO/"src"/src,stage/dest)
    rootfs=private/"roundtrip-v2.43/unpacked/rootfs";rootfs.parent.mkdir(parents=True);rootfs.symlink_to(stock,target_is_directory=True)
    demo=private/"peq-lab/tasks/U9-four-row-peq-ui/out/demo";demo.parent.mkdir(parents=True);shutil.copyfile(baseline,demo)
    tool=private/"peq-lab/tasks/T1-toolchain/toolchain/root";tool.parent.mkdir(parents=True);tool.symlink_to(tr,target_is_directory=True)
    env=os.environ.copy();env["XM5_WORKSPACE"]=str(private)
    env["LD_LIBRARY_PATH"]=str(tr/"usr/lib/x86_64-linux-gnu")+":"+str(bins.parent/"lib/x86_64-linux-gnu")
    n=stage/"native";b=n/"build";b.mkdir();(n/"out").mkdir()
    run([sys.executable,"extra_hooks.py"],cwd=n,env=env)
    flags=[f"-B{tr}/usr/lib/gcc-cross/mipsel-linux-gnu/12/","-DXM5_FREESTANDING","-march=mips32r2","-mabi=32","-EL","-mhard-float","-mfpxx","-mno-odd-spreg","-mnan=2008","-G0","-mno-abicalls","-fno-pic","-fno-pie","-fno-stack-protector","-ffreestanding","-fno-builtin","-ffunction-sections","-fdata-sections","-Os","-Wall","-Wextra","-Werror","-Wno-misleading-indentation","-Wno-parentheses"]
    objects=[]
    for name in ("player_hooks.c","bitmap_safe.c","managed_image.c","hook_thunks.S","prepared_io.c","minic.c"):
        obj=b/(Path(name).stem+".o");run([gcc,*flags,"-c",name,"-o",obj],cwd=n,env=env);objects.append(obj)
    elf=b/"art-payload.elf"
    run([bins/"mipsel-linux-gnu-ld","-m","elf32ltsmip","-T","payload.ld",*objects,"-o",elf],cwd=n,env=env)
    if run([bins/"mipsel-linux-gnu-nm","-u",elf],env=env,capture=True).strip():raise RuntimeError("Undefined native symbols")
    (b/"art-payload.sym").write_text(run([bins/"mipsel-linux-gnu-nm","-n",elf],env=env,capture=True))
    run([bins/"mipsel-linux-gnu-objcopy","-O","binary",elf,b/"art-payload.bin"],env=env)
    run([sys.executable,"patch_demo.py"],cwd=n,env=env)
    audio=stage/"audio/audio-info1"
    (audio/"out").mkdir(exist_ok=True)
    run([sys.executable,"scripts/derive_plt.py"],cwd=audio,env=env)
    run(["bash","scripts/build_payload.sh"],cwd=audio,env=env)
    run([sys.executable,"scripts/build_candidate.py"],cwd=audio,env=env)
    org=stage/"organizer";ob=org/"build";(ob/"headers/gnu").mkdir(parents=True)
    (ob/"headers/gnu/stubs-o32_hard_2008.h").touch()
    run([gcc,f"-B{tr}/usr/lib/gcc-cross/mipsel-linux-gnu/12/",f"--sysroot={tr}","-isystem",tr/"usr/mipsel-linux-gnu/include","-I",ob/"headers","-mabi=32","-EL","-march=mips32r2","-mhard-float","-mfpxx","-mno-odd-spreg","-mnan=2008","-fPIC","-fvisibility=hidden","-shared","-nostdlib","-fno-stack-protector","-std=c11","-Wall","-Wextra","-Werror","-Wno-misleading-indentation","-Os","inline_flac.c","organizer.c","formats/other_container.c","-Wl,-z,defs,--no-as-needed",f"-L{stock}/lib","-l:libc-2.28.so","-o",ob/"libxm5inline.so"],cwd=org,env=env)
    run([sys.executable,REPO/"tools/elf_compact.py","--input",ob/"libxm5inline.so","--output",ob/"libxm5inline.compact.so","--manifest",ob/"COMPACT.json"],env=env)
    artifacts={"demo":n/"out/demo","hciplayer.candidate":audio/"out/hciplayer.candidate","libxm5inline.so":ob/"libxm5inline.compact.so","xm5-organize":org/"xm5-organize"}
    manifest={"schema":"xm5.storage-components.v1","stock_hciplayer_sha256":HCI_PIN,"peq08_demo_sha256":DEMO_PIN,"files":{}}
    for name,p in artifacts.items():
        shutil.copyfile(p,output/name);manifest["files"][name]={"size":p.stat().st_size,"sha256":sha(p)}
    (output/"COMPONENTS.json").write_text(json.dumps(manifest,indent=2)+"\n")
    print(json.dumps(manifest,indent=2))

if __name__=="__main__":main()
