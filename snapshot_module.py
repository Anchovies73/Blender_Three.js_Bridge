bl_info = {
    "name": "UMZ Snapshot (world-aware)",
    "author": "Vlad",
    "version": (0, 2, 0),
    "blender": (4, 0, 0),
    "category": "Object",
}

import bpy
import json
import os
from datetime import datetime
from bpy.props import StringProperty, EnumProperty, BoolProperty, IntProperty

from .transform_utils import (
    matrix_to_list,
    matrix_from_list,
    capture_local_matrix,
    capture_world_matrix,
    decompose_matrix,
    compute_local_from_world,
    apply_local_matrix_to_object,
    log_debug,
)

MODULE_ID = "snapshot"
MODULE_NAME = "Сохранение сцен"
SNAPSHOT_TEXT_NAME = "scene_snapshots.json"
SNAPSHOT_SCHEMA_VERSION = 2
MISSING_EMPTY_SUFFIX = "__Missing"

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


def _read_internal_snapshots_raw():
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
    SNAPSHOT_CACHE = dict(d)
    SNAPSHOT_CACHE_DIRTY = False


def get_external_folder():
    addon = __name__.split('.')[0]
    try:
        prefs = bpy.context.preferences.addons.get(addon).preferences
    except Exception:
        prefs = None
    if prefs and getattr(prefs, "external_snapshots_folder", ""):
        return bpy.path.abspath(prefs.external_snapshots_folder)
    return None


def write_snapshot_to_file(name, snapshot_entry):
    folder = get_external_folder()
    if not folder:
        return False
    try:
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{name}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({name: snapshot_entry}, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def remove_snapshot_file(name):
    folder = get_external_folder()
    if not folder:
        return False
    path = os.path.join(folder, f"{name}.json")
    try:
        if os.path.isfile(path):
            os.remove(path)
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

        if isinstance(data, dict):
            if "snapshots" in data and isinstance(data["snapshots"], dict):
                for k, v in data["snapshots"].items():
                    if isinstance(v, dict) and "objects" in v:
                        res[k] = v
            else:
                for k, v in data.items():
                    if isinstance(v, dict) and "objects" in v:
                        res[k] = v
    return res


def read_all_snapshots_raw():
    internal = _read_internal_snapshots_raw()
    external = read_external_snapshots()
    merged = dict(internal)
    merged.update(external)
    return merged


def get_cached_snapshots():
    global SNAPSHOT_CACHE, SNAPSHOT_CACHE_DIRTY
    if SNAPSHOT_CACHE_DIRTY:
        SNAPSHOT_CACHE = dict(read_all_snapshots_raw())
        SNAPSHOT_CACHE_DIRTY = False
    return SNAPSHOT_CACHE


def safe_set_frame(frame: int):
    try:
        bpy.context.scene.frame_set(int(frame))
    except Exception:
        pass


def _format_object_snapshot(obj):
    local_matrix = capture_local_matrix(obj)
    world_matrix = capture_world_matrix(obj)
    out = {
        "name": obj.name,
        "type": obj.type,
        "parent": obj.parent.name if obj.parent else None,
        "parent_type": getattr(obj, "parent_type", "OBJECT"),
        "parent_bone": getattr(obj, "parent_bone", "") or "",
        "collections": [coll.name for coll in obj.users_collection],
        "material_slots": [],
        "data_name": getattr(obj.data, "name", None) if getattr(obj, "data", None) else None,
        "matrix_local": matrix_to_list(local_matrix),
        "matrix_world": matrix_to_list(world_matrix),
        "matrix_parent_inverse": matrix_to_list(getattr(obj, "matrix_parent_inverse", matrix_from_list(None))),
    }
    out.update({
        "local_" + k: v for k, v in decompose_matrix(local_matrix).items()
    })
    out.update({
        "world_" + k: v for k, v in decompose_matrix(world_matrix).items()
    })

    mats_seen = []
    for ms in getattr(obj, "material_slots", []) or []:
        if ms.material:
            mats_seen.append(ms.material.name)
    out["material_slots"] = mats_seen
    return out


def collect_snapshot(name: str):
    scene = bpy.context.scene
    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "scene_name": scene.name,
        "created_at": datetime.now().isoformat(),
        "scene_frame": int(scene.frame_current),
        "objects": {},
        "materials": [],
    }

    mats_seen = {}
    for obj in list(scene.objects):
        obj_data = _format_object_snapshot(obj)
        snapshot["objects"][obj.name] = obj_data
        for mname in obj_data.get("material_slots", []):
            if mname not in mats_seen:
                mat = bpy.data.materials.get(mname)
                mats_seen[mname] = {
                    "name": mname,
                    "use_nodes": bool(getattr(mat, "use_nodes", False)) if mat else False,
                }

    snapshot["materials"] = list(mats_seen.values())

    internal_raw = _read_internal_snapshots_raw()
    internal_raw[name] = snapshot
    write_internal_snapshots(internal_raw)
    write_snapshot_to_file(name, snapshot)

    global SNAPSHOT_CACHE_DIRTY
    SNAPSHOT_CACHE_DIRTY = True
    try:
        scene.umz_selected_snapshot = name
    except Exception:
        pass
    return True


