import bpy
import os
import time
import json
from datetime import datetime
from bpy.props import (
    StringProperty,
    BoolProperty,
    IntProperty,
    FloatProperty,
    CollectionProperty,
)

from .constants import MODULE_ID, MODULE_NAME, GLTF_ID_PROP
from . import storage as _storage
from .blender_codec import META_KEYS_ORDER
from .ops import (
    create_animation_from_scene,
    update_animation_from_scene,
    apply_animation_to_scene,
    delete_animation,
    set_outline_rule_on_objects,
    clear_outline_rule_on_objects,
    get_object_outline_rules_data,
    get_animation_transform_conflicts,
    rewrite_animation_conflicts_from_snapshot,

    # outline rule CP keys
    OUTLINE_RULE_DEFAULT_DURATION_SEC,
    OUTLINE_RULE_DEFAULT_INTERVAL_SEC,

    # unified materials CP operator + warning
    UMZ_OT_set_material_shaders_cp,
    umz_scene_has_materials_missing_shaders_cp,
    umz_scene_has_materials_missing_mix_factor_cp,
)

# =========================================================
# UI version: per-object ZIP + meta locks + copy + warnings
# + gltf_id warning for MISSING or DUPLICATE ids
# + material shaders button + warning
# =========================================================


class UMZ_AnimationItem(bpy.types.PropertyGroup):
    name: StringProperty(name="name", default="")


class ANIM_UL_umz_list(bpy.types.UIList):
    bl_idname = "ANIM_UL_umz_list"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.label(text=item.name, icon='ACTION')

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        flt_flags = []
        flt_neworder = []

        filter_str = (self.filter_name or "").lower().strip()
        if not filter_str:
            flt_flags = [self.bitflag_filter_item] * len(items)
            return flt_flags, flt_neworder

        for it in items:
            name = (getattr(it, "name", "") or "").lower()
            flt_flags.append(self.bitflag_filter_item if filter_str in name else 0)

        return flt_flags, flt_neworder


def format_created(timestamp):
    try:
        dt = datetime.fromisoformat(timestamp)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(timestamp)


def _on_list_index_changed(self, context):
    sc = self
    try:
        idx = int(sc.umz_anim_list_index)
    except Exception:
        return
    if idx < 0 or idx >= len(sc.umz_anim_list):
        return
    try:
        sc.umz_selected_animation = sc.umz_anim_list[idx].name
    except Exception:
        pass


def _store_transform_conflicts_cache(scene, anim_name, snapshot_name, conflicts):
    try:
        scene.umz_anim_conflicts_json = json.dumps(conflicts or [], ensure_ascii=False)
    except Exception:
        scene.umz_anim_conflicts_json = '[]'

    try:
        scene.umz_anim_conflict_animation_name = str(anim_name or '')
    except Exception:
        scene.umz_anim_conflict_animation_name = ''

    try:
        scene.umz_anim_conflict_snapshot_name = str(snapshot_name or '')
    except Exception:
        scene.umz_anim_conflict_snapshot_name = ''


def _read_transform_conflicts_cache(scene):
    try:
        data = json.loads(getattr(scene, 'umz_anim_conflicts_json', '[]') or '[]')
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def _clear_transform_conflicts_cache(scene):
    _store_transform_conflicts_cache(scene, '', '', [])


def _get_selected_snapshot_name(scene):
    try:
        return str(getattr(scene, 'umz_selected_snapshot', '') or '').strip()
    except Exception:
        return ''


def _is_transform_conflict_cache_current(scene):
    sel_anim = str(getattr(scene, 'umz_selected_animation', '') or '').strip()
    return str(getattr(scene, 'umz_anim_conflict_animation_name', '') or '').strip() == sel_anim


# -------------------------
# Object ZIP (individual)
# -------------------------

_ZIP_ITEMS = (
    ("О", "О", ""),
    ("Г", "Г", ""),
    ("О/Г", "О/Г", ""),
)


def _register_object_zip_prop():
    if hasattr(bpy.types.Object, "umz_zip_choice_obj"):
        return

    def _zip_obj_update(self, context):
        try:
            self["zip"] = self.umz_zip_choice_obj
        except Exception:
            pass

    bpy.types.Object.umz_zip_choice_obj = bpy.props.EnumProperty(
        name="ЗИП",
        items=list(_ZIP_ITEMS),
        default="О",
        update=_zip_obj_update,
    )


def _unregister_object_zip_prop():
    if hasattr(bpy.types.Object, "umz_zip_choice_obj"):
        try:
            delattr(bpy.types.Object, "umz_zip_choice_obj")
        except Exception:
            pass


