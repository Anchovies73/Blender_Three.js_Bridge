import bpy
from datetime import datetime
from mathutils import Matrix

from . import storage
from .utils import (
    get_scene_objects,
    safe_set_frame,
    matrix_to_list,
    find_data_in_current_blend,
    load_data_from_external_blend,
    link_object_to_collections,
)


def collect_snapshot(name: str):
    scene = bpy.context.scene
    objs = get_scene_objects()
    try:
        scene_frame = scene.frame_current
    except Exception:
        scene_frame = None

    snapshot = {
        "scene_name": scene.name,
        "created_at": datetime.now().isoformat(),
        "scene_frame": scene_frame,
        "objects": {},
        "materials": {},
    }

    mats_seen = {}

    for obj in objs:
        obj_data = {
            "name": obj.name,
            "type": obj.type,
            "parent": obj.parent.name if obj.parent else None,
            "parent_type": getattr(obj, "parent_type", None),
            "parent_bone": getattr(obj, "parent_bone", None),
            "collections": [],
            "material_slots": [],
            "data_name": getattr(obj.data, "name", None) if getattr(obj, "data", None) else None,
        }

        obj_data["location"] = list(obj.location)
        obj_data["scale"] = list(obj.scale)
        obj_data["rotation_mode"] = obj.rotation_mode
        if obj.rotation_mode == 'QUATERNION':
            obj_data["rotation_quaternion"] = list(obj.rotation_quaternion)
            obj_data["rotation_euler"] = None
        else:
            obj_data["rotation_euler"] = list(obj.rotation_euler)
            obj_data["rotation_quaternion"] = None

        try:
            mpi = obj.matrix_parent_inverse.copy()
            obj_data["matrix_parent_inverse"] = matrix_to_list(mpi)
        except Exception:
            obj_data["matrix_parent_inverse"] = None

        obj_data["matrix_world"] = matrix_to_list(obj.matrix_world)

        for coll in obj.users_collection:
            obj_data["collections"].append(coll.name)

        for ms in obj.material_slots:
            if ms.material:
                mname = ms.material.name
                obj_data["material_slots"].append(mname)
                if mname not in mats_seen:
                    mats_seen[mname] = {
                        "name": mname,
                        "use_nodes": ms.material.use_nodes,
                    }

        snapshot["objects"][obj.name] = obj_data

    snapshot["materials"] = list(mats_seen.values())

    internal_raw = storage.read_internal_snapshots()
    internal_raw[name] = snapshot
    storage.write_internal_snapshots(internal_raw)
    storage.write_snapshot_to_file(name, snapshot)
    storage.mark_cache_dirty()

    try:
        bpy.context.scene.umz_selected_snapshot = name
    except Exception:
        pass
    return True


def delete_snapshot(snapshot_name):
    internal_raw = storage.read_internal_snapshots()
    removed = False
    if snapshot_name in internal_raw:
        del internal_raw[snapshot_name]
        storage.write_internal_snapshots(internal_raw)
        removed = True

    if storage.remove_snapshot_file(snapshot_name):
        removed = True or removed

    storage.mark_cache_dirty()

    scene = bpy.context.scene
    all_names = list(storage.get_cached_snapshots().keys())
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


