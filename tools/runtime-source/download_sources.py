"""Acquire only named upstream source archives; freeze their exact SHA256."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import tarfile
import urllib.request

ROOT=Path("/var/tmp/xm5-runtime-build-20261108")
SOURCES={
 "ffmpeg":("8.0.3","https://ffmpeg.org/releases/ffmpeg-8.0.3.tar.xz"),
 "mpv":("902818753849d9ad8f0096fad6ca5aeaa37113b9","https://codeload.github.com/mpv-player/mpv/tar.gz/902818753849d9ad8f0096fad6ca5aeaa37113b9"),
 "libplacebo":("7.387.0","https://codeload.github.com/haasn/libplacebo/tar.gz/refs/tags/v7.387.0"),
 "freetype":("2.13.3","https://download.savannah.gnu.org/releases/freetype/freetype-2.13.3.tar.xz"),
 "harfbuzz":("10.4.0","https://codeload.github.com/harfbuzz/harfbuzz/tar.gz/refs/tags/10.4.0"),
 "fribidi":("1.0.16","https://github.com/fribidi/fribidi/releases/download/v1.0.16/fribidi-1.0.16.tar.xz"),
 "libass":("0.17.4","https://github.com/libass/libass/releases/download/0.17.4/libass-0.17.4.tar.xz"),
 "zlib":("1.3.1","https://zlib.net/fossils/zlib-1.3.1.tar.gz"),
 "mingw-w64":("11.0.1","https://codeload.github.com/mingw-w64/mingw-w64/tar.gz/refs/tags/v11.0.1"),
 "fast_float":("97b54ca9e75f5303507699d27c6b4f4efe4641a1","https://codeload.github.com/fastfloat/fast_float/tar.gz/97b54ca9e75f5303507699d27c6b4f4efe4641a1"),
 "Vulkan-Headers":("74d8a6cb930c68ef617b202c3ff3c59d919e086b","https://codeload.github.com/KhronosGroup/Vulkan-Headers/tar.gz/74d8a6cb930c68ef617b202c3ff3c59d919e086b"),
}

def fetch(item):
 name,(version,url)=item
 ext=".tar.xz" if url.endswith(".xz") else ".tar.gz"
 archive=ROOT/"sources"/(name+"-"+version+ext)
 if not archive.exists():
  req=urllib.request.Request(url,headers={"User-Agent":"XM5-source-build/0.2.0"})
  with urllib.request.urlopen(req,timeout=90) as response:archive.write_bytes(response.read())
 data=archive.read_bytes();digest=hashlib.sha256(data).hexdigest()
 lock_file=Path(__file__).with_name("SOURCES.json")
 if lock_file.exists():
  expected=json.loads(lock_file.read_text())[name]
  if expected["url"]!=url or expected["sha256"]!=digest or expected["size"]!=len(data):raise ValueError("Frozen source input differs: "+name)
 dest=ROOT/"src"/name
 if not dest.exists():
  with tarfile.open(archive) as t:
   roots={m.name.split("/")[0] for m in t.getmembers()}
   if len(roots)!=1:raise ValueError(name+" has multiple roots")
   t.extractall(ROOT/"src",filter="data")
   (ROOT/"src"/roots.pop()).rename(dest)
 return name,{"version":version,"url":url,"file":archive.name,"size":len(data),"sha256":digest}

def main():
 if ROOT.resolve()!=Path("/var/tmp/xm5-runtime-build-20261108"):raise ValueError("Wrong build root")
 for folder in ("sources","src","build","prefix","logs"):(ROOT/folder).mkdir(parents=True,exist_ok=True)
 with ThreadPoolExecutor(max_workers=3) as pool:
  rows=dict(pool.map(fetch,SOURCES.items()))
 for name in ("fast_float","Vulkan-Headers"):
  link=ROOT/"src/libplacebo/3rdparty"/name
  if link.is_dir() and not any(link.iterdir()):link.rmdir()
  if not link.exists():link.symlink_to(ROOT/"src"/name,target_is_directory=True)
 (ROOT/"SOURCES.json").write_text(json.dumps(rows,indent=2)+"\n")
 print(json.dumps(rows,indent=2))

if __name__=="__main__":main()