def register_scene_props():
    if not hasattr(bpy.types.Scene, "umz_anim_list"):
        bpy.types.Scene.umz_anim_list = CollectionProperty(type=UMZ_AnimationItem)

    if not hasattr(bpy.types.Scene, "umz_anim_list_index"):
        bpy.types.Scene.umz_anim_list_index = IntProperty(default=0, update=_on_list_index_changed)

    if not hasattr(bpy.types.Scene, "umz_selected_animation"):
        bpy.types.Scene.umz_selected_animation = StringProperty(name="Анимация", default="")

    if not hasattr(bpy.types.Scene, "umz_anim_full_delete"):
        bpy.types.Scene.umz_anim_full_delete = BoolProperty(name="Полное удаление", default=False)

    if not hasattr(bpy.types.Scene, "umz_export_alpha_tracks"):
        bpy.types.Scene.umz_export_alpha_tracks = BoolProperty(
            name="Экспорт прозрачности (alpha)",
            description="Экспорт alpha_tracks из Object Color (color[3]) или CP ['alpha'] в three_*.json",
            default=True
        )

    if not hasattr(bpy.types.Scene, "umz_anim_visible_selected_only"):
        bpy.types.Scene.umz_anim_visible_selected_only = BoolProperty(
            name="Только выделенные объекты",
            default=False
        )

    if not hasattr(bpy.types.Scene, "umz_text_and_markers"):
        bpy.types.Scene.umz_text_and_markers = BoolProperty(
            name="Текст и метки",
            default=False
        )

    if not hasattr(bpy.types.Scene, "umz_meta_ui_open"):
        bpy.types.Scene.umz_meta_ui_open = BoolProperty(name="Поля объекта", default=True)

    if not hasattr(bpy.types.Scene, "umz_outline_rule_duration_sec"):
        bpy.types.Scene.umz_outline_rule_duration_sec = bpy.props.FloatProperty(
            name="Длительность",
            default=float(OUTLINE_RULE_DEFAULT_DURATION_SEC),
            min=0.001,
        )

    if not hasattr(bpy.types.Scene, "umz_outline_rule_interval_sec"):
        bpy.types.Scene.umz_outline_rule_interval_sec = bpy.props.FloatProperty(
            name="Интервал",
            default=float(OUTLINE_RULE_DEFAULT_INTERVAL_SEC),
            min=0.001,
        )

    if not hasattr(bpy.types.Scene, "umz_ui_animation_library_open"):
        bpy.types.Scene.umz_ui_animation_library_open = BoolProperty(
            name="Библиотека анимаций",
            default=True,
        )

    if not hasattr(bpy.types.Scene, "umz_ui_outline_rules_open"):
        bpy.types.Scene.umz_ui_outline_rules_open = BoolProperty(
            name="Правила контурной обводки",
            default=False,
        )

    if not hasattr(bpy.types.Scene, "umz_ui_object_fields_open"):
        bpy.types.Scene.umz_ui_object_fields_open = BoolProperty(
            name="Поля объекта",
            default=False,
        )

    if not hasattr(bpy.types.Scene, "umz_ui_conflict_tools_open"):
        bpy.types.Scene.umz_ui_conflict_tools_open = BoolProperty(
            name="Исправление конфликтов",
            default=False,
        )

    if not hasattr(bpy.types.Scene, "umz_anim_conflicts_json"):
        bpy.types.Scene.umz_anim_conflicts_json = StringProperty(default="[]")

    if not hasattr(bpy.types.Scene, "umz_anim_conflict_animation_name"):
        bpy.types.Scene.umz_anim_conflict_animation_name = StringProperty(default="")

    if not hasattr(bpy.types.Scene, "umz_anim_conflict_snapshot_name"):
        bpy.types.Scene.umz_anim_conflict_snapshot_name = StringProperty(default="")


def unregister_scene_props():
    for prop in (
        "umz_anim_list_index",
        "umz_anim_list",
        "umz_selected_animation",
        "umz_anim_full_delete",
        "umz_export_alpha_tracks",
        "umz_anim_visible_selected_only",
        "umz_text_and_markers",
        "umz_meta_ui_open",
        "umz_outline_rule_duration_sec",
        "umz_outline_rule_interval_sec",
        "umz_ui_animation_library_open",
        "umz_ui_outline_rules_open",
        "umz_ui_object_fields_open",
        "umz_ui_conflict_tools_open",
        "umz_anim_conflicts_json",
        "umz_anim_conflict_animation_name",
        "umz_anim_conflict_snapshot_name",
    ):
        if hasattr(bpy.types.Scene, prop):
            try:
                delattr(bpy.types.Scene, prop)
            except Exception:
                pass


def _rebuild_list(scene, prefer_name=""):
    films = _storage.read_all_films_cached() or {}
    names = sorted(list(films.keys()))

    prev = prefer_name.strip() if prefer_name else (scene.umz_selected_animation or "")
    try:
        prev_idx = int(scene.umz_anim_list_index)
    except Exception:
        prev_idx = 0

    lst = scene.umz_anim_list
    lst.clear()
    for n in names:
        it = lst.add()
        it.name = n

    if not names:
        scene.umz_anim_list_index = 0
        scene.umz_selected_animation = ""
        return

    if prev and prev in names:
        idx = names.index(prev)
    else:
        idx = max(0, min(prev_idx, len(names) - 1))

    scene.umz_anim_list_index = idx
    scene.umz_selected_animation = names[idx]


# -------------------------
# gltf_id warning (cached): missing + duplicates
# -------------------------

_GLTF_MISSING_CACHE = None
_GLTF_MISSING_CACHE_T = 0.0
_GLTF_DUPES_CACHE = None
_GLTF_DUPES_CACHE_T = 0.0
_GLTF_CACHE_TTL = 0.75


def _scene_has_missing_gltf_id(scene):
    global _GLTF_MISSING_CACHE, _GLTF_MISSING_CACHE_T
    now = time.time()
    if _GLTF_MISSING_CACHE is not None and (now - _GLTF_MISSING_CACHE_T) < _GLTF_CACHE_TTL:
        return _GLTF_MISSING_CACHE

    missing = False
    try:
        for obj in scene.objects:
            try:
                if GLTF_ID_PROP not in obj.keys():
                    missing = True
                    break
            except Exception:
                missing = True
                break
    except Exception:
        missing = False

    _GLTF_MISSING_CACHE = missing
    _GLTF_MISSING_CACHE_T = now
    return missing


def _scene_has_duplicate_gltf_id(scene):
    global _GLTF_DUPES_CACHE, _GLTF_DUPES_CACHE_T
    now = time.time()
    if _GLTF_DUPES_CACHE is not None and (now - _GLTF_DUPES_CACHE_T) < _GLTF_CACHE_TTL:
        return _GLTF_DUPES_CACHE

    seen = set()
    dup = False
    try:
        for obj in scene.objects:
            try:
                if GLTF_ID_PROP not in obj.keys():
                    continue
                v = obj.get(GLTF_ID_PROP)
            except Exception:
                continue

            if not isinstance(v, str):
                continue
            v = v.strip()
            if not v:
                continue

            if v in seen:
                dup = True
                break
            seen.add(v)
    except Exception:
        dup = False

    _GLTF_DUPES_CACHE = dup
    _GLTF_DUPES_CACHE_T = now
    return dup


def _invalidate_gltf_cache():
    global _GLTF_MISSING_CACHE, _GLTF_MISSING_CACHE_T
    global _GLTF_DUPES_CACHE, _GLTF_DUPES_CACHE_T
    _GLTF_MISSING_CACHE = None
    _GLTF_MISSING_CACHE_T = 0.0
    _GLTF_DUPES_CACHE = None
    _GLTF_DUPES_CACHE_T = 0.0


# -------------------------
# Meta helpers (locks + copy)
# -------------------------