def restore_snapshot(name: str, delete_added: bool = True, restore_materials: bool = True):
    scene = bpy.context.scene

    try:
        original_frame = scene.frame_current
    except Exception:
        original_frame = 0

    snapshot = storage.get_cached_snapshots().get(name)
    if not snapshot:
        return {"error": "Снимок не найден"}

    saved_objects = snapshot.get("objects", {})
    saved_mats = {m["name"]: m for m in snapshot.get("materials", [])}
    saved_frame = snapshot.get("scene_frame", None)

    if saved_frame is not None:
        safe_set_frame(saved_frame)
    else:
        safe_set_frame(0)

    if restore_materials:
        for mat_info in saved_mats.values():
            try:
                if bpy.data.materials.get(mat_info["name"]) is None:
                    bpy.data.materials.new(mat_info["name"])
            except Exception:
                pass

    current_names = {o.name for o in scene.objects}
    saved_names = set(saved_objects.keys())
    extra = current_names - saved_names
    if delete_added and extra:
        for n in list(extra):
            o = bpy.data.objects.get(n)
            if o:
                for c in list(o.users_collection):
                    try:
                        c.objects.unlink(o)
                    except Exception:
                        pass
                try:
                    bpy.data.objects.remove(o, do_unlink=True)
                except Exception:
                    pass

    missing_objects_count = 0
    actual_name_map = {}

    def resolve_scene_object(saved_name):
        if not saved_name:
            return None
        actual_name = actual_name_map.get(saved_name, saved_name)
        return bpy.data.objects.get(actual_name)

    def make_unique_missing_name(base_name):
        candidate = f"{base_name}__Missing"
        if bpy.data.objects.get(candidate) is None:
            return candidate
        i = 1
        while True:
            nm = f"{candidate}.{i:03d}"
            if bpy.data.objects.get(nm) is None:
                return nm
            i += 1

    for obj_name, obj_data in saved_objects.items():
        existing = bpy.data.objects.get(obj_name)
        if existing:
            actual_name_map[obj_name] = existing.name
            continue

        obj_type = obj_data.get("type")
        data_name = obj_data.get("data_name")
        data_block = find_data_in_current_blend(obj_type, data_name) if data_name else None

        try:
            if data_block is not None:
                new_obj = bpy.data.objects.new(obj_name, data_block)
            else:
                missing_objects_count += 1
                missing_name = make_unique_missing_name(obj_name)
                new_obj = bpy.data.objects.new(missing_name, None)
                try:
                    new_obj.empty_display_type = 'PLAIN_AXES'
                    new_obj.empty_display_size = 0.25
                except Exception:
                    pass
                try:
                    new_obj["umz_missing_from_snapshot"] = True
                    new_obj["umz_missing_original_name"] = obj_name
                    if data_name:
                        new_obj["umz_missing_data_name"] = data_name
                    if obj_type:
                        new_obj["umz_missing_object_type"] = obj_type
                except Exception:
                    pass
        except Exception:
            continue

        try:
            scene.collection.objects.link(new_obj)
            actual_name_map[obj_name] = new_obj.name
        except Exception:
            try:
                bpy.data.objects.remove(new_obj, do_unlink=True)
            except Exception:
                pass
            continue

    parent_map = {}
    parent_type_map = {}
    parent_bone_map = {}
    collections_map = {}
    material_slots_map = {}
    loc_map = {}
    rot_mode_map = {}
    rot_euler_map = {}
    rot_quat_map = {}
    scale_map = {}
    world_map = {}

    for oname, d in saved_objects.items():
        parent_map[oname] = d.get("parent")
        parent_type_map[oname] = d.get("parent_type")
        parent_bone_map[oname] = d.get("parent_bone")
        collections_map[oname] = d.get("collections", [])
        material_slots_map[oname] = d.get("material_slots", [])
        loc_map[oname] = d.get("location")
        rot_mode_map[oname] = d.get("rotation_mode")
        rot_euler_map[oname] = d.get("rotation_euler")
        rot_quat_map[oname] = d.get("rotation_quaternion")
        scale_map[oname] = d.get("scale")

        mw_list = d.get("matrix_world")
        try:
            world_map[oname] = Matrix(mw_list) if mw_list else None
        except Exception:
            world_map[oname] = None

    for obj_name, coll_names in collections_map.items():
        obj = resolve_scene_object(obj_name)
        if obj:
            link_object_to_collections(obj, coll_names)

    if restore_materials:
        for obj_name, ms in material_slots_map.items():
            obj = resolve_scene_object(obj_name)
            if not obj:
                continue
            try:
                while len(getattr(obj, "material_slots", [])) < len(ms):
                    if getattr(obj, "data", None) and hasattr(obj.data, "materials"):
                        obj.data.materials.append(None)
                    else:
                        break
            except Exception:
                pass
            for idx, mname in enumerate(ms):
                mat = bpy.data.materials.get(mname)
                try:
                    if idx < obj.material_slots.__len__():
                        obj.material_slots[idx].material = mat
                    else:
                        if obj.data and hasattr(obj.data, "materials"):
                            obj.data.materials.append(mat)
                except Exception:
                    pass

    for obj_name, parent_name in parent_map.items():
        obj = resolve_scene_object(obj_name)
        if not obj:
            continue

        p_type = parent_type_map.get(obj_name)
        p_bone = parent_bone_map.get(obj_name)

        if not parent_name:
            try:
                obj.parent = None
                obj.parent_type = 'OBJECT'
                obj.parent_bone = ""
            except Exception:
                pass
            continue

        parent = resolve_scene_object(parent_name)
        if not parent:
            try:
                obj.parent = None
                obj.parent_type = 'OBJECT'
                obj.parent_bone = ""
            except Exception:
                pass
            continue

        if p_type == 'BONE' and p_bone and hasattr(parent, 'pose') and parent.pose:
            try:
                obj.parent = parent
                obj.parent_type = 'BONE'
                obj.parent_bone = p_bone
            except Exception:
                pass
        else:
            try:
                obj.parent = parent
                obj.parent_type = 'OBJECT'
                obj.parent_bone = ""
            except Exception:
                pass

    level_map = {}

    def compute_depth(obj_name):
        if obj_name in level_map:
            return level_map[obj_name]
        parent_name = parent_map.get(obj_name)
        if not parent_name:
            depth = 0
        else:
            depth = compute_depth(parent_name) + 1
        level_map[obj_name] = depth
        return depth

    for obj_name in saved_objects.keys():
        compute_depth(obj_name)

    depth_to_names = {}
    max_depth = 0
    for obj_name, depth in level_map.items():
        depth_to_names.setdefault(depth, []).append(obj_name)
        max_depth = max(max_depth, depth)

    def apply_locals(obj, obj_name):
        loc = loc_map.get(obj_name)
        rot_mode = rot_mode_map.get(obj_name)
        rot_euler = rot_euler_map.get(obj_name)
        rot_quat = rot_quat_map.get(obj_name)
        sca = scale_map.get(obj_name)

        if rot_mode:
            try:
                obj.rotation_mode = rot_mode
            except Exception:
                pass

        if loc is not None:
            try:
                obj.location = loc
            except Exception:
                pass
        if sca is not None:
            try:
                obj.scale = sca
            except Exception:
                pass
        try:
            if rot_mode == 'QUATERNION' and rot_quat is not None:
                obj.rotation_quaternion = rot_quat
            elif rot_euler is not None:
                obj.rotation_euler = rot_euler
        except Exception:
            pass

    for obj_name in depth_to_names.get(0, []):
        obj = resolve_scene_object(obj_name)
        if not obj:
            continue
        mw = world_map.get(obj_name)
        if mw is not None:
            try:
                obj.matrix_world = mw
            except Exception:
                pass
        apply_locals(obj, obj_name)

    for depth in range(1, max_depth + 1):
        names = depth_to_names.get(depth, [])
        if not names:
            continue
        for obj_name in names:
            obj = resolve_scene_object(obj_name)
            if not obj:
                continue

            parent_name = parent_map.get(obj_name)
            parent = resolve_scene_object(parent_name) if parent_name else None
            mw_child_saved = world_map.get(obj_name)
            mw_parent_saved = world_map.get(parent_name) if parent_name else None

            apply_locals(obj, obj_name)

            if parent and mw_child_saved is not None and mw_parent_saved is not None:
                try:
                    B = obj.matrix_basis.copy()
                    M_p = parent.matrix_world.copy()
                    M_c = mw_child_saved
                    M_pi = M_p.inverted() @ M_c @ B.inverted()
                    obj.matrix_parent_inverse = M_pi
                    obj.matrix_world = M_c
                except Exception:
                    try:
                        obj.matrix_world = mw_child_saved
                    except Exception:
                        pass
            else:
                if mw_child_saved is not None:
                    try:
                        obj.matrix_world = mw_child_saved
                    except Exception:
                        pass

    try:
        bpy.context.view_layer.update()
    except Exception:
        pass

    if saved_frame is not None:
        safe_set_frame(saved_frame)
    else:
        safe_set_frame(original_frame)

    try:
        scene.umz_missing_objects_count = missing_objects_count
        scene.umz_missing_snapshot_name = name
    except Exception:
        pass

    return {
        "restored_count": len(saved_objects),
        "missing_count": missing_objects_count,
    }


