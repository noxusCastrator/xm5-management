"""Bounded MinGW local-audio build from the frozen source manifest.

All generated files stay beneath /var/tmp/xm5-runtime-build-20261108.
No installer or player is run by this build script.
"""
from __future__ import annotations
import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT=Path("/var/tmp/xm5-runtime-build-20261108")
SRC=ROOT/"src";BUILD=ROOT/"build";PREFIX=ROOT/"prefix"
CROSS="x86_64-w64-mingw32-"
ENV=os.environ.copy()
ENV.update({"CC":CROSS+"gcc","CXX":CROSS+"g++","AR":CROSS+"ar","RANLIB":CROSS+"ranlib","STRIP":CROSS+"strip","PKG_CONFIG_LIBDIR":str(PREFIX/"lib/pkgconfig")+":"+str(PREFIX/"share/pkgconfig"),"PKG_CONFIG_PATH":"","CFLAGS":"-O2 -g0","CXXFLAGS":"-O2 -g0","LDFLAGS":"-static -static-libgcc -static-libstdc++"})
LOG=ROOT/"logs/build.log"

def run(args,cwd=None,extra=None):
 env=ENV.copy();env.update(extra or {})
 with LOG.open("a") as out:
  out.write("\nCOMMAND "+repr([str(x) for x in args])+"\n");out.flush()
  p=subprocess.run([str(x) for x in args],cwd=cwd,env=env,stdout=out,stderr=subprocess.STDOUT)
 if p.returncode:raise SystemExit(f"Build failed ({p.returncode}); see {LOG}")

def meson(name,*options):
 b=BUILD/name
 if not (b/"meson-private/coredata.dat").exists():
  run(["meson","setup",b,SRC/name,"--cross-file",ROOT/"cross.ini","--prefix",PREFIX,"--buildtype=release","--default-library=static","--wrap-mode=nodownload","-Dauto_features=disabled",*options])
 elif name=="mpv":
  run(["meson","setup","--reconfigure","--clearcache",b,SRC/name])
 run(["ninja","-C",b,"-j8"])
 run(["meson","install","-C",b,"--no-rebuild"])

def build_zlib():
 if (PREFIX/"lib/libz.a").exists():return
 b=BUILD/"zlib";b.mkdir(exist_ok=True)
 run([SRC/"zlib/configure",f"--prefix={PREFIX}","--static"],cwd=b,extra={"CHOST":"x86_64-w64-mingw32"})
 run(["make","-j8"],cwd=b);run(["make","install"],cwd=b)

def build_ffmpeg():
 if (PREFIX/"bin/ffmpeg.exe").exists():return
 b=BUILD/"ffmpeg";b.mkdir(exist_ok=True)
 args=[SRC/"ffmpeg/configure",f"--prefix={PREFIX}","--target-os=mingw32","--arch=x86_64",f"--cross-prefix={CROSS}","--enable-cross-compile","--pkg-config=pkg-config","--pkg-config-flags=--static",f"--extra-cflags=-I{PREFIX}/include -O2 -g0",f"--extra-ldflags=-L{PREFIX}/lib -static -static-libgcc","--disable-autodetect","--disable-gpl","--disable-nonfree","--disable-version3","--disable-doc","--disable-debug","--disable-shared","--enable-static","--enable-small","--disable-network","--disable-pthreads","--enable-w32threads","--disable-everything","--enable-ffmpeg","--disable-ffplay","--disable-ffprobe","--enable-zlib","--enable-protocol=file,pipe","--enable-demuxer=flac,mp3,wav,aiff,mov,ogg,ape,wv,asf,dsf,dsdiff,image2,image2pipe,jpeg_pipe,png_pipe","--enable-muxer=null,image2,image2pipe,flac,wav,aiff,pcm_s16le,pcm_f32le,pcm_f64le","--enable-decoder=flac,mp3,mp3float,aac,aac_fixed,aac_latm,alac,vorbis,opus,ape,wavpack,wmalossless,wmapro,wmav1,wmav2,pcm_s8,pcm_u8,pcm_s16le,pcm_s16be,pcm_s24le,pcm_s24be,pcm_s32le,pcm_s32be,pcm_f32le,pcm_f32be,pcm_f64le,pcm_f64be,pcm_s64le,pcm_s64be,dsd_lsbf,dsd_msbf,dsd_lsbf_planar,dsd_msbf_planar,png,mjpeg,bmp","--enable-encoder=flac,pcm_s16le,pcm_s24le,pcm_s32le,pcm_f32le,pcm_f64le,png,mjpeg","--enable-parser=aac,aac_latm,flac,mpegaudio,opus,vorbis,png,mjpeg","--enable-bsf=aac_adtstoasc","--enable-indev=lavfi","--enable-filter=abuffer,abuffersink,aresample,aformat,anull,astats,volume,biquad,equalizer,bass,treble,highpass,lowpass,pan,channelsplit,amerge,asplit,alimiter,asetpts,atempo,atrim,asetnsamples,sine,anullsrc,buffer,buffersink,scale,crop,pad,format,split,transpose,color,setsar,null,trim,setpts,concat,amix"]
 if not (b/"config.h").exists():run(args,cwd=b)
 (ROOT/"FFMPEG-CONFIGURE.json").write_text(json.dumps([str(x) for x in args],indent=2)+"\n")
 run(["make","-j8"],cwd=b);run(["make","install"],cwd=b)