def delete_snapshot(snapshot_name):
    global SNAPSHOT_CACHE_DIRTY
    internal_raw = _read_internal_snapshots_raw()
    removed = False
    if snapshot_name in internal_raw:
        del internal_raw[snapshot_name]
        write_internal_snapshots(internal_raw)
        removed = True
    if remove_snapshot_file(snapshot_name):
        removed = True
    SNAPSHOT_CACHE_DIRTY = True

    scene = bpy.context.scene
    all_names = list(get_cached_snapshots().keys())
    try:
        scene.umz_selected_snapshot = all_names[0] if all_names else ""
    except Exception:
        pass
    try:
        if getattr(scene, "umz_missing_snapshot_name", "") == snapshot_name:
            scene.umz_missing_objects_count = 0
            scene.umz_missing_snapshot_name = ""
    except Exception:
        pass
    return removed


def _find_data_in_current_blend(obj_type, data_name):
    if not data_name:
        return None
    type_map = {
        'MESH': bpy.data.meshes,
        'CURVE': bpy.data.curves,
        'ARMATURE': bpy.data.armatures,
        'LATTICE': bpy.data.lattices,
        'CAMERA': bpy.data.cameras,
        'LIGHT': bpy.data.lights,
    }
    coll = type_map.get(obj_type)
    if coll is not None:
        return coll.get(data_name)
    gp_coll = getattr(bpy.data, "grease_pencils", None)
    if obj_type == 'GREASEPENCIL' and gp_coll:
        return gp_coll.get(data_name)
    return None


def _load_data_from_external_blend(obj_type, data_name, blend_path):
    if not data_name or not blend_path or not os.path.isfile(blend_path):
        return None
    type_map = {
        'MESH': ('meshes', bpy.data.meshes),
        'CURVE': ('curves', bpy.data.curves),
        'ARMATURE': ('armatures', bpy.data.armatures),
        'LATTICE': ('lattices', bpy.data.lattices),
        'CAMERA': ('cameras', bpy.data.cameras),
        'LIGHT': ('lights', bpy.data.lights),
        'GREASEPENCIL': ('grease_pencils', getattr(bpy.data, 'grease_pencils', None)),
    }
    lib_attr, collection = type_map.get(obj_type, (None, None))
    if lib_attr is None or collection is None:
        return None
    try:
        with bpy.data.libraries.load(blend_path, link=False) as (data_from, data_to):
            from_list = getattr(data_from, lib_attr, None)
            if not from_list or data_name not in from_list:
                return None
            setattr(data_to, lib_attr, [data_name])
    except Exception:
        return None
    try:
        return collection.get(data_name)
    except Exception:
        return None


def link_object_to_collections(obj, coll_names):
    if not coll_names:
        return
    desired = []
    for name in coll_names:
        coll = bpy.data.collections.get(name)
        if coll:
            desired.append(coll)
    if not desired:
        return
    for c in desired:
        try:
            if obj.name not in c.objects:
                c.objects.link(obj)
        except Exception:
            pass
    for c in list(obj.users_collection):
        if c.name not in coll_names:
            try:
                c.objects.unlink(obj)
            except Exception:
                pass