_META_LABELS_RU = {
    "position": "Позиция",
    "oboznachenie": "Обозначение",
    "naimenovanie": "Наименование",
    "count_in_animation": "Количество в анимации",
    "count_in_zip": "Количество в ЗИП",
    "zip": "ЗИП",
    "fnn": "ФНН",
    "proizvoditel": "Производитель",
    "link": "Ссылка",
}

_LOCKABLE_META_KEYS = ("oboznachenie", "naimenovanie", "count_in_animation", "count_in_zip")


def _lock_prop_name(field_key: str) -> str:
    return f"_lock_{field_key}"


def _ensure_lock_props(obj):
    for k in _LOCKABLE_META_KEYS:
        lk = _lock_prop_name(k)
        try:
            if lk not in obj.keys():
                obj[lk] = False
        except Exception:
            pass


def _is_locked(obj, field_key: str) -> bool:
    lk = _lock_prop_name(field_key)
    try:
        return bool(obj.get(lk, False))
    except Exception:
        return False


def _split_obj_name(obj_name: str):
    s = (obj_name or "").strip()
    if not s:
        return "", ""
    if " " not in s:
        return s, ""
    left, right = s.split(" ", 1)
    return left.strip(), right.strip()


# -------------------------
# Counts: within collection of the object
# -------------------------

def _split_base_and_numeric_tail(obj_name: str):
    """
    Split by LAST dot, and only treat tail as suffix if it's digits.

    Examples:
      'Cube.1147'      -> ('Cube', '1147')
      'Болт м6.5.001'  -> ('Болт м6.5', '001')
      'Cube'           -> ('Cube', '')
      'Cube.foo'       -> ('Cube.foo', '')
    """
    s = (obj_name or "").strip()
    if not s:
        return "", ""

    if "." not in s:
        return s, ""

    base, tail = s.rsplit(".", 1)
    base = base.strip()
    tail = tail.strip()

    if base and tail.isdigit():
        return base, tail

    return s, ""


def _base_name_for_count(obj_name: str):
    base, _tail = _split_base_and_numeric_tail(obj_name)
    return base


def _get_primary_collection_for_object(obj):
    """
    Returns the first users_collection, or None.
    This defines "which collection to count inside".

    If object is in multiple collections and you want another rule, tell me.
    """
    try:
        cols = list(getattr(obj, "users_collection", []) or [])
    except Exception:
        cols = []
    return cols[0] if cols else None


def _compute_counts_for_collection(col: bpy.types.Collection):
    """
    Count instances by base name inside a collection (including objects in child collections).
    """
    res = {}
    if not col:
        return res

    try:
        objs = list(col.all_objects)
    except Exception:
        try:
            objs = list(col.objects)
        except Exception:
            objs = []

    for o in objs:
        try:
            base = _base_name_for_count(o.name)
        except Exception:
            continue
        if not base:
            continue
        res[base] = res.get(base, 0) + 1

    return res


def _get_target_objects_for_meta_ops(context):
    try:
        sel = list(context.selected_objects) if context.selected_objects else []
    except Exception:
        sel = []
    if sel:
        return sel
    if context.object:
        return [context.object]
    return []


def _sync_object_zip_enum_from_custom_prop(obj):
    try:
        v = str(obj.get("zip", "О"))
    except Exception:
        v = "О"
    if v not in ("О", "Г", "О/Г"):
        v = "О"
    try:
        obj.umz_zip_choice_obj = v
    except Exception:
        pass


# -------------------------
# Operators
# -------------------------

class ANIM_OT_refresh_list(bpy.types.Operator):
    bl_idname = "umz.anim_refresh_list"
    bl_label = "Синхронизировать"

    def execute(self, context):
        _storage.mark_cache_dirty()
        _rebuild_list(context.scene)
        return {'FINISHED'}


class ANIM_OT_set_dir(bpy.types.Operator):
    bl_idname = "umz.anim_set_directory"
    bl_label = "Папка для анимаций"
    filepath: StringProperty(subtype='FILE_PATH', default="")

    def execute(self, context):
        addon = __name__.split('.')[0]
        try:
            prefs = bpy.context.preferences.addons[addon].preferences
            if self.filepath:
                prefs.external_animations_folder = os.path.dirname(self.filepath)
                self.report({'INFO'}, f"Папка анимаций: {prefs.external_animations_folder}")
            else:
                self.report({'WARNING'}, "Путь не задан.")
        except Exception as e:
            self.report({'ERROR'}, f"{e}")
            return {'CANCELLED'}

        _storage.mark_cache_dirty()
        _rebuild_list(context.scene)
        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


class ANIM_OT_clear_dir(bpy.types.Operator):
    bl_idname = "umz.anim_clear_directory"
    bl_label = "Очистить папку"

    def execute(self, context):
        ok = _storage.clear_external_folder_pref()
        if not ok:
            self.report({'ERROR'}, "Не удалось очистить настройку папки.")
            return {'CANCELLED'}
        _storage.mark_cache_dirty()
        _rebuild_list(context.scene)
        return {'FINISHED'}


class ANIM_OT_save_selected(bpy.types.Operator):
    bl_idname = "umz.anim_save_selected"
    bl_label = "Создать/Пересохранить"

    name: StringProperty(name="Имя", default="")
    description: StringProperty(name="Описание", default="")

    def execute(self, context):
        name = (self.name or "").strip()
        if not name:
            self.report({'ERROR'}, "Имя анимации не задано.")
            return {'CANCELLED'}

        only_sel = bool(getattr(context.scene, "umz_anim_visible_selected_only", False))

        internal = _storage.read_internal_films()
        if name in internal:
            update_animation_from_scene(name, only_selected=only_sel, description=self.description)
            self.report({'INFO'}, f"Анимация '{name}' обновлена.")
        else:
            create_animation_from_scene(name, self.description, only_selected=only_sel)
            self.report({'INFO'}, f"Анимация '{name}' создана.")

        _storage.mark_cache_dirty()
        _rebuild_list(context.scene, prefer_name=name)
        return {'FINISHED'}

    def invoke(self, context, event):
        sel = (getattr(context.scene, "umz_selected_animation", "") or "").strip()
        self.name = sel if sel else "animation1"

        try:
            film = (_storage.read_all_films_cached() or {}).get(self.name) or {}
            self.description = film.get("description", "") or ""
        except Exception:
            self.description = ""

        return context.window_manager.invoke_props_dialog(self)


