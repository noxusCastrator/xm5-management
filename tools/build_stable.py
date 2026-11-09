"""Reconstruct the paired four-overlay firmware from explicit official inputs.

Builds offline copies only. No device/card operation or publication is present.
"""
from __future__ import annotations
import argparse
import base64
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys

REPO=Path(__file__).resolve().parents[1]
STOCK="8b3f05155840c7b821f3bcbcd4690eaf64a00e867c7c3cef68f7be1959368be4"

def sha(p):
 with p.open("rb") as f:return hashlib.file_digest(f,"sha256").hexdigest()
def run(args):subprocess.run([str(x) for x in args],check=True)
def module(name,p):
 s=importlib.util.spec_from_file_location(name,p);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
def compact(source,dest):
 c=module("stable_compactor",REPO/"tools/elf_compact.py");raw=source.read_bytes();ranges,protected,_=c.plan(raw);data=c.compact(raw,ranges)
 expected=bytearray(raw);struct.pack_into("<I",expected,32,0);struct.pack_into("<HH",expected,48,0,0)
 for name,start,end in protected:
  if data[start:end]!=expected[start:end]:raise ValueError("Compaction changed runtime range: "+name)
 dest.write_bytes(data)
def embedded(path, target):
    data = path.read_bytes()
    packed = bytearray(gzip.compress(data, compresslevel=9, mtime=0))
    packed[9] = 255  # deterministic gzip OS byte across Python platforms
    encoded = base64.encodebytes(packed).decode("ascii")
    digest = hashlib.sha256(data).hexdigest()
    return (f"if base64 -d <<'XM5_EMBEDDED_EOF' | gzip -d >{target}.part\n" + encoded +
            "XM5_EMBEDDED_EOF\nthen\n" +
            f" if [ \"$(sha256sum {target}.part | cut -d ' ' -f 1)\" = '{digest}' ]; then\n"+
            f"  chmod 755 {target}.part && mv {target}.part {target}\n"+
            f" else rm -f {target}.part; fi\nelse rm -f {target}.part; fi\n")

def main():
 ap=argparse.ArgumentParser(description=__doc__)
 ap.add_argument("--stock",type=Path,required=True)
 ap.add_argument("--toolchain",type=Path,required=True)
 ap.add_argument("--binutils",type=Path)
 ap.add_argument("--output",type=Path,required=True)
 ap.add_argument("--peq08-build",type=Path,help="Optional existing completed historical build/work/xm5-workspace; read-only")
 a=ap.parse_args();stock=a.stock.resolve();tr=a.toolchain.resolve();out=a.output.resolve()
 if sys.platform!="linux" or os.geteuid()!=0:ap.error("Run as root in Linux/WSL to preserve rootfs metadata.")
 if sha(stock)!=STOCK:ap.error("Official V2.43 input hash mismatch.")
 if out.exists():ap.error("Output exists; choose a fresh directory.")
 out.mkdir(parents=True)
 if a.peq08_build:
  alpha=a.peq08_build.resolve()
 else:
  baseline=out/"peq08-source"
  for name in ("src","tools"):shutil.copytree(REPO/name,baseline/name,ignore=shutil.ignore_patterns("__pycache__","downloads","output"))
  shutil.copyfile(REPO/"tools/portable/alpha-release.json",baseline/"release.json")
  run([sys.executable,baseline/"tools/build.py","--stock",stock,"--toolchain",tr])
  alpha=baseline/"build/work/xm5-workspace"
 rootfs=alpha/"roundtrip-v2.43/unpacked/rootfs"
 demo=alpha/"peq-lab/tasks/U9-four-row-peq-ui/out/demo"
 resource=alpha/"peq-lab/tasks/U9-four-row-peq-ui/out/equalizer_page.bin"
 components=out/"components"
 cmd=[sys.executable,REPO/"tools/build_storage_components.py","--stock-rootfs",rootfs,"--peq08-demo",demo,"--toolchain",tr,"--output",components]
 if a.binutils:cmd += ["--binutils",a.binutils.resolve()]
 run(cmd)
 package=out/"package";scripts=package/"scripts";overlay=package/"build/overlay"
 scripts.mkdir(parents=True);overlay.mkdir(parents=True);(package/"out").mkdir()
 for name in ("build_candidate.py","verify_candidate.py","resource_guard.py","awtk_resource.py"):shutil.copyfile(REPO/"tools/package_stable"/name,scripts/name)
 compact(components/"demo",overlay/"demo");compact(components/"hciplayer.candidate",overlay/"hciplayer")
 patch=json.loads((REPO/"src/startup/library/library-patch.json").read_text())
 original=(rootfs/"etc/init.d/S90play").read_bytes()
 if hashlib.sha256(original).hexdigest()!=patch["stock_sha256"]:raise ValueError("Startup input hash mismatch")
 body=original
 for edit in sorted(patch["edits"],key=lambda r:r["offset"],reverse=True):
  i,n=edit["offset"],edit["delete_bytes"]
  if hashlib.sha256(body[i:i+n]).hexdigest()!=edit["before_sha256"]:raise ValueError("Startup edit context mismatch")
  body=body[:i]+(REPO/edit["file"]).read_bytes()+body[i+n:]
 if hashlib.sha256(body).hexdigest()!=patch["output_sha256"]:raise ValueError("Startup reconstruction mismatch")
 text=body.decode();needle="    /release/bin/demo &"
 if text.count(needle)!=1:raise ValueError("Startup demo launch context mismatch")
 text=text.replace(needle,embedded(components/"libxm5inline.so","/tmp/libxm5inline.so")+embedded(components/"xm5-organize","/tmp/xm5-organize")+needle)
 (overlay/"S90play").write_text(text,newline="\n")
 shutil.copyfile(resource,overlay/"equalizer_page.bin")
 names={"release/bin/demo":"demo","usr/bin/hciplayer":"hciplayer","etc/init.d/S90play":"S90play","release/assets/default/raw/ui/playset/equalizer_page.bin":"equalizer_page.bin"}
 lines=["xm5-overlay-manifest\t1","model\tONIX XM5","base_version\tV2.43","base_sha256\t"+STOCK]
 for (name,filename),mode in zip(names.items(),(0o755,0o775,0o775,0o744)):
  lines.append("\t".join(("file",name,sha(rootfs/name),sha(overlay/filename),f"{mode:04o}","0:0","overlay/"+filename)))
 manifest=package/"build/lib01-manifest.tsv";manifest.write_text("\n".join(lines)+"\n",newline="\n")
 final=package/"out/final"
 run([sys.executable,scripts/"build_candidate.py","--input",stock,"--overlay",manifest,"--out",final,"--tag","V2.43-LIB01"])
 run([sys.executable,scripts/"verify_candidate.py","--stock",stock,"--candidate",final/"update.bin","--overlay",manifest,"--expect-tag","V2.43-LIB01","--out",final/"INDEPENDENT-VERIFY.json"])
 metadata={"version":"0.2.0","tag":"v0.2.0","firmware_tag":"V2.43-LIB01","base_version":"ONIX XM5 V2.43","base_sha256":STOCK,"update_sha256":sha(final/"update.bin"),"expected_overlays":{name:sha(overlay/file) for name,file in names.items()},"physical_operations":False}
 (out/"release-reconstruction.json").write_text(json.dumps(metadata,indent=2)+"\n")
 print(json.dumps(metadata,indent=2))

if __name__=="__main__":main()