def _safe_missing_name(base_name):
    candidate = f"{base_name}{MISSING_EMPTY_SUFFIX}"
    if bpy.data.objects.get(candidate) is None:
        return candidate
    i = 1
    while bpy.data.objects.get(f"{candidate}_{i}") is not None:
        i += 1
    return f"{candidate}_{i}"


def _create_missing_placeholder(snapshot_name, obj_data, scene):
    missing_name = _safe_missing_name(snapshot_name)
    empty = bpy.data.objects.new(missing_name, None)
    empty.empty_display_type = 'PLAIN_AXES'
    empty["umz_missing_from_snapshot"] = True
    empty["umz_missing_original_name"] = snapshot_name
    empty["umz_missing_data_name"] = str(obj_data.get("data_name") or "")
    empty["umz_missing_object_type"] = str(obj_data.get("type") or "")
    scene.collection.objects.link(empty)
    return empty


def restore_snapshot(name: str, delete_added: bool = True, restore_materials: bool = True):
    scene = bpy.context.scene
    original_frame = int(scene.frame_current)
    snaps = get_cached_snapshots()
    snapshot = snaps.get(name)
    if not snapshot:
        return {"error": "Снимок не найден"}

    saved_objects = snapshot.get("objects") or {}
    saved_mats = {m["name"]: m for m in (snapshot.get("materials") or []) if isinstance(m, dict) and "name" in m}
    saved_frame = snapshot.get("scene_frame", 0)

    safe_set_frame(saved_frame)

    if restore_materials:
        for mat_info in saved_mats.values():
            if bpy.data.materials.get(mat_info["name"]) is None:
                try:
                    bpy.data.materials.new(mat_info["name"])
                except Exception:
                    pass

    current_names = {o.name for o in scene.objects}
    saved_names = set(saved_objects.keys())
    extra = current_names - saved_names
    if delete_added and extra:
        for n in list(extra):
            o = scene.objects.get(n) or bpy.data.objects.get(n)
            if not o:
                continue
            for c in list(o.users_collection):
                try:
                    c.objects.unlink(o)
                except Exception:
                    pass
            try:
                bpy.data.objects.remove(o, do_unlink=True)
            except Exception:
                pass

    resolved = {}
    missing_objects_count = 0

    # pass 1: ensure objects exist and build mapping snapshot_name -> actual scene object
    for snapshot_obj_name, obj_data in saved_objects.items():
        obj = scene.objects.get(snapshot_obj_name) or bpy.data.objects.get(snapshot_obj_name)
        if obj is None:
            data_block = _find_data_in_current_blend(obj_data.get("type"), obj_data.get("data_name"))
            try:
                if data_block is not None:
                    obj = bpy.data.objects.new(snapshot_obj_name, data_block)
                    scene.collection.objects.link(obj)
                else:
                    obj = _create_missing_placeholder(snapshot_obj_name, obj_data, scene)
                    missing_objects_count += 1
            except Exception:
                continue
        resolved[snapshot_obj_name] = obj

    # collections and materials can be restored immediately
    for snapshot_obj_name, obj_data in saved_objects.items():
        obj = resolved.get(snapshot_obj_name)
        if not obj:
            continue
        link_object_to_collections(obj, obj_data.get("collections", []))
        if restore_materials and getattr(obj, "data", None) and hasattr(obj.data, "materials"):
            mats = obj_data.get("material_slots", [])
            try:
                while len(obj.data.materials) < len(mats):
                    obj.data.materials.append(None)
            except Exception:
                pass
            for idx, mname in enumerate(mats):
                try:
                    obj.material_slots[idx].material = bpy.data.materials.get(mname)
                except Exception:
                    pass

    # pass 2: parent relations using resolved mapping (supports placeholders)
    for snapshot_obj_name, obj_data in saved_objects.items():
        obj = resolved.get(snapshot_obj_name)
        if not obj:
            continue
        parent_name = str(obj_data.get("parent") or "")
        parent_obj = resolved.get(parent_name)
        try:
            if parent_obj is None:
                obj.parent = None
                obj.parent_type = 'OBJECT'
                obj.parent_bone = ""
            else:
                obj.parent = parent_obj
                obj.parent_type = 'OBJECT'
                obj.parent_bone = ""
        except Exception:
            pass

    # pass 3: world-space restore by hierarchy depth
    depths = {}

    def _depth(name_key):
        if name_key in depths:
            return depths[name_key]
        parent_name = str((saved_objects.get(name_key) or {}).get("parent") or "")
        if not parent_name:
            depths[name_key] = 0
        else:
            depths[name_key] = _depth(parent_name) + 1
        return depths[name_key]

    ordered_names = sorted(saved_objects.keys(), key=_depth)
    for snapshot_obj_name in ordered_names:
        obj = resolved.get(snapshot_obj_name)
        if not obj:
            continue
        obj_data = saved_objects.get(snapshot_obj_name) or {}
        saved_world = matrix_from_list(obj_data.get("matrix_world"))
        parent_name = str(obj_data.get("parent") or "")
        parent_obj = resolved.get(parent_name)
        parent_world = parent_obj.matrix_world.copy() if parent_obj else None
        local_matrix = compute_local_from_world(parent_world, saved_world)
        apply_local_matrix_to_object(obj, local_matrix)
        log_debug(f"snapshot restored using world transform: {snapshot_obj_name}")

    try:
        bpy.context.view_layer.update()
    except Exception:
        pass
    safe_set_frame(saved_frame if saved_frame is not None else original_frame)

    try:
        scene.umz_missing_objects_count = missing_objects_count
        scene.umz_missing_snapshot_name = name if missing_objects_count > 0 else ""
    except Exception:
        pass

    return {"restored_count": len(saved_objects), "missing_count": missing_objects_count}