class ANIM_OT_load_selected(bpy.types.Operator):
    bl_idname = "umz.anim_load_selected"
    bl_label = "Загрузить"

    def execute(self, context):
        sel = (getattr(context.scene, "umz_selected_animation", "") or "").strip()
        if not sel:
            self.report({'WARNING'}, "Анимация не выбрана.")
            return {'CANCELLED'}

        try:
            res = apply_animation_to_scene(sel, remove_other_animations=True)
        except Exception as e:
            self.report({'ERROR'}, f"Ошибка при применении анимации: {e}")
            return {'CANCELLED'}

        conflicts = res.get('conflicts') or []
        snapshot_name = res.get('snapshot_name') or _get_selected_snapshot_name(context.scene)
        _store_transform_conflicts_cache(context.scene, sel, snapshot_name, conflicts)

        msg = f"Загружено объектов: {len(res.get('applied', []))}"
        if snapshot_name:
            msg += f"; конфликтов: {len(conflicts)}"
        self.report({'INFO'}, msg)
        _storage.mark_cache_dirty()
        _rebuild_list(context.scene, prefer_name=sel)
        return {'FINISHED'}


class ANIM_OT_delete_selected(bpy.types.Operator):
    bl_idname = "umz.anim_delete_selected"
    bl_label = "Удалить"

    def execute(self, context):
        scene = context.scene
        sel = (getattr(scene, "umz_selected_animation", "") or "").strip()
        if not sel:
            self.report({'WARNING'}, "Анимация не выбрана.")
            return {'CANCELLED'}

        try:
            idx_before = int(scene.umz_anim_list_index)
        except Exception:
            idx_before = 0

        full = getattr(scene, "umz_anim_full_delete", False)
        ok = delete_animation(sel, full_delete=bool(full))
        if not ok:
            self.report({'ERROR'}, "Не найдено.")
            return {'CANCELLED'}

        _storage.mark_cache_dirty()
        _rebuild_list(scene)

        names = [it.name for it in scene.umz_anim_list]
        if not names:
            scene.umz_anim_list_index = 0
            scene.umz_selected_animation = ""
            return {'FINISHED'}

        idx = idx_before
        if idx >= len(names):
            idx = len(names) - 1
        if idx < 0:
            idx = 0
        scene.umz_anim_list_index = idx
        scene.umz_selected_animation = names[idx]
        return {'FINISHED'}


class ANIM_OT_rename_selected(bpy.types.Operator):
    bl_idname = "umz.anim_rename_selected"
    bl_label = "Переименовать"

    new_name: StringProperty(name="Новое имя", default="")

    def execute(self, context):
        scene = context.scene
        old = (scene.umz_selected_animation or "").strip()
        new = (self.new_name or "").strip()

        if not old:
            self.report({'WARNING'}, "Анимация не выбрана.")
            return {'CANCELLED'}
        if not new:
            self.report({'ERROR'}, "Новое имя пустое.")
            return {'CANCELLED'}
        if new == old:
            return {'FINISHED'}

        internal = _storage.read_internal_films()
        if old not in internal:
            self.report({'ERROR'}, "Переименование поддерживается только для внутренних анимаций (internal).")
            return {'CANCELLED'}
        if new in internal:
            self.report({'ERROR'}, "Имя уже занято.")
            return {'CANCELLED'}

        entry = internal.pop(old)
        internal[new] = entry
        _storage.write_internal_films(internal)

        try:
            _storage.remove_animation_file(old)
            _storage.write_animation_to_file(new, entry)
        except Exception:
            pass

        _storage.mark_cache_dirty()
        _rebuild_list(scene, prefer_name=new)
        self.report({'INFO'}, f"Переименовано: {old} -> {new}")
        return {'FINISHED'}

    def invoke(self, context, event):
        scene = context.scene
        old = (scene.umz_selected_animation or "").strip()
        self.new_name = old
        return context.window_manager.invoke_props_dialog(self)


class ANIM_OT_refresh_transform_conflicts(bpy.types.Operator):
    bl_idname = "umz.anim_refresh_transform_conflicts"
    bl_label = "Проверить конфликты"
    bl_description = "Сравнить текущую мировую траекторию объектов с сохранёнными world matrices анимации"

    def execute(self, context):
        scene = context.scene
        anim_name = (getattr(scene, 'umz_selected_animation', '') or '').strip()
        if not anim_name:
            self.report({'WARNING'}, 'Анимация не выбрана.')
            return {'CANCELLED'}

        try:
            data = get_animation_transform_conflicts(anim_name, scene=scene)
        except Exception as e:
            self.report({'ERROR'}, f'Ошибка проверки конфликтов: {e}')
            return {'CANCELLED'}

        conflicts = data.get('conflicts') or []
        _store_transform_conflicts_cache(scene, anim_name, '', conflicts)
        self.report({'INFO'}, f'Найдено конфликтов: {len(conflicts)}')
        return {'FINISHED'}


class ANIM_OT_fix_transform_conflict_single(bpy.types.Operator):
    bl_idname = "umz.anim_fix_transform_conflict_single"
    bl_label = "Исправить конфликт"
    bl_description = "Пересчитать локальные keyframes из сохранённых world matrices относительно текущего parent"

    object_name: StringProperty(name='Объект', default='')

    def execute(self, context):
        scene = context.scene
        anim_name = (getattr(scene, 'umz_selected_animation', '') or '').strip()
        if not anim_name:
            self.report({'WARNING'}, 'Анимация не выбрана.')
            return {'CANCELLED'}
        if not (self.object_name or '').strip():
            self.report({'WARNING'}, 'Объект не задан.')
            return {'CANCELLED'}

        try:
            res = rewrite_animation_conflicts_from_snapshot(anim_name, object_names=[self.object_name], scene=scene)
        except Exception as e:
            self.report({'ERROR'}, f'Ошибка исправления: {e}')
            return {'CANCELLED'}

        changed = res.get('changed_objects') or []
        if not changed:
            self.report({'WARNING'}, 'Для этого объекта нечего исправлять автоматически.')
            return {'CANCELLED'}

        try:
            data = get_animation_transform_conflicts(anim_name, scene=scene)
            conflicts = data.get('conflicts') or []
            _store_transform_conflicts_cache(scene, anim_name, '', conflicts)
        except Exception:
            pass

        self.report({'INFO'}, f'Исправлен объект: {changed[0]}')
        return {'FINISHED'}


class ANIM_OT_fix_transform_conflicts_all(bpy.types.Operator):
    bl_idname = "umz.anim_fix_transform_conflicts_all"
    bl_label = "Исправить все конфликты"
    bl_description = "Пересчитать локальные keyframes из сохранённых world matrices для всех конфликтующих объектов"

    def execute(self, context):
        scene = context.scene
        anim_name = (getattr(scene, 'umz_selected_animation', '') or '').strip()
        if not anim_name:
            self.report({'WARNING'}, 'Анимация не выбрана.')
            return {'CANCELLED'}

        try:
            res = rewrite_animation_conflicts_from_snapshot(anim_name, scene=scene)
        except Exception as e:
            self.report({'ERROR'}, f'Ошибка исправления: {e}')
            return {'CANCELLED'}

        changed = res.get('changed_objects') or []
        if not changed:
            self.report({'WARNING'}, 'Автоматически исправлять нечего.')
            return {'CANCELLED'}

        try:
            data = get_animation_transform_conflicts(anim_name, scene=scene)
            conflicts = data.get('conflicts') or []
            _store_transform_conflicts_cache(scene, anim_name, '', conflicts)
        except Exception:
            pass

        self.report({'INFO'}, f'Исправлено объектов: {len(changed)}')
        return {'FINISHED'}


class ANIM_OT_fill_gltf_id_scene(bpy.types.Operator):
    bl_idname = "umz.anim_fill_gltf_id_scene"
    bl_label = "Установить gltf_id"

    def execute(self, context):
        count = 0
        for obj in context.scene.objects:
            try:
                obj[GLTF_ID_PROP] = obj.name
                count += 1
            except Exception:
                pass
        _invalidate_gltf_cache()
        self.report({'INFO'}, f"{GLTF_ID_PROP} установлен для объектов: {count}")
        return {'FINISHED'}


# -------------------------
# Meta Operators (selected-based + locks + copy)
# -------------------------

class ANIM_OT_init_meta_fields_on_active(bpy.types.Operator):
    bl_idname = "umz.anim_init_meta_fields"
    bl_label = "Создать поля (выделенные)"
    bl_description = "Создаёт meta-поля на всех выделенных объектах (если ничего не выделено — на активном)"

    def execute(self, context):
        targets = _get_target_objects_for_meta_ops(context)
        if not targets:
            self.report({'WARNING'}, "Нет активного/выделенных объектов.")
            return {'CANCELLED'}

        created = 0

        # cache counts per collection to avoid recomputing many times
        counts_cache = {}  # collection_name -> dict(base->count)

        for obj in targets:
            obozn, naim = _split_obj_name(obj.name)

            col = _get_primary_collection_for_object(obj)
            if col:
                col_key = col.name_full if hasattr(col, "name_full") else col.name
            else:
                col_key = "__SCENE__"

            if col_key not in counts_cache:
                counts_cache[col_key] = _compute_counts_for_collection(col) if col else {}

            counts = counts_cache[col_key]
            base = _base_name_for_count(obj.name)
            qty = int(counts.get(base, 1))

            for k in META_KEYS_ORDER:
                try:
                    if k not in obj.keys():
                        if k == "oboznachenie":
                            obj[k] = obozn
                        elif k == "naimenovanie":
                            obj[k] = naim
                        elif k == "count_in_animation":
                            obj[k] = qty
                        elif k == "count_in_zip":
                            obj[k] = qty
                        elif k == "zip":
                            obj[k] = "О"
                        else:
                            obj[k] = ""
                        created += 1
                except Exception:
                    pass

            _ensure_lock_props(obj)

        self.report({'INFO'}, f"Создано полей: {created}")
        return {'FINISHED'}


class ANIM_OT_update_meta_fields_all(bpy.types.Operator):
    bl_idname = "umz.anim_update_meta_fields_all"
    bl_label = "Обновить поля (выделенные)"
    bl_description = "Обновить Обозначение/Наименование и количества для выделенных объектов (с учётом lock)"

    def execute(self, context):
        targets = _get_target_objects_for_meta_ops(context)
        if not targets:
            self.report({'WARNING'}, "Нет активного/выделенных объектов.")
            return {'CANCELLED'}

        updated = 0

        # cache counts per collection
        counts_cache = {}

        for obj in targets:
            try:
                keys = obj.keys()
            except Exception:
                continue

            if ("oboznachenie" not in keys) and ("naimenovanie" not in keys) and ("count_in_animation" not in keys) and ("count_in_zip" not in keys):
                continue

            _ensure_lock_props(obj)

            obozn, naim = _split_obj_name(obj.name)

            col = _get_primary_collection_for_object(obj)
            if col:
                col_key = col.name_full if hasattr(col, "name_full") else col.name
            else:
                col_key = "__SCENE__"

            if col_key not in counts_cache:
                counts_cache[col_key] = _compute_counts_for_collection(col) if col else {}

            counts = counts_cache[col_key]
            base = _base_name_for_count(obj.name)
            qty = int(counts.get(base, 1))

            try:
                if "oboznachenie" in obj.keys() and (not _is_locked(obj, "oboznachenie")):
                    obj["oboznachenie"] = obozn
                    updated += 1
            except Exception:
                pass
            try:
                if "naimenovanie" in obj.keys() and (not _is_locked(obj, "naimenovanie")):
                    obj["naimenovanie"] = naim
                    updated += 1
            except Exception:
                pass
            try:
                if "count_in_animation" in obj.keys() and (not _is_locked(obj, "count_in_animation")):
                    obj["count_in_animation"] = qty
                    updated += 1
            except Exception:
                pass
            try:
                if "count_in_zip" in obj.keys() and (not _is_locked(obj, "count_in_zip")):
                    obj["count_in_zip"] = qty
                    updated += 1
            except Exception:
                pass

        self.report({'INFO'}, f"Обновлено значений: {updated}")
        return {'FINISHED'}