def find_missing_from_blend(snapshot_name: str, blend_path: str):
    scene = bpy.context.scene
    if not blend_path:
        return 0

    snapshot = storage.get_cached_snapshots().get(snapshot_name)
    if not snapshot:
        return 0

    saved_objects = snapshot.get("objects", {})
    restored = 0

    for obj_name, obj_data in saved_objects.items():
        data_name = obj_data.get("data_name")
        obj_type = obj_data.get("type")

        if bpy.data.objects.get(obj_name):
            continue

        missing_obj = None
        for candidate in bpy.data.objects:
            try:
                if not bool(candidate.get("umz_missing_from_snapshot", False)):
                    continue
                if str(candidate.get("umz_missing_original_name", "")) != obj_name:
                    continue
                missing_obj = candidate
                break
            except Exception:
                continue

        if not missing_obj or not data_name:
            continue

        data_block = load_data_from_external_blend(obj_type, data_name, blend_path)
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
            new_obj.matrix_world = missing_obj.matrix_world.copy()
        except Exception:
            pass
        try:
            new_obj.parent = missing_obj.parent
            new_obj.parent_type = missing_obj.parent_type
            new_obj.parent_bone = missing_obj.parent_bone
            new_obj.matrix_parent_inverse = missing_obj.matrix_parent_inverse.copy()
        except Exception:
            pass

        try:
            for c in list(missing_obj.users_collection):
                c.objects.unlink(missing_obj)
            bpy.data.objects.remove(missing_obj, do_unlink=True)
        except Exception:
            pass

        restored += 1

    if restored > 0:
        try:
            current_missing = getattr(scene, "umz_missing_objects_count", 0)
            new_missing = max(0, current_missing - restored)
            scene.umz_missing_objects_count = new_missing
            if new_missing == 0:
                scene.umz_missing_snapshot_name = ""
        except Exception:
            pass

    try:
        bpy.context.view_layer.update()
    except Exception:
        pass

    return restored