# ---------------- UI / registration ----------------

class UMZ_SnapshotItem(bpy.types.PropertyGroup):
    name: StringProperty(name="name", default="")


class SNAPSHOT_UL_umz_list(bpy.types.UIList):
    bl_idname = "SNAPSHOT_UL_umz_list"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.label(text=item.name, icon='SCENE_DATA')

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        flt_flags = []
        flt_neworder = []
        filter_str = (self.filter_name or "").lower().strip()
        if not filter_str:
            flt_flags = [self.bitflag_filter_item] * len(items)
            return flt_flags, flt_neworder
        for it in items:
            nm = (getattr(it, "name", "") or "").lower()
            flt_flags.append(self.bitflag_filter_item if filter_str in nm else 0)
        return flt_flags, flt_neworder


def snapshot_items(self, context):
    snaps = get_cached_snapshots()
    items = [(n, n, "") for n in snaps.keys()]
    if not items:
        items = [("", "(нет снимков)", "")]
    return items


def _on_snapshot_list_index_changed(self, context):
    sc = self
    try:
        idx = int(sc.umz_snapshot_list_index)
    except Exception:
        return
    if idx < 0 or idx >= len(sc.umz_snapshot_list):
        return
    try:
        sc.umz_selected_snapshot = sc.umz_snapshot_list[idx].name
    except Exception:
        pass


def _rebuild_snapshot_list(scene, prefer_name=""):
    snaps = get_cached_snapshots() or {}
    names = sorted(list(snaps.keys()))
    prev = prefer_name.strip() if prefer_name else (getattr(scene, 'umz_selected_snapshot', '') or '')
    try:
        prev_idx = int(getattr(scene, 'umz_snapshot_list_index', 0))
    except Exception:
        prev_idx = 0
    lst = scene.umz_snapshot_list
    lst.clear()
    for n in names:
        it = lst.add()
        it.name = n
    if not names:
        scene.umz_snapshot_list_index = 0
        scene.umz_selected_snapshot = ""
        return
    idx = names.index(prev) if prev in names else max(0, min(prev_idx, len(names) - 1))
    scene.umz_snapshot_list_index = idx
    scene.umz_selected_snapshot = names[idx]


def clear_external_folder_pref():
    addon = __name__.split('.')[0]
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


