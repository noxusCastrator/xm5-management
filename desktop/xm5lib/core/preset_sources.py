"""Read-only conversion of selected original PEQ source folders.

Sources are never rewritten. Every unsupported file remains an original asset;
only strictly representable presets receive separate player working copies.
"""
from pathlib import Path
import hashlib
import os
import re
from . import peace, peq

MAX_PRESET_BYTES = 1024 * 1024

def collect_presets(roots, source_ids=None, cancel=None):
    presets, metadata, warnings = {}, {}, []
    source_ids = source_ids or {}
    for folder in roots:
        root = Path(folder).resolve()
        sid = source_ids.get(str(root)) or source_ids.get(os.path.normcase(str(root))) or hashlib.sha256(str(root).encode()).hexdigest()[:24]
        for directory, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not (Path(directory)/d).is_symlink() and not getattr((Path(directory)/d).lstat(), 'st_file_attributes', 0)&0x400)
            for name in sorted(names):
                if cancel is not None and cancel.is_set():
                    raise InterruptedError('Preset conversion cancelled')
                path = Path(directory)/name
                if path.suffix.lower() not in ('.peace', '.txt', '.apo'):
                    continue
                relative = path.relative_to(root).as_posix()
                try:
                    if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0)&0x400:
                        raise ValueError('Preset source cannot be a link')
                    if path.stat().st_size > MAX_PRESET_BYTES:
                        raise ValueError('Preset exceeds the 1MiB conversion limit')
                    data = path.read_bytes()
                    before_hash = hashlib.sha256(data).hexdigest()
                    if path.suffix.lower() == '.peace':
                        result = peace.read_profile(path)
                        if not result.ok:
                            raise ValueError('; '.join(result.reasons))
                        preset, notes = result.preset, result.notes
                    else:
                        preset, notes = peq.parse_apo_text(data.decode('utf-8-sig'), path.stem)
                        if notes:
                            raise ValueError('; '.join(notes))
                    errors = peq.validate(preset)
                    if errors:
                        raise ValueError('; '.join(errors))
                    if path.read_bytes() != data:
                        raise ValueError('Preset changed during conversion')
                    slug = re.sub(r'[^A-Za-z0-9_-]+', '-', path.stem).strip('-')[:34] or 'Preset'
                    key = slug+'-'+hashlib.sha256((sid+'\0'+relative).encode()).hexdigest()[:12]
                    presets[key] = peq.to_apo_text(preset)
                    metadata[key] = {'label': path.stem, 'source_id': sid, 'relative_path': relative,
                                     'source_sha256': before_hash, 'format': path.suffix.lower()[1:],
                                     'notes': notes}
                except (OSError, ValueError, UnicodeError) as exc:
                    warnings.append({'code': 'preset_not_converted', 'source_id': sid, 'relative_path': relative,
                                     'message': str(exc), 'original_preserved': True})
    return {'presets': presets, 'metadata': metadata, 'warnings': warnings}