class ANIM_OT_remove_meta_fields_on_active(bpy.types.Operator):
    bl_idname = "umz.anim_remove_meta_fields"
    bl_label = "Удалить поля (выделенные)"
    bl_description = "Удаляет meta-поля на всех выделенных объектах (если ничего не выделено — на активном)"

    def execute(self, context):
        targets = _get_target_objects_for_meta_ops(context)
        if not targets:
            self.report({'WARNING'}, "Нет активного/выделенных объектов.")
            return {'CANCELLED'}

        removed = 0
        for obj in targets:
            for k in META_KEYS_ORDER:
                try:
                    if k in obj.keys():
                        del obj[k]
                        removed += 1
                except Exception:
                    pass

            for k in _LOCKABLE_META_KEYS:
                lk = _lock_prop_name(k)
                try:
                    if lk in obj.keys():
                        del obj[lk]
                except Exception:
                    pass

        self.report({'INFO'}, f"Удалено полей: {removed}")
        return {'FINISHED'}


class ANIM_OT_copy_meta_fields_active_to_selected(bpy.types.Operator):
    bl_idname = "umz.anim_copy_meta_fields"
    bl_label = "Копировать поля активного -> выделенные"
    bl_description = "Копирует значения meta-полей (включая zip) с активного объекта на все выделенные (кроме активного). Уважает lock на целевых объектах."

    def execute(self, context):
        src = context.object
        if not src:
            self.report({'WARNING'}, "Нет активного объекта.")
            return {'CANCELLED'}

        try:
            selected = list(context.selected_objects) if context.selected_objects else []
        except Exception:
            selected = []

        if not selected:
            self.report({'WARNING'}, "Нет выделенных объектов.")
            return {'CANCELLED'}

        src_fields = {}
        for k in META_KEYS_ORDER:
            try:
                if k in src.keys():
                    src_fields[k] = src.get(k)
            except Exception:
                pass

        if not src_fields:
            self.report({'WARNING'}, "У активного объекта нет meta-полей для копирования.")
            return {'CANCELLED'}

        copied = 0
        for dst in selected:
            if dst == src:
                continue

            _ensure_lock_props(dst)

            for k, v in src_fields.items():
                if k in _LOCKABLE_META_KEYS and _is_locked(dst, k):
                    continue
                try:
                    dst[k] = v
                    copied += 1
                except Exception:
                    pass

        self.report({'INFO'}, f"Скопировано значений: {copied}")
        return {'FINISHED'}


class ANIM_OT_set_outline_rule_on_selected(bpy.types.Operator):
    bl_idname = "umz.anim_set_outline_rule_on_selected"
    bl_label = "Правило обводки"
    bl_description = "Добавить правило обводки для активного/выделенных объектов по текущему кадру"

    def execute(self, context):
        try:
            duration_sec = float(getattr(context.scene, "umz_outline_rule_duration_sec", OUTLINE_RULE_DEFAULT_DURATION_SEC))
        except Exception:
            duration_sec = float(OUTLINE_RULE_DEFAULT_DURATION_SEC)

        try:
            interval_sec = float(getattr(context.scene, "umz_outline_rule_interval_sec", OUTLINE_RULE_DEFAULT_INTERVAL_SEC))
        except Exception:
            interval_sec = float(OUTLINE_RULE_DEFAULT_INTERVAL_SEC)

        targets = _get_target_objects_for_meta_ops(context)
        if not targets:
            self.report({'WARNING'}, "Нет активного или выделенных объектов.")
            return {'CANCELLED'}

        applied = set_outline_rule_on_objects(
            targets,
            frame=getattr(context.scene, "frame_current", 0),
            duration_sec=duration_sec,
            interval_sec=interval_sec,
        )
        if applied <= 0:
            self.report({'WARNING'}, "Не удалось записать правило обводки.")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Правило обводки добавлено: {applied}")
        return {'FINISHED'}


class ANIM_OT_clear_outline_rule_on_selected(bpy.types.Operator):
    bl_idname = "umz.anim_clear_outline_rule_on_selected"
    bl_label = "Очистить правила обводки"
    bl_description = "Удалить все правила обводки с активного/выделенных объектов"

    def execute(self, context):
        targets = _get_target_objects_for_meta_ops(context)
        if not targets:
            self.report({'WARNING'}, "Нет активного или выделенных объектов.")
            return {'CANCELLED'}

        cleared = clear_outline_rule_on_objects(targets)
        if cleared <= 0:
            self.report({'WARNING'}, "Правила обводки не найдены.")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Правила обводки очищены: {cleared}")
        return {'FINISHED'}


# -------------------------
# UI drawing
# -------------------------

def _draw_outline_rule_fields(box, context):
    obj = context.object

    if not obj:
        box.label(text="Нет активного объекта")
        return

    box.label(text=f"Объект: {obj.name}", icon='OBJECT_DATA')

    scene = context.scene
    row_cfg = box.row(align=True)
    row_cfg.prop(scene, "umz_outline_rule_duration_sec", text="Длительность, c")
    row_cfg.prop(scene, "umz_outline_rule_interval_sec", text="Шаг, c")

    row_btn = box.row(align=True)
    row_btn.operator("umz.anim_set_outline_rule_on_selected", text="Добавить по текущему кадру", icon='KEYTYPE_MOVING_HOLD_VEC')
    row_btn.operator("umz.anim_clear_outline_rule_on_selected", text="Очистить все", icon='TRASH')

    rules = []
    try:
        rules = get_object_outline_rules_data(obj) or []
    except Exception:
        rules = []

    if not rules:
        box.label(text="Правила не заданы", icon='INFO')
        return

    box.label(text=f"Количество правил: {len(rules)}", icon='SEQ_SEQUENCER')
    info = box.column(align=True)
    for idx, rule in enumerate(rules, start=1):
        rbox = info.box()
        rbox.label(text=f"Правило #{idx}", icon='DOT')
        try:
            rbox.label(text=f"Кадр старта: {int(rule.get('frame', 0))}")
        except Exception:
            pass
        try:
            rbox.label(text=f"Время старта: {float(rule.get('time_sec', 0.0)):.3f} c")
        except Exception:
            pass
        try:
            rbox.label(text=f"Длительность: {float(rule.get('duration_sec', 0.0)):.3f} c")
        except Exception:
            pass
        try:
            rbox.label(text=f"Интервал мигания: {float(rule.get('interval_sec', 0.0)):.3f} c")
        except Exception:
            pass