def register_scene_props():
    if not hasattr(bpy.types.Scene, "umz_selected_snapshot"):
        bpy.types.Scene.umz_selected_snapshot = EnumProperty(name="Снимок", items=snapshot_items)
    if not hasattr(bpy.types.Scene, "umz_snapshot_list"):
        bpy.types.Scene.umz_snapshot_list = bpy.props.CollectionProperty(type=UMZ_SnapshotItem)
    if not hasattr(bpy.types.Scene, "umz_snapshot_list_index"):
        bpy.types.Scene.umz_snapshot_list_index = IntProperty(default=0, update=_on_snapshot_list_index_changed)
    if not hasattr(bpy.types.Scene, "umz_missing_objects_count"):
        bpy.types.Scene.umz_missing_objects_count = IntProperty(name="Потерянные объекты", default=0)
    if not hasattr(bpy.types.Scene, "umz_missing_snapshot_name"):
        bpy.types.Scene.umz_missing_snapshot_name = StringProperty(name="Снимок с потерями", default="")
    if not hasattr(bpy.types.Scene, "umz_snapshot_keep_new"):
        bpy.types.Scene.umz_snapshot_keep_new = BoolProperty(name="Не удалять новое", default=False)
    if not hasattr(bpy.types.Scene, "umz_ui_snapshot_open"):
        bpy.types.Scene.umz_ui_snapshot_open = BoolProperty(name="Сохранение сцен", default=True)


def unregister_scene_props():
    for prop in (
        "umz_selected_snapshot", "umz_snapshot_list", "umz_snapshot_list_index",
        "umz_missing_objects_count", "umz_missing_snapshot_name", "umz_snapshot_keep_new",
        "umz_ui_snapshot_open",
    ):
        if hasattr(bpy.types.Scene, prop):
            try:
                delattr(bpy.types.Scene, prop)
            except Exception:
                pass


def format_created(timestamp):
    try:
        dt = datetime.fromisoformat(timestamp)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(timestamp)


class SNAPSHOT_OT_save(bpy.types.Operator):
    bl_idname = "umz.snapshot_save"
    bl_label = "Сохранить сцену"
    name: StringProperty(name="Имя", default="snapshot1")

    def execute(self, context):
        try:
            collect_snapshot(self.name)
            _rebuild_snapshot_list(context.scene, prefer_name=self.name)
            context.scene.umz_selected_snapshot = self.name
        except Exception as e:
            self.report({'ERROR'}, f"Ошибка при сохранении снимка: {e}")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Снимок '{self.name}' сохранён.")
        return {'FINISHED'}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)


class SNAPSHOT_OT_set_dir(bpy.types.Operator):
    bl_idname = "umz.snapshot_set_directory"
    bl_label = "Папка для сцен"
    filepath: StringProperty(subtype='FILE_PATH', default="")

    def execute(self, context):
        addon = __name__.split('.')[0]
        try:
            prefs = bpy.context.preferences.addons[addon].preferences
            if self.filepath:
                prefs.external_snapshots_folder = os.path.dirname(self.filepath)
                self.report({'INFO'}, f"Папка сцен: {prefs.external_snapshots_folder}")
            else:
                self.report({'WARNING'}, "Путь не задан.")
        except Exception as e:
            self.report({'ERROR'}, f"{e}")
            return {'CANCELLED'}
        global SNAPSHOT_CACHE_DIRTY
        SNAPSHOT_CACHE_DIRTY = True
        _rebuild_snapshot_list(context.scene)
        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


class SNAPSHOT_OT_clear_dir(bpy.types.Operator):
    bl_idname = "umz.snapshot_clear_directory"
    bl_label = "Очистить папку"

    def execute(self, context):
        ok = clear_external_folder_pref()
        if not ok:
            self.report({'ERROR'}, "Не удалось очистить настройку папки.")
            return {'CANCELLED'}
        global SNAPSHOT_CACHE_DIRTY
        SNAPSHOT_CACHE_DIRTY = True
        _rebuild_snapshot_list(context.scene)
        return {'FINISHED'}


class SNAPSHOT_OT_refresh_list(bpy.types.Operator):
    bl_idname = "umz.snapshot_refresh_list"
    bl_label = "Синхронизировать"

    def execute(self, context):
        global SNAPSHOT_CACHE_DIRTY
        SNAPSHOT_CACHE_DIRTY = True
        _rebuild_snapshot_list(context.scene)
        return {'FINISHED'}


