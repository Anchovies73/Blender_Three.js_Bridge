import bpy
import json
import os
from .constants import FILMS_TEXT_NAME

# Кеш анимаций (внутренние + внешние)
FILMS_CACHE = {}
FILMS_CACHE_DIRTY = True


def ensure_films_text(create_if_missing=True):
    try:
        txt = bpy.data.texts.get(FILMS_TEXT_NAME)
        if not txt and create_if_missing:
            txt = bpy.data.texts.new(FILMS_TEXT_NAME)
            txt.clear()
            txt.write(json.dumps({"animations": {}}, ensure_ascii=False, indent=2))
        return txt
    except Exception:
        return None


def read_internal_films():
    txt = ensure_films_text(create_if_missing=False)
    if not txt:
        return {}
    try:
        data = json.loads(txt.as_string())
        return data.get("animations", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}


def read_all_films():
    """Read ALL animations (internal + external) without cache."""
    internal = read_internal_films()
    external = read_external_films()
    merged = dict(internal)
    merged.update(external)
    return merged


def write_internal_films(d):
    """
    Write internal library (Text datablock) and refresh cache as 'clean'.
    """
    global FILMS_CACHE, FILMS_CACHE_DIRTY
    txt = ensure_films_text(create_if_missing=True)
    if not txt:
        raise RuntimeError("Не удалось получить текст-блок для анимаций.")
    txt.clear()
    txt.write(json.dumps({"animations": d}, ensure_ascii=False, indent=2))

    # Keep cache in sync (including external overrides)
    FILMS_CACHE = dict(read_all_films())
    FILMS_CACHE_DIRTY = False


def _get_addon_package_name():
    # procedural_films.storage -> procedural_films -> addon root package
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
    if prefs and getattr(prefs, "external_animations_folder", ""):
        return bpy.path.abspath(prefs.external_animations_folder)
    return None


def clear_external_folder_pref():
    """
    Очистить настройку папки (не трогая файлы на диске).
    """
    addon = _get_addon_package_name()
    try:
        prefs = bpy.context.preferences.addons.get(addon).preferences
    except Exception:
        prefs = None
    if prefs is None:
        return False
    try:
        prefs.external_animations_folder = ""
        return True
    except Exception:
        return False


def list_external_animation_names():
    """
    Быстро получить имена json-файлов в папке (без чтения json).
    Используется для UI-диагностики/синхронизации.
    """
    folder = get_external_folder()
    if not folder or not os.path.isdir(folder):
        return []

    names = []
    try:
        for fname in os.listdir(folder):
            if not fname.lower().endswith(".json"):
                continue
            base = os.path.splitext(fname)[0]
            # three_<name>.json не считаем библиотекой
            if base.startswith("three_"):
                continue
            names.append(base)
    except Exception:
        return []

    names.sort()
    return names


def write_animation_to_file(name, entry):
    global FILMS_CACHE_DIRTY
    folder = get_external_folder()
    if not folder:
        return False
    try:
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{name}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({name: entry}, f, ensure_ascii=False, indent=2)
        FILMS_CACHE_DIRTY = True
        return True
    except Exception:
        return False


def remove_animation_file(name):
    global FILMS_CACHE_DIRTY
    folder = get_external_folder()
    if not folder:
        return False
    path = os.path.join(folder, f"{name}.json")
    try:
        if os.path.isfile(path):
            os.remove(path)
            FILMS_CACHE_DIRTY = True
            return True
    except Exception:
        pass
    return False


def read_external_films():
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

        # Supported formats:
        # 1) {"animations": {...}}
        # 2) {"AnimName": {...}, ...} where {...} contains "tracks"
        if "animations" in data and isinstance(data["animations"], dict):
            res.update(data["animations"])
        else:
            for k, v in data.items():
                if isinstance(v, dict) and "tracks" in v:
                    res[k] = v

    return res


def read_all_films_cached():
    """
    Return animations using cache. Disk is read only when FILMS_CACHE_DIRTY == True.
    """
    global FILMS_CACHE, FILMS_CACHE_DIRTY
    if FILMS_CACHE_DIRTY:
        FILMS_CACHE = read_all_films()
        FILMS_CACHE_DIRTY = False
    return FILMS_CACHE


def mark_cache_dirty():
    global FILMS_CACHE_DIRTY
    FILMS_CACHE_DIRTY = True