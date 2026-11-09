"""Ordered action groups over exact captured native tags, pictures and inputs."""
import base64
import copy
import hashlib
from pathlib import Path

from .. import scripting
from ..core import script_tags
from ..core.restore_card import no_links
from ..scripting.actions import decode_document
from ..scripting.converters import safe_relative_path
from ..scripting import pictures
from .script_actions import _fail, _hash, _version, _audio_identity

RICH_ACTIONS = {"adjust_cover", "import_cover", "export_cover", "set_cover_properties", "import_text", "export", "generate_playlists"}


def plan_group(owner, group, records, payload, context, arguments):
    if any(row.blocking for row in group.diagnostics):
        raise scripting.ScriptError("ACTION_BLOCKED", "; ".join(row.message for row in group.diagnostics if row.blocking))
    current=copy.deepcopy(records)
    original={row["track_id"]:copy.deepcopy(row["fields"]) for row in current}
    original_pictures={row["track_id"]:script_tags.pictures(Path(row["path"])) for row in current}
    present_pictures=copy.deepcopy(original_pictures)
    removed_native={row["track_id"]:set() for row in current}
    payload.update(files=[],playlists=[],inputs=[])
    used_paths=set()
    def evaluate(text,row): return scripting.evaluate(str(text),row["fields"],row["technical"],captured=context)
    def load(expression,row,maximum):
        value=Path(evaluate(expression,row))
        if not value.is_absolute(): value=Path(row["path"]).parent/value
        value=owner.engine._under(value,owner.engine.import_roots+owner.engine.music_roots)
        before=_version(value)
        if before["size"]>maximum: _fail("LIMIT","Action input exceeds its bounded size",path=str(value))
        data=value.read_bytes()
        if _version(value)!=before: _fail("SOURCE_CHANGED","Action input changed during capture")
        observed={"path":str(value),"version":before,"sha256":hashlib.sha256(data).hexdigest()}
        old=next((item for item in payload["inputs"] if item["path"]==str(value)),None)
        if old is not None and old!=observed: _fail("SOURCE_CHANGED","Repeated action input changed")
        if old is None: payload["inputs"].append(observed)
        return data,value.name
    def output(relative,data):
        root=owner.engine._under(arguments["output_directory"],owner.engine.export_roots)
        if not root.is_dir(): _fail("OUTPUT_SCOPE","Choose an existing allowed output directory")
        path=no_links(root/safe_relative_path(relative))
        if path.exists() or str(path).casefold() in used_paths: _fail("OUTPUT_EXISTS","Action output already exists or collides",path=str(path))
        used_paths.add(str(path).casefold())
        payload["files"].append({"path":str(path),"data_base64":base64.b64encode(data).decode(),"sha256":hashlib.sha256(data).hexdigest()})
    for action in group.actions:
        options=action.options
        if action.kind not in RICH_ACTIONS:
            if action.kind in {"remove","remove_except"}:
                fields=options.get("fields",[])
                for row in current:
                    removed_native[row["track_id"]].update(script_tags.native_removals(Path(row["path"]),fields,except_fields=action.kind=="remove_except"))
                    if ((bool({str(field).upper() for field in fields}&{"COVER","PICTURE"})) == (action.kind=="remove")):
                        present_pictures[row["track_id"]]=[]
                options={**options,"fields":[field for field in fields if str(field).upper() not in {"COVER","PICTURE"}]}
                action=scripting.Action(action.kind,options)
            plan=scripting.plan_actions(scripting.ActionGroup(group.name,(action,)),owner._tracks(current),captured=context)
            for operation in plan.operations:
                if operation.kind!="set_tags": _fail("SOURCE_ACTION_UNSUPPORTED","Use the exact rename converter for filename actions")
                row=next(row for row in current if row["track_id"]==operation.track_id)
                for field in operation.values["remove"]: row["fields"].pop(field,None)
                row["fields"].update(operation.values["set"])
            continue
        if action.kind=="export":
            raw,name=load(options["template_file"],current[0],4_000_000) if "template_file" in options else (options["text"].encode("utf-8"),"inline.mte")
            plan=scripting.plan_export(scripting.import_mte(raw,name,options.get("encoding")),owner._tracks(current),default_name=options.get("default_name","export.txt"),
                                        partition=options.get("partition"),bom=options.get("bom",False),ansi_encoding=options.get("ansi_encoding"),captured=context)
            for item in plan.files: output(item.relative_path,item.data)
            continue
        if action.kind=="generate_playlists":
            recipe=dict(options["recipe"])
            for key in ("all_of","any_of"): recipe[key]=tuple(scripting.Predicate(**item) for item in recipe.get(key,[]))
            plan=scripting.plan_playlists(scripting.PlaylistRecipe(**recipe),owner._tracks(current),captured=context)
            existing={item["name"]:item for item in owner.engine.application.query("playlists.list",{})["playlists"]}
            for item in plan.playlists:
                if any(row["name"]==item.name for row in payload["playlists"]): _fail("PLAYLIST_NAME_COLLISION","Action group generates a playlist name more than once")
                payload["playlists"].append({"name":item.name,"track_ids":[entry.track_id for entry in item.occurrences],"target":existing.get(item.name)})
            continue
        for row in current:
            identity=row["track_id"]
            if action.kind=="import_text":
                raw,name=load(options["path"],row,4_000_000)
                _,text=decode_document(raw,name,options.get("encoding"))
                row["fields"][script_tags.canonical(options["field"])]=[text] if text else []
            elif action.kind=="export_cover":
                values=present_pictures[identity]
                indexes=options.get("indexes",list(range(len(values))))
                for index in indexes:
                    if type(index) is not int or not 0<=index<len(values): _fail("COVER_SELECTION","Select an actual captured picture index")
                    picture=values[index]
                    extension={"image/png":"png","image/jpeg":"jpg","image/gif":"gif","image/bmp":"bmp","image/webp":"webp"}.get(picture["mime"])
                    if not extension: _fail("COVER_FORMAT","Picture MIME has no verified extension")
                    name=evaluate(options.get("pattern","%_FILENAME%"),row)+"-"+str(index)+"."+extension
                    output(name,base64.b64decode(picture["data_base64"],validate=True))
            else:
                values=dict(options)
                values["operation"]={"import_cover":"import","adjust_cover":"adjust","set_cover_properties":"properties"}[action.kind]
                imported=load(options["path"],row,16*1024*1024)[0] if action.kind=="import_cover" else None
                present_pictures[identity]=pictures.transform(present_pictures[identity],values,imported)
    writes=[]
    for row in current:
        identity=row["track_id"];fields=row["fields"]
        changes={script_tags.canonical(field):list(values) for field,values in fields.items() if values!=original[identity].get(field)}
        changes.update({script_tags.canonical(field):[] for field in original[identity] if field not in fields})
        present_pictures[identity]=script_tags.prepare_pictures(Path(row["path"]),present_pictures[identity])
        changed_pictures=present_pictures[identity]!=original_pictures[identity]
        absent_native=set(script_tags.native_removals(Path(row["path"]),list(fields)+(["COVER"] if present_pictures[identity] else []),except_fields=True))
        native_delete=sorted(removed_native[identity]&absent_native)
        if not changes and not changed_pictures and not native_delete: continue
        path=Path(row["path"])
        write={"track_id":identity,"path":str(path),"changes":changes,"audio_identity":_audio_identity(path,pictures_changed=changed_pictures),
               "untouched":script_tags.untouched(path,changes,pictures_changed=changed_pictures,native_delete=native_delete),"native_delete":native_delete}
        if changed_pictures: write["pictures"]=present_pictures[identity]
        writes.append(write)
    return writes