class SNAPSHOT_OT_load_delete(bpy.types.Operator):
    bl_idname = "umz.snapshot_load_delete"
    bl_label = "Загрузить/Удалить"
    snapshot: StringProperty()
    do_delete: BoolProperty(default=False)

    def execute(self, context):
        if self.do_delete:
            ok = delete_snapshot(self.snapshot)
            if ok:
                _rebuild_snapshot_list(context.scene)
                all_names = list(get_cached_snapshots().keys())
                try:
                    context.scene.umz_selected_snapshot = all_names[0] if all_names else ""
                except Exception:
                    pass
                self.report({'INFO'}, f"Снимок '{self.snapshot}' удалён.")
                return {'FINISHED'}
            self.report({'ERROR'}, "Не найден.")
            return {'CANCELLED'}

        keep_new = getattr(context.scene, "umz_snapshot_keep_new", False)
        try:
            res = restore_snapshot(self.snapshot, delete_added=not keep_new, restore_materials=True)
            missing = res.get("missing_count", 0)
            if missing > 0:
                self.report({'WARNING'}, f"Снимок '{self.snapshot}' загружен. Потерянных объектов: {missing}")
            else:
                self.report({'INFO'}, f"Снимок '{self.snapshot}' загружен.")
        except Exception as e:
            self.report({'ERROR'}, f"Ошибка при загрузке снимка: {e}")
            return {'CANCELLED'}
        return {'FINISHED'}


class SNAPSHOT_OT_find_missing_from_blend(bpy.types.Operator):
    bl_idname = "umz.snapshot_find_missing_from_blend"
    bl_label = "Найти потерянные объекты в .blend"
    filepath: StringProperty(subtype='FILE_PATH', default="")

    def execute(self, context):
        scene = context.scene
        blend_path = bpy.path.abspath(self.filepath) if self.filepath else ""
        if not blend_path or not os.path.isfile(blend_path):
            self.report({'WARNING'}, "Файл .blend не выбран или не существует.")
            return {'CANCELLED'}

        snaps = get_cached_snapshots()
        snap_name = getattr(scene, "umz_selected_snapshot", "")
        snapshot = snaps.get(snap_name)
        if not snapshot:
            self.report({'ERROR'}, "Снимок для восстановления не найден.")
            return {'CANCELLED'}

        saved_objects = snapshot.get("objects", {})
        restored = 0
        for obj_name, obj_data in saved_objects.items():
            missing_obj = None
            for candidate in bpy.data.objects:
                if candidate.get("umz_missing_original_name") == obj_name:
                    missing_obj = candidate
                    break
            if not missing_obj:
                continue
            data_block = _load_data_from_external_blend(obj_data.get("type"), obj_data.get("data_name"), blend_path)
            if not data_block:
                continue
            try:
                new_obj = bpy.data.objects.new(obj_name, data_block)
            except Exception:
                continue
            try:
                if missing_obj.users_collection:
                    for coll in missing_obj.users_collection:
                        coll.objects.link(new_obj)
                else:
                    scene.collection.objects.link(new_obj)
            except Exception:
                try:
                    scene.collection.objects.link(new_obj)
                except Exception:
                    continue
            try:
                bpy.data.objects.remove(missing_obj, do_unlink=True)
            except Exception:
                pass
            restored += 1

        if restored > 0:
            scene.umz_missing_objects_count = max(0, int(getattr(scene, "umz_missing_objects_count", 0)) - restored)
            if int(getattr(scene, "umz_missing_objects_count", 0)) == 0:
                scene.umz_missing_snapshot_name = ""
            self.report({'INFO'}, f"Восстановлено объектов: {restored}")
        else:
            self.report({'INFO'}, "Совпадающих объектов в выбранном .blend не найдено.")
        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