def _draw_transform_conflicts(box, context):
    scene = context.scene
    anim_name = (getattr(scene, 'umz_selected_animation', '') or '').strip()

    top = box.row(align=True)
    top.operator('umz.anim_refresh_transform_conflicts', text='Проверить', icon='FILE_REFRESH')
    top.operator('umz.anim_fix_transform_conflicts_all', text='Исправить все', icon='MODIFIER')

    if not anim_name:
        box.label(text='Выберите анимацию в библиотеке.', icon='INFO')
        return

    info = box.column(align=True)
    info.label(text=f'Анимация: {anim_name}', icon='ACTION')
    info.label(text='Эталон: сохранённые world matrices из animation data', icon='ORIENTATION_GLOBAL')

    cache_current = _is_transform_conflict_cache_current(scene)
    conflicts = _read_transform_conflicts_cache(scene) if cache_current else []

    if not cache_current:
        box.label(text='Нажмите «Проверить», чтобы обновить world-space конфликты.', icon='INFO')
        return

    if not conflicts:
        box.label(text='Конфликтов world-space не найдено.', icon='CHECKMARK')
        return

    box.label(text=f'Найдено конфликтов: {len(conflicts)}', icon='ERROR')
    col = box.column(align=True)
    for item in conflicts:
        name = str(item.get('object_name') or '')
        if not name:
            continue
        rbox = col.box()
        header = rbox.row(align=True)
        header.label(text=name, icon='OBJECT_DATA')
        op = header.operator('umz.anim_fix_transform_conflict_single', text='Исправить', icon='MODIFIER')
        op.object_name = name

        frame_no = int(item.get('first_conflict_frame', 0) or 0)
        rbox.label(text=f'Первый конфликтующий кадр: {frame_no}', icon='TIME')
        rbox.label(text=f"Проверено кадров: {int(item.get('world_frames_checked', 0))}, конфликтующих: {int(item.get('world_frames_conflicted', 0))}")

        if item.get('location_conflict'):
            d = item.get('delta_location') or [0.0, 0.0, 0.0]
            rbox.label(text=f"World Δ location: X {d[0]:.4f} | Y {d[1]:.4f} | Z {d[2]:.4f}")
        if item.get('scale_conflict'):
            d = item.get('scale_delta') or [0.0, 0.0, 0.0]
            rbox.label(text=f"World Δ scale: X {d[0]:.4f} | Y {d[1]:.4f} | Z {d[2]:.4f}")
        if item.get('rotation_conflict'):
            rbox.label(text=f"World rotation error: {float(item.get('max_rotation_error', 0.0)):.6f} rad")
        if item.get('parent_conflict'):
            cur_parent = str(item.get('current_parent') or '(нет)')
            snap_parent = str(item.get('snapshot_parent') or '(нет)')
            rbox.label(text=f'Parent: сейчас {cur_parent} / при capture {snap_parent}', icon='CONSTRAINT')



def _draw_meta_fields(box, context):
    obj = context.object

    if not obj:
        box.label(text="Нет активного объекта")
        return

    top = box.row(align=True)
    top.label(text=obj.name, icon='OBJECT_DATA')
    buttons = top.row(align=True)
    buttons.operator("umz.anim_init_meta_fields", text="", icon='ADD')
    buttons.operator("umz.anim_remove_meta_fields", text="", icon='TRASH')
    buttons.operator("umz.anim_copy_meta_fields", text="", icon='PASTEDOWN')
    buttons.operator("umz.anim_update_meta_fields_all", text="", icon='FILE_REFRESH')

    missing_any = False
    for k in META_KEYS_ORDER:
        try:
            if k not in obj.keys():
                missing_any = True
                break
        except Exception:
            missing_any = True
            break

    if missing_any:
        box.label(text="Поля отсутствуют — нажмите +", icon='INFO')
        return

    _ensure_lock_props(obj)

    col = box.column(align=True)
    for k in META_KEYS_ORDER:
        label = _META_LABELS_RU.get(k, k)

        if k == "zip":
            col.separator(factor=0.15)
            col.label(text="ЗИП")
            _sync_object_zip_enum_from_custom_prop(obj)
            col.prop(obj, "umz_zip_choice_obj", text="")
            continue

        if k in _LOCKABLE_META_KEYS:
            col.separator(factor=0.15)
            col.label(text=label)

            row = col.row(align=True)
            try:
                row.prop(obj, f'["{k}"]', text="")
            except Exception:
                pass

            lk = _lock_prop_name(k)
            try:
                lock_icon = 'LOCKED' if bool(obj.get(lk, False)) else 'UNLOCKED'
            except Exception:
                lock_icon = 'UNLOCKED'

            try:
                row.prop(obj, f'["{lk}"]', text="", icon=lock_icon, emboss=True)
            except Exception:
                pass

            continue

        col.separator(factor=0.15)
        col.label(text=label)
        try:
            col.prop(obj, f'["{k}"]', text="")
        except Exception:
            pass