def build_freetype():
 if (PREFIX/"lib/libfreetype.a").exists():return
 b=BUILD/"freetype";b.mkdir(exist_ok=True)
 run([SRC/"freetype/configure","--host=x86_64-w64-mingw32",f"--prefix={PREFIX}","--enable-static","--disable-shared","--without-zlib","--without-bzip2","--without-png","--without-brotli","--without-harfbuzz"],cwd=b)
 run(["make","-j8"],cwd=b);run(["make","install"],cwd=b)

def patch_mpv():
 # The existing companion always explicitly disables scripting. A source build
 # without Lua/JS normally removes those options entirely. Accept only "no";
 # no scripting engine, enabled mode or compatibility approximation is added.
 changes={
 "options/options.h":("    char **reset_options;","    char **reset_options;\n    int xm5_disabled_script_option;"),
 "options/options.c":('    {"reset-on-next-file", OPT_STRINGLIST(reset_options)},', '''    {"reset-on-next-file", OPT_STRINGLIST(reset_options)},
#if !(HAVE_LUA || HAVE_JAVASCRIPT || HAVE_CPLUGINS)
    {"load-scripts", OPT_CHOICE(xm5_disabled_script_option, {"no", 0})},
#endif
#if !HAVE_LUA
    {"osc", OPT_CHOICE(xm5_disabled_script_option, {"no", 0})},
    {"ytdl", OPT_CHOICE(xm5_disabled_script_option, {"no", 0})},
#endif'''),
 "MPV_VERSION":("0.42.0-UNKNOWN","0.42.0-xm5-min-g902818753"),
 }
 diffs=[]
 for rel,(before,after) in changes.items():
  p=SRC/"mpv"/rel;text=p.read_text()
  original=text.replace(after,before) if after in text else text
  if after not in text:
   if text.count(before)!=1:raise ValueError("Unexpected pinned mpv patch context: "+rel)
   text=text.replace(before,after,1);p.write_text(text)
  diffs.extend(difflib.unified_diff(original.splitlines(True),text.splitlines(True),fromfile="a/"+rel,tofile="b/"+rel))
 (ROOT/"mpv-minimal-compatibility.patch").write_text("".join(diffs))

def main():
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument("--phase",choices=["all","ffmpeg","fonts","placebo","mpv"],default="all");a=ap.parse_args()
 lock=json.loads((ROOT/"SOURCES.json").read_text())
 for row in lock.values():
  p=ROOT/"sources"/row["file"]
  with p.open("rb") as f:actual=hashlib.file_digest(f,"sha256").hexdigest()
  if actual!=row["sha256"]:raise ValueError(f"Source checksum mismatch: {p.name}")
 (ROOT/"cross.ini").write_text(f"""[binaries]
c = '{CROSS}gcc'
cpp = '{CROSS}g++'
ar = '{CROSS}ar'
strip = '{CROSS}strip'
windres = '{CROSS}windres'
dlltool = '{CROSS}dlltool'
nasm = 'nasm'
pkg-config = 'pkg-config'
[host_machine]
system = 'windows'
cpu_family = 'x86_64'
cpu = 'x86_64'
endian = 'little'
[properties]
needs_exe_wrapper = true
pkg_config_libdir = ['{PREFIX}/lib/pkgconfig', '{PREFIX}/share/pkgconfig']
[built-in options]
c_args = ['-O2', '-g0', '-I{PREFIX}/include']
cpp_args = ['-O2', '-g0', '-I{PREFIX}/include']
c_link_args = ['-static', '-static-libgcc']
cpp_link_args = ['-static', '-static-libgcc', '-static-libstdc++']
default_library = 'static'
prefer_static = true
""")
 if a.phase in ("all","ffmpeg"):build_zlib();build_ffmpeg()
 if a.phase in ("all","fonts"):
  build_freetype()
  meson("fribidi","-Ddocs=false","-Dbin=false","-Dtests=false")
  meson("harfbuzz","-Dtests=disabled","-Dutilities=disabled","-Ddocs=disabled","-Dintrospection=disabled","-Dfreetype=disabled","-Dglib=disabled","-Dgobject=disabled","-Dcairo=disabled","-Dicu=disabled")
  meson("libass","-Ddirectwrite=enabled","-Dfontconfig=disabled","-Dcoretext=disabled","-Dasm=disabled")
 if a.phase in ("all","placebo"):
  # Only API declarations used by libplacebo's disabled-backend stubs; there
  # is no Vulkan loader or GPU runtime in this package.
  shutil.copytree(SRC/"Vulkan-Headers/include",PREFIX/"include",dirs_exist_ok=True)
  meson("libplacebo","-Ddemos=false","-Dtests=false","-Dbench=false","-Dvulkan=disabled","-Dopengl=disabled","-Dd3d11=disabled","-Dglslang=disabled","-Dshaderc=disabled","-Dlcms=disabled","-Ddovi=disabled","-Dlibdovi=disabled","-Dxxhash=disabled","-Dunwind=disabled")
 if a.phase in ("all","mpv"):
  patch_mpv()
  # The mandatory static libplacebo contains C++ float conversion code, while
  # mpv's no-GPU build links with the C driver. Preserve that standard runtime
  # dependency in pkg-config metadata so it stays inside the static link group.
  pc=PREFIX/"lib/pkgconfig/libplacebo.pc"
  text=pc.read_text()
  if "-lstdc++" not in text:pc.write_text(text+"\nLibs.private: -lstdc++\n")
  meson("mpv","-Dgpl=false","-Dlibmpv=false","-Dcplayer=true","-Dbuild-date=false","-Dlua=disabled","-Dgl=disabled","-Dwasapi=enabled","-Dwin32-threads=enabled","-Dwin32-subsystem=console")
 print("Completed phase:",a.phase)

if __name__=="__main__":main()