def draw_ui(layout, context):
    scene = context.scene
    header = layout.row(align=True)
    is_open = bool(getattr(scene, "umz_ui_snapshot_open", True))
    icon = 'TRIA_DOWN' if is_open else 'TRIA_RIGHT'
    header.prop(scene, "umz_ui_snapshot_open", text="Сохранение сцен", icon=icon, emboss=False)
    if not is_open:
        return

    box = layout.box()
    addon = __name__.split('.')[0]
    prefs = None
    try:
        prefs = bpy.context.preferences.addons[addon].preferences
    except Exception:
        pass
    folder = bpy.path.abspath(getattr(prefs, 'external_snapshots_folder', '')) if (prefs and getattr(prefs, 'external_snapshots_folder', '')) else ""
    row_dir = box.row(align=True)
    if folder:
        row_dir.label(text=folder, icon='FILE_FOLDER')
        row_dir.operator("umz.snapshot_set_directory", text="", icon='FILE_FOLDER')
        row_dir.operator("umz.snapshot_clear_directory", text="", icon='X')
    else:
        row_dir.scale_y = 1.4
        row_dir.operator("umz.snapshot_set_directory", text="Папка для сцен", icon='FILE_FOLDER')
    settings_box = box.box()
    settings_box.prop(scene, "umz_snapshot_keep_new", text="Не удалять новое")

    try:
        _rebuild_snapshot_list(scene, prefer_name=getattr(scene, 'umz_selected_snapshot', ''))
    except Exception:
        pass

    row = box.row()
    row.template_list("SNAPSHOT_UL_umz_list", "", scene, "umz_snapshot_list", scene, "umz_snapshot_list_index", rows=6)
    col_ops = row.column(align=True)
    col_ops.operator("umz.snapshot_save", text="", icon='FILE_TICK')
    op_load = col_ops.operator("umz.snapshot_load_delete", text="", icon='IMPORT')
    op_load.snapshot = getattr(scene, 'umz_selected_snapshot', '')
    op_load.do_delete = False
    op_del = col_ops.operator("umz.snapshot_load_delete", text="", icon='TRASH')
    op_del.snapshot = getattr(scene, 'umz_selected_snapshot', '')
    op_del.do_delete = True
    col_ops.separator()
    col_ops.operator("umz.snapshot_refresh_list", text="", icon='FILE_REFRESH')

    snaps = get_cached_snapshots()
    snap_data = snaps.get(getattr(scene, 'umz_selected_snapshot', '')) if snaps else None
    if snap_data:
        box.label(text=f"Создано: {format_created(snap_data.get('created_at', ''))}")

    missing_count = getattr(scene, "umz_missing_objects_count", 0)
    missing_for = getattr(scene, "umz_missing_snapshot_name", "")
    current_snap = getattr(scene, "umz_selected_snapshot", "")
    if missing_count > 0 and missing_for and (missing_for == current_snap):
        warn_box = box.box()
        row = warn_box.row(align=True)
        row.label(text=f"Потерянные объекты: {missing_count}", icon='ERROR')
        row.operator("umz.snapshot_find_missing_from_blend", text="Найти в .blend", icon='FILE_FOLDER')


classes = (
    UMZ_SnapshotItem,
    SNAPSHOT_UL_umz_list,
    SNAPSHOT_OT_save,
    SNAPSHOT_OT_set_dir,
    SNAPSHOT_OT_clear_dir,
    SNAPSHOT_OT_refresh_list,
    SNAPSHOT_OT_load_delete,
    SNAPSHOT_OT_find_missing_from_blend,
)

_registered = False
_register_cb = None


def register(register_callback=None):
    global _registered, _register_cb, SNAPSHOT_CACHE_DIRTY
    if _registered:
        return
    for c in classes:
        bpy.utils.register_class(c)
    register_scene_props()
    SNAPSHOT_CACHE_DIRTY = True
    try:
        _rebuild_snapshot_list(bpy.context.scene)
    except Exception:
        pass
    if register_callback is not None:
        _register_cb = register_callback
        try:
            register_callback({
                "id": MODULE_ID,
                "name": MODULE_NAME,
                "draw": draw_ui,
                "register": register,
                "unregister": unregister,
            })
        except Exception:
            pass
    _registered = True


def unregister():
    global _registered, _register_cb, SNAPSHOT_CACHE
    if not _registered:
        return
    for c in reversed(classes):
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass
    unregister_scene_props()
    SNAPSHOT_CACHE = {}
    _registered = False
    _register_cb = None