def draw_ui(layout, context):
    scene = context.scene

    # -------------------------
    # 1. Библиотека анимаций
    # -------------------------
    header = layout.row(align=True)
    is_open = bool(getattr(scene, "umz_ui_animation_library_open", True))
    icon = 'TRIA_DOWN' if is_open else 'TRIA_RIGHT'
    header.prop(scene, "umz_ui_animation_library_open", text="Библиотека анимаций", icon=icon, emboss=False)

    if is_open:
        box = layout.box()

        folder = _storage.get_external_folder()
        row_dir = box.row(align=True)

        if folder:
            row_dir.label(text=folder, icon='FILE_FOLDER')
            row_dir.operator("umz.anim_set_directory", text="", icon='FILE_FOLDER')
            row_dir.operator("umz.anim_clear_directory", text="", icon='X')
        else:
            row_dir.scale_y = 1.4
            row_dir.operator("umz.anim_set_directory", text="Папка для анимаций", icon='FILE_FOLDER')

        settings_box = box.box()
        settings_box.prop(scene, "umz_anim_visible_selected_only", text="Только выделенные объекты")
        settings_box.prop(scene, "umz_export_alpha_tracks", text="Экспорт прозрачности (alpha)")
        settings_box.prop(scene, "umz_text_and_markers", text="Текст и метки")
        settings_box.prop(scene, "umz_anim_full_delete", text="Полное удаление")

        row = box.row()
        row.template_list(
            "ANIM_UL_umz_list",
            "",
            scene,
            "umz_anim_list",
            scene,
            "umz_anim_list_index",
            rows=8
        )

        col_ops = row.column(align=True)
        col_ops.operator("umz.anim_save_selected", text="", icon='FILE_TICK')
        col_ops.operator("umz.anim_load_selected", text="", icon='IMPORT')
        col_ops.operator("umz.anim_delete_selected", text="", icon='TRASH')
        col_ops.separator()
        col_ops.operator("umz.anim_rename_selected", text="", icon='OUTLINER_DATA_FONT')
        col_ops.operator("umz.anim_refresh_list", text="", icon='FILE_REFRESH')

        sel = (scene.umz_selected_animation or "").strip()
        if sel:
            film = (_storage.read_all_films_cached() or {}).get(sel) or {}
            created = film.get("created_at")
            if created:
                box.label(text=f"Создано: {format_created(created)}")
            desc = film.get("description", "")
            if desc:
                box.label(text=f"Описание: {desc}")

    layout.separator(factor=0.35)
    layout.label(text="Инструменты", icon='TOOL_SETTINGS')

    # -------------------------
    # 2. Правила контурной обводки
    # -------------------------
    header = layout.row(align=True)
    is_open = bool(getattr(scene, "umz_ui_outline_rules_open", False))
    icon = 'TRIA_DOWN' if is_open else 'TRIA_RIGHT'
    header.prop(scene, "umz_ui_outline_rules_open", text="Правила контурной обводки", icon=icon, emboss=False)

    if is_open:
        box = layout.box()

        row_gltf = box.row(align=True)
        row_gltf.operator("umz.anim_fill_gltf_id_scene", text="Установить gltf_id", icon='SORTALPHA')
        if _scene_has_missing_gltf_id(scene) or _scene_has_duplicate_gltf_id(scene):
            warn = row_gltf.row(align=True)
            warn.alert = True
            warn.label(text="", icon='ERROR')

        row_mat = box.row(align=True)
        row_mat.operator("umz.set_material_shaders_cp", text="Установить shaders (materials)", icon='MATERIAL')
        try:
            if umz_scene_has_materials_missing_mix_factor_cp():
                warn = row_mat.row(align=True)
                warn.alert = True
                warn.label(icon='ERROR')
            elif umz_scene_has_materials_missing_shaders_cp():
                row_mat.label(text="", icon='ERROR')
        except Exception:
            pass

        box.separator()
        _draw_outline_rule_fields(box, context)

    # -------------------------
    # 3. Исправление конфликтов
    # -------------------------
    header = layout.row(align=True)
    is_open = bool(getattr(scene, "umz_ui_conflict_tools_open", False))
    icon = 'TRIA_DOWN' if is_open else 'TRIA_RIGHT'
    header.prop(scene, "umz_ui_conflict_tools_open", text="Исправление конфликтов", icon=icon, emboss=False)

    if is_open:
        box = layout.box()
        _draw_transform_conflicts(box, context)

    # -------------------------
    # 4. Поля объекта
    # -------------------------
    header = layout.row(align=True)
    is_open = bool(getattr(scene, "umz_ui_object_fields_open", False))
    icon = 'TRIA_DOWN' if is_open else 'TRIA_RIGHT'
    header.prop(scene, "umz_ui_object_fields_open", text="Поля объекта", icon=icon, emboss=False)

    if is_open:
        box = layout.box()
        _draw_meta_fields(box, context)


_classes = (
    UMZ_AnimationItem,
    ANIM_UL_umz_list,

    ANIM_OT_refresh_list,
    ANIM_OT_set_dir,
    ANIM_OT_clear_dir,
    ANIM_OT_save_selected,
    ANIM_OT_load_selected,
    ANIM_OT_delete_selected,
    ANIM_OT_rename_selected,

    ANIM_OT_refresh_transform_conflicts,
    ANIM_OT_fix_transform_conflict_single,
    ANIM_OT_fix_transform_conflicts_all,

    ANIM_OT_fill_gltf_id_scene,

    UMZ_OT_set_material_shaders_cp,

    ANIM_OT_set_outline_rule_on_selected,
    ANIM_OT_clear_outline_rule_on_selected,

    ANIM_OT_init_meta_fields_on_active,
    ANIM_OT_update_meta_fields_all,
    ANIM_OT_remove_meta_fields_on_active,
    ANIM_OT_copy_meta_fields_active_to_selected,
)

_registered = False
_register_cb = None


def register(register_callback):
    global _registered, _register_cb
    if _registered:
        return

    bpy.utils.register_class(UMZ_AnimationItem)
    bpy.utils.register_class(ANIM_UL_umz_list)
    register_scene_props()
    _register_object_zip_prop()

    for c in _classes:
        if c in (UMZ_AnimationItem, ANIM_UL_umz_list):
            continue
        bpy.utils.register_class(c)

    _register_cb = register_callback
    try:
        register_callback({
            "id": MODULE_ID,
            "name": MODULE_NAME,
            "draw": draw_ui,
            "register": register,
            "unregister": unregister
        })
    except Exception:
        pass

    try:
        _storage.mark_cache_dirty()
        _rebuild_list(bpy.context.scene)
    except Exception:
        pass

    _registered = True


def unregister():
    global _registered, _register_cb
    if not _registered:
        return

    for c in reversed(_classes):
        if c in (UMZ_AnimationItem, ANIM_UL_umz_list):
            continue
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass

    unregister_scene_props()
    _unregister_object_zip_prop()

    try:
        bpy.utils.unregister_class(ANIM_UL_umz_list)
    except Exception:
        pass
    try:
        bpy.utils.unregister_class(UMZ_AnimationItem)
    except Exception:
        pass

    _registered = False
    _register_cb = None