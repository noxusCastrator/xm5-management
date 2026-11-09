"""Run actual new Windows binaries through the existing application controller.

Playback tests always select null output; hardware audio is never opened.
"""
from __future__ import annotations
import argparse
from array import array
import hashlib
import json
import math
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import time
import wave

HERE=Path(__file__).resolve().parent

def run(args,**kwargs):
    return subprocess.run([str(a) for a in args],capture_output=True,check=True,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0),**kwargs)

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mpv",type=Path,required=True)
    ap.add_argument("--ffmpeg",type=Path,required=True)
    ap.add_argument("--app",type=Path,required=True)
    ap.add_argument("--fixture-ffmpeg",type=Path,help="Existing local encoder, used only to generate tiny input fixtures")
    ap.add_argument("--fixtures",type=Path,default=HERE/"test-inputs",help="Prepared synthetic audio fixtures; no external encoder needed")
    ap.add_argument("--output",type=Path,required=True)
    a=ap.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=True)
    sys.path.insert(0,str(a.app.resolve()))
    from xm5lib.core.mpv_session import Session
    from xm5lib.core import peq,desktop_dsp
    result={"mpv_version":run([a.mpv,"--version"]).stdout.decode("utf-8","replace"),"ffmpeg_version":run([a.ffmpeg,"-version"]).stdout.decode("utf-8","replace"),"audio_output":"null only","checks":[]}
    ff_license=run([a.ffmpeg,"-L"]).stdout.decode("utf-8","replace")
    assert "Lesser" in ff_license,ff_license
    result["checks"].append("FFmpeg executable license output identifies LGPL2.1+;mpv GPL-off build config verified separately")
    fixtures=out/"fixtures";fixtures.mkdir(exist_ok=True)
    if a.fixtures.is_dir():shutil.copytree(a.fixtures,fixtures,dirs_exist_ok=True)
    for rate in (44100,192000):
        wav=fixtures/f"tone-{rate}.wav"
        samples=array("h",(round(12000*math.sin(2*math.pi*1000*i/rate)) for i in range(rate*2) for _ in range(2)))
        with wave.open(str(wav),"wb") as f:f.setparams((2,2,rate,0,"NONE","none"));f.writeframes(samples.tobytes())
    original=fixtures/"tone-44100.wav"
    encoder=a.fixture_ffmpeg or a.ffmpeg
    for suffix,codec in (("flac","flac"),("mp3","libmp3lame"),("aiff","pcm_s16be"),("m4a","alac")):
        path=fixtures/("tone."+suffix)
        if not path.exists():run([encoder,"-v","error","-i",original,"-c:a",codec,"-y",path])
    result["checks"].append("generated local PCM/FLAC/MP3/AIFF/ALAC M4A inputs")
    for path in [original,*[fixtures/("tone."+x) for x in ("flac","mp3","aiff","m4a")]]:
        decoded=run([a.ffmpeg,"-v","error","-i",path,"-f","s16le","-"]).stdout
        assert len(decoded)>=44100*4, (path,len(decoded))
    result["checks"].append("real FFmpeg decoding of five supported container types")
    unicode_track=fixtures/"Album Space \u4e13\u8f91"/"\u6d4b\u8bd5.flac";unicode_track.parent.mkdir(exist_ok=True)
    shutil.copyfile(fixtures/"tone.flac",unicode_track)
    assert run([a.ffmpeg,"-v","error","-i",unicode_track,"-f","s16le","-"]).stdout
    result["checks"].append("Unicode and spaced Windows input paths")
    measured=run([a.ffmpeg,"-hide_banner","-i",original,"-af","aformat=sample_fmts=dbl,astats=measure_perchannel=none:measure_overall=Peak_level+Number_of_NaNs+Number_of_Infs","-f","null","-"])
    log=measured.stderr.decode("utf-8","replace");assert "Peak level dB:" in log,log
    result["checks"].append("actual export peak/NaN/Inf analysis filter")
    # Fixed generated PNG and JPEG processing, independent of user artwork.
    from PIL import Image
    Image.new("RGB",(512,300),(20,80,130)).save(fixtures/"cover.png")
    for side in (80,160,330):
        target=fixtures/f"cover-{side}.jpg"
        run([a.ffmpeg,"-v","error","-i",fixtures/"cover.png","-vf",f"scale={side}:{side}","-frames:v","1","-threads","1","-c:v","mjpeg","-y",target])
        with Image.open(target) as image:assert image.size==(side,side)
    result["checks"].append("PNG decode, scale and baseline JPEG encode at80/160/330")
    preset=peq.Preset("runtime-test",-6,[peq.Band("PK",1000,3,1,True,"ALL"),peq.Band("LSC",100,-2,0.7,True,"L"),peq.Band("HSC",8000,1,0.7,True,"R")])
    for rate in (44100,192000):
        graph=desktop_dsp.compile_preset(preset,rate).graph
        filtered=run([a.ffmpeg,"-v","error","-i",fixtures/f"tone-{rate}.wav","-af",graph,"-f","f64le","-"]).stdout
        assert len(filtered)==rate*2*2*8,(rate,len(filtered))
    result["checks"].append("actual f64 per-channel RBJ PEQ graph at44.1/192kHz")
    ao=run([a.mpv,"--ao=help"]).stdout.decode("utf-8","replace")
    assert "wasapi" in ao,ao
    result["checks"].append("WASAPI output compiled and advertised")
    for rate in (44100,192000):
        session=Session(a.mpv,test_null=True,timeout=5)
        try:
            session.command("peq.set",{"preset":preset})
            session.play_tracks([str(fixtures/f"tone-{rate}.wav")])
            deadline=time.monotonic()+4
            while time.monotonic()<deadline:
                state=session.status()
                if state.get("bridge_error"):raise AssertionError(state)
                reply=session._raw(["get_property","audio-params/samplerate"])
                if reply.get("data")==rate and not state.get("awaiting_format"):break
                time.sleep(.025)
            else:raise AssertionError(session.status())
            session.command("volume.set",{"value":37})
            assert abs(session.command("volume.get")["value"]-37)<.01
            session.command("pause")
            session.command("seek",{"position_ms":500})
            filters=session._raw(["get_property","af"])
            assert filters.get("error")=="success" and filters.get("data"),filters
            inventory=session.output_inventory()
            result.setdefault("output_device_count",len(inventory))
            session.command("peq.clear")
            session.command("stop")
        finally:session.close()
        assert session.process.poll() is not None
    result["checks"].append("real Windows Session JSON IPC:confirmed44.1/192k PCM,PEQ,volume,pause,seek,device enumeration,cleanup")
    result["binaries"]={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (a.mpv,a.ffmpeg)}
    result["passed"]=True
    (out/"WINDOWS-RESULT.json").write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(result,indent=2))

if __name__=="__main__":main()
