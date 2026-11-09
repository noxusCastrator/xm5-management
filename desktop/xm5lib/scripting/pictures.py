"""Bounded captured-picture edits; no filesystem operations."""
import base64
import copy
import hashlib
import io

from PIL import Image, ImageOps

from .formatter import ScriptError


def picture(raw, kind=3, description=""):
    if len(raw)>16*1024*1024: raise ScriptError("COVER_LIMIT","Picture exceeds sixteen MiB")
    with Image.open(io.BytesIO(raw)) as value:
        if value.width*value.height>40_000_000: raise ScriptError("COVER_LIMIT","Picture exceeds forty million pixels")
        mime=Image.MIME.get(value.format)
        if not mime: raise ScriptError("COVER_FORMAT","Picture format has no known MIME type")
        value.verify()
    if type(kind) is not int or not 0<=kind<=20 or not isinstance(description,str) or len(description)>1024 or "\0" in description:
        raise ScriptError("COVER_ARGUMENT","Invalid picture type/description")
    return {"native":{},"type":kind,"mime":mime,"description":description,"data_base64":base64.b64encode(raw).decode("ascii"),"sha256":hashlib.sha256(raw).hexdigest()}


def transform(existing, options, imported=None):
    rows=copy.deepcopy(existing);operation=options["operation"]
    indexes=options.get("indexes",[next((i for i,row in enumerate(rows) if row["type"]==3),0)] if rows else [])
    if not isinstance(indexes,list) or len(indexes)!=len(set(indexes)) or any(type(i) is not int or not 0<=i<len(rows) for i in indexes):
        raise ScriptError("COVER_SELECTION","Picture indexes must select actual unique captured pictures")
    if operation=="import":
        if imported is None: raise ScriptError("COVER_INPUT","Import needs exact captured picture bytes")
        new=picture(imported,options.get("type",3),options.get("description",""))
        if options.get("remove_existing",False): rows=[]
        elif options.get("replace_type",True): rows=[row for row in rows if row["type"]!=new["type"]]
        rows.append(new)
    elif operation=="remove":
        rows=[row for i,row in enumerate(rows) if i not in indexes]
    elif operation=="properties":
        for i in indexes:
            old=rows[i]
            rows[i]=picture(base64.b64decode(old["data_base64"],validate=True),options.get("type",old["type"]),options.get("description",old["description"]))
            rows[i]["native"]=old.get("native",{})
    elif operation=="adjust":
        maximum=options.get("max_size",1200);quality=options.get("quality",90);encoding=options.get("format","keep")
        if type(maximum) is not int or not 1<=maximum<=10000 or type(quality) is not int or not 1<=quality<=100 or encoding not in {"keep","png","jpeg"}:
            raise ScriptError("COVER_ARGUMENT","Invalid cover maximum/quality/format")
        for i in indexes:
            row=rows[i];raw=base64.b64decode(row["data_base64"],validate=True)
            with Image.open(io.BytesIO(raw)) as value:
                if encoding=="keep" and row["mime"] not in {"image/png","image/jpeg"}:
                    raise ScriptError("COVER_FORMAT","Keeping this cover format is not supported; choose explicit png/jpeg")
                if getattr(value,"n_frames",1)>1:
                    raise ScriptError("COVER_FORMAT","Animated/multipage cover adjustment needs an explicit frame policy")
                if value.width*value.height>40_000_000: raise ScriptError("COVER_LIMIT","Picture exceeds forty million pixels")
                target=("png" if row["mime"]=="image/png" else "jpeg") if encoding=="keep" else encoding
                value=ImageOps.exif_transpose(value).convert("RGBA")
                value.thumbnail((maximum,maximum),Image.Resampling.LANCZOS)
                if target=="jpeg":
                    canvas=Image.new("RGB",value.size,"black");canvas.paste(value,mask=value.getchannel("A"));value=canvas
                stream=io.BytesIO();value.save(stream,"PNG" if target=="png" else "JPEG",**({} if target=="png" else {"quality":quality}))
            rows[i]=picture(stream.getvalue(),row["type"],row["description"])
    else:
        raise ScriptError("COVER_OPERATION","Unknown cover operation")
    return rows
