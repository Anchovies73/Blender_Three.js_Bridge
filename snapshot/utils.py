import bpy
import os
from datetime import datetime
from mathutils import Matrix


def safe_set_frame(frame: int):
    try:
        bpy.context.scene.frame_set(int(frame))
    except Exception:
        pass


def get_scene_objects():
    return list(bpy.context.scene.objects)


def matrix_to_list(m: Matrix):
    return [list(row) for row in m]


def matrix_from_list(data):
    try:
        return Matrix(data)
    except Exception:
        return None


def format_created(timestamp):
    try:
        dt = datetime.fromisoformat(timestamp)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        try:
            if "T" in timestamp:
                base = timestamp.split("T")[0]
                timepart = timestamp.split("T")[1].split(".")[0]
                return f"{base} {timepart}"
        except Exception:
            pass
    return str(timestamp)


def find_data_in_current_blend(obj_type, data_name):
    if not data_name:
        return None
    try:
        if obj_type == 'MESH':
            return bpy.data.meshes.get(data_name)
        if obj_type == 'CURVE':
            return bpy.data.curves.get(data_name)
        if obj_type == 'ARMATURE':
            return bpy.data.armatures.get(data_name)
        if obj_type == 'LATTICE':
            return bpy.data.lattices.get(data_name)
        if obj_type == 'CAMERA':
            return bpy.data.cameras.get(data_name)
        if obj_type == 'LIGHT':
            return bpy.data.lights.get(data_name)
        if obj_type == 'GREASEPENCIL':
            gp_coll = getattr(bpy.data, "grease_pencils", None)
            return gp_coll.get(data_name) if gp_coll else None

        for coll_name in (
            "meshes", "curves", "armatures", "lattices",
            "cameras", "lights", "grease_pencils"
        ):
            coll = getattr(bpy.data, coll_name, None)
            if coll is None:
                continue
            candidate = coll.get(data_name)
            if candidate is not None:
                return candidate
    except Exception:
        pass
    return None


def load_data_from_external_blend(obj_type, data_name, blend_path):
    if not data_name or not blend_path or not os.path.isfile(blend_path):
        return None

    type_map = {
        'MESH': ('meshes', 'meshes'),
        'CURVE': ('curves', 'curves'),
        'ARMATURE': ('armatures', 'armatures'),
        'LATTICE': ('lattices', 'lattices'),
        'CAMERA': ('cameras', 'cameras'),
        'LIGHT': ('lights', 'lights'),
        'GREASEPENCIL': ('grease_pencils', 'grease_pencils'),
    }
    entry = type_map.get(obj_type)
    if entry is None:
        return None

    lib_attr, bpy_collection = entry

    try:
        with bpy.data.libraries.load(blend_path, link=False) as (data_from, data_to):
            from_list = getattr(data_from, lib_attr, None)
            if not from_list or data_name not in from_list:
                return None
            setattr(data_to, lib_attr, [data_name])
    except Exception:
        return None

    try:
        coll = getattr(bpy.data, bpy_collection, None)
        if coll is None:
            return None
        return coll.get(data_name)
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

    try:
        for c in list(obj.users_collection):
            if c.name not in coll_names:
                try:
                    c.objects.unlink(obj)
                except Exception:
                    pass
    except Exception:
        pass
