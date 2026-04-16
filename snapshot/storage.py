import bpy
import json
import os

from .constants import SNAPSHOT_TEXT_NAME

SNAPSHOT_CACHE = {}
SNAPSHOT_CACHE_DIRTY = True


def ensure_snapshot_text(create_if_missing=True):
    try:
        txt = bpy.data.texts.get(SNAPSHOT_TEXT_NAME)
        if not txt and create_if_missing:
            txt = bpy.data.texts.new(SNAPSHOT_TEXT_NAME)
            txt.clear()
            txt.write(json.dumps({"snapshots": {}}, ensure_ascii=False, indent=2))
        return txt
    except Exception:
        return None


def read_internal_snapshots():
    txt = ensure_snapshot_text(create_if_missing=False)
    if not txt:
        return {}
    try:
        data = json.loads(txt.as_string())
        return data.get("snapshots", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_internal_snapshots(d):
    global SNAPSHOT_CACHE, SNAPSHOT_CACHE_DIRTY
    txt = ensure_snapshot_text(create_if_missing=True)
    if not txt:
        raise RuntimeError("Не удалось получить текст-блок для снимков.")
    txt.clear()
    txt.write(json.dumps({"snapshots": d}, ensure_ascii=False, indent=2))
    SNAPSHOT_CACHE = dict(read_all_snapshots())
    SNAPSHOT_CACHE_DIRTY = False


def _get_addon_package_name():
    try:
        return __name__.split('.')[0]
    except Exception:
        return ""


def get_external_folder():
    addon = _get_addon_package_name()
    try:
        prefs = bpy.context.preferences.addons.get(addon).preferences
    except Exception:
        prefs = None
    if prefs and getattr(prefs, "external_snapshots_folder", ""):
        return bpy.path.abspath(prefs.external_snapshots_folder)
    return None


def clear_external_folder_pref():
    addon = _get_addon_package_name()
    try:
        prefs = bpy.context.preferences.addons.get(addon).preferences
    except Exception:
        prefs = None
    if prefs is None:
        return False
    try:
        prefs.external_snapshots_folder = ""
        return True
    except Exception:
        return False


def write_snapshot_to_file(name, snapshot_entry):
    global SNAPSHOT_CACHE_DIRTY
    folder = get_external_folder()
    if not folder:
        return False
    try:
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{name}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({name: snapshot_entry}, f, ensure_ascii=False, indent=2)
        SNAPSHOT_CACHE_DIRTY = True
        return True
    except Exception:
        return False


def remove_snapshot_file(name):
    global SNAPSHOT_CACHE_DIRTY
    folder = get_external_folder()
    if not folder:
        return False
    path = os.path.join(folder, f"{name}.json")
    try:
        if os.path.isfile(path):
            os.remove(path)
            SNAPSHOT_CACHE_DIRTY = True
            return True
    except Exception:
        pass
    return False


def read_external_snapshots():
    folder = get_external_folder()
    res = {}
    if not folder or not os.path.isdir(folder):
        return res

    for fname in os.listdir(folder):
        if not fname.lower().endswith(".json"):
            continue
        path = os.path.join(folder, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue

        if not isinstance(data, dict):
            continue

        if "snapshots" in data and isinstance(data["snapshots"], dict):
            for k, v in data["snapshots"].items():
                if isinstance(v, dict) and "objects" in v:
                    res[k] = v
            continue

        for k, v in data.items():
            if isinstance(v, dict) and "objects" in v:
                res[k] = v

    return res


def read_all_snapshots():
    internal = read_internal_snapshots()
    external = read_external_snapshots()
    merged = dict(internal)
    merged.update(external)
    return merged


def get_cached_snapshots():
    global SNAPSHOT_CACHE, SNAPSHOT_CACHE_DIRTY
    if SNAPSHOT_CACHE_DIRTY:
        SNAPSHOT_CACHE = dict(read_all_snapshots())
        SNAPSHOT_CACHE_DIRTY = False
    return SNAPSHOT_CACHE


def mark_cache_dirty():
    global SNAPSHOT_CACHE_DIRTY
    SNAPSHOT_CACHE_DIRTY = True
