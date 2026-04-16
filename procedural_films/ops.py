import bpy
import json
import os
from datetime import datetime
from mathutils import Vector, Euler, Quaternion

from .constants import GLTF_ID_PROP

from .storage import (
    read_internal_films,
    write_internal_films,
    write_animation_to_file,
    remove_animation_file,
    read_all_films_cached,
    read_external_films,
    mark_cache_dirty,
    get_external_folder,
)
from .blender_codec import (
    serialize_nla_for_object,
    deserialize_nla_for_object,
    deserialize_action,
    pushdown_action_to_nla,
    nla_has_transform_curves,
    META_KEYS_ORDER,  # нужен список ключей meta
)
from .worldspace import (
    TRANSFORM_SCHEMA_VERSION,
    build_world_aware_animation_track,
    build_track_lookup,
    apply_local_samples_to_object,
    repair_world_animation_track,
    detect_world_conflicts_for_entry,
)
from .three_export import (
    build_three_clip_from_saved_entry,
    write_three_animation_to_file,
)
from .text_utils import (
    read_active_text,
    write_active_text,
)

# =========================================================
# Materials -> Custom Properties for three.js
# - umz_base_color: [r,g,b]
# - umz_shader: dict with procedural shader params (generated->mapping->colorramp->principled base color)
# - umz_mix_mode: string with Mix blend mode for Principled(BaseColor) chain
#   examples: 'MULTIPLY_ALPHA', 'MIX_ALPHA', 'ADD_ALPHA', 'MULTIPLY'
# - umz_mix_factor: float if Mix Factor is NOT linked and uses its local default value
# =========================================================

MATERIAL_RGB_CP_KEY = "umz_base_color"      # material.userData.umz_base_color in three.js
MATERIAL_SHADER_CP_KEY = "umz_shader"       # material.userData.umz_shader in three.js
MATERIAL_MIX_MODE_CP_KEY = "umz_mix_mode"   # material.userData.umz_mix_mode in three.js
MATERIAL_MIX_FACTOR_CP_KEY = "umz_mix_factor"  # material.userData.umz_mix_factor in three.js

MIX_MODE_MIX_ALPHA = "MIX_ALPHA"
MIX_MODE_MULTIPLY_ALPHA = "MULTIPLY_ALPHA"

# Per-object outline blink rules (stored in Blender Custom Properties)
OUTLINE_RULES_JSON_CP_KEY = "umz_outline_blink_rules_json"
OUTLINE_RULE_ENABLED_CP_KEY = "umz_outline_blink_enabled"  # legacy single-rule support
OUTLINE_RULE_FRAME_CP_KEY = "umz_outline_blink_frame"  # legacy single-rule support
OUTLINE_RULE_TIME_CP_KEY = "umz_outline_blink_time_sec"  # legacy single-rule support
OUTLINE_RULE_DURATION_CP_KEY = "umz_outline_blink_duration_sec"  # legacy single-rule support
OUTLINE_RULE_INTERVAL_CP_KEY = "umz_outline_blink_interval_sec"  # legacy single-rule support
OUTLINE_RULE_DEFAULT_DURATION_SEC = 4.0
OUTLINE_RULE_DEFAULT_INTERVAL_SEC = 1.0

_SUPPORTED_MIX_BLEND_TYPES = {
    "MIX",
    "MULTIPLY",
    "ADD",
    "SUBTRACT",
    "DIVIDE",
    "SCREEN",
    "OVERLAY",
    "SOFT_LIGHT",
    "LINEAR_LIGHT",
    "DIFFERENCE",
    "DARKEN",
    "LIGHTEN",
    "COLOR_DODGE",
    "COLOR_BURN",
    "HUE",
    "SATURATION",
    "VALUE",
    "COLOR",
}


# -------------------------
# Color extraction (RGB / Principled / diffuse fallback)
# -------------------------

def _material_has_rgb_node(mat: bpy.types.Material) -> bool:
    try:
        if not mat or not mat.use_nodes or not mat.node_tree:
            return False
        for n in mat.node_tree.nodes:
            if n and n.type == 'RGB':
                return True
    except Exception:
        return False
    return False


def _get_first_rgb_node_color(mat: bpy.types.Material):
    """
    Returns (r,g,b) in 0..1 floats or None.
    Deterministic: first RGB node in node list order.
    """
    try:
        if not mat or not mat.use_nodes or not mat.node_tree:
            return None
        for n in mat.node_tree.nodes:
            if not n:
                continue
            if n.type == 'RGB':
                try:
                    v = n.outputs[0].default_value  # (r,g,b,a)
                    return (float(v[0]), float(v[1]), float(v[2]))
                except Exception:
                    try:
                        v = n.color
                        return (float(v[0]), float(v[1]), float(v[2]))
                    except Exception:
                        return None
    except Exception:
        return None
    return None


def _get_principled_base_color(mat: bpy.types.Material):
    try:
        if not mat or not mat.use_nodes or not mat.node_tree:
            return None
        for n in mat.node_tree.nodes:
            if not n:
                continue
            if n.type == "BSDF_PRINCIPLED":
                inp = None
                try:
                    inp = n.inputs.get("Base Color")
                except Exception:
                    inp = None
                if not inp:
                    return None
                try:
                    v = inp.default_value  # (r,g,b,a)
                    return (float(v[0]), float(v[1]), float(v[2]))
                except Exception:
                    return None
    except Exception:
        return None
    return None


def _get_material_diffuse_color(mat: bpy.types.Material):
    try:
        if not mat:
            return None
        v = getattr(mat, "diffuse_color", None)
        if v is None:
            return None
        return (float(v[0]), float(v[1]), float(v[2]))
    except Exception:
        return None


def _get_best_material_base_color(mat: bpy.types.Material):
    # 1) RGB node
    rgb = _get_first_rgb_node_color(mat)
    if rgb is not None:
        return rgb

    # 2) Principled base color
    p = _get_principled_base_color(mat)
    if p is not None:
        return p

    # 3) fallback
    return _get_material_diffuse_color(mat)


# -------------------------
# Shader pattern extraction: Generated -> Mapping -> ColorRamp -> Principled(BaseColor)
# -------------------------

def _node_link_from_input_socket(node, input_name: str):
    """
    Returns (from_node, from_socket_name) or (None, None)
    """
    try:
        sock = node.inputs.get(input_name)
        if not sock or not sock.is_linked or not sock.links:
            return (None, None)
        ln = sock.links[0]
        return (ln.from_node, ln.from_socket.name if ln.from_socket else None)
    except Exception:
        return (None, None)


def _is_mapping_node(node) -> bool:
    try:
        return bool(node and node.type == "MAPPING")
    except Exception:
        return False


def _is_texcoord_node(node) -> bool:
    try:
        return bool(node and node.type == "TEX_COORD")
    except Exception:
        return False


def _is_colorramp_node(node) -> bool:
    try:
        return bool(node and node.type == "VALTORGB")
    except Exception:
        return False


def _is_principled_node(node) -> bool:
    try:
        return bool(node and node.type == "BSDF_PRINCIPLED")
    except Exception:
        return False


def _extract_colorramp_data(ramp_node):
    """
    Returns dict ramp data or None.
    """
    if not _is_colorramp_node(ramp_node):
        return None
    try:
        cr = ramp_node.color_ramp
        if not cr:
            return None
        elems = []
        for el in cr.elements:
            c = el.color  # RGBA floats
            elems.append({
                "pos": float(el.position),
                "color": [float(c[0]), float(c[1]), float(c[2]), float(c[3])],
            })
        elems.sort(key=lambda e: e["pos"])
        return {
            "interpolation": str(cr.interpolation),
            "elements": elems,
        }
    except Exception:
        return None


def _extract_mapping_data(mapping_node):
    if not _is_mapping_node(mapping_node):
        return None
    try:
        t = getattr(mapping_node, "vector_type", None)  # 'POINT', 'TEXTURE', 'VECTOR'
        if not t:
            t = getattr(mapping_node, "type", None)
        t = str(t) if t is not None else "POINT"

        loc = mapping_node.inputs["Location"].default_value
        rot = mapping_node.inputs["Rotation"].default_value
        scl = mapping_node.inputs["Scale"].default_value

        # Blender Mapping rotation is in radians; store degrees for readability in json
        rot_deg = [
            float(rot[0] * 57.29577951308232),
            float(rot[1] * 57.29577951308232),
            float(rot[2] * 57.29577951308232),
        ]

        return {
            "type": t,
            "location": [float(loc[0]), float(loc[1]), float(loc[2])],
            "rotation_deg": rot_deg,
            "scale": [float(scl[0]), float(scl[1]), float(scl[2])],
        }
    except Exception:
        return None


def _extract_generated_colorramp_shader(mat: bpy.types.Material):
    """
    Tries to detect chain:
      TextureCoord(Generated) -> Mapping(Vector) -> ColorRamp(Fac) -> Principled(Base Color)

    Returns shader dict or None.
    """
    try:
        if not mat or not mat.use_nodes or not mat.node_tree:
            return None
        nodes = mat.node_tree.nodes
        if not nodes:
            return None

        for n in nodes:
            if not _is_principled_node(n):
                continue

            from_node, _from_sock = _node_link_from_input_socket(n, "Base Color")
            if not from_node or not _is_colorramp_node(from_node):
                continue
            ramp_node = from_node

            fac_from, _ = _node_link_from_input_socket(ramp_node, "Fac")
            if not fac_from or not _is_mapping_node(fac_from):
                continue
            mapping_node = fac_from

            vec_from, vec_from_socket_name = _node_link_from_input_socket(mapping_node, "Vector")
            if not vec_from or not _is_texcoord_node(vec_from):
                continue

            # Must be "Generated" output
            if vec_from_socket_name and str(vec_from_socket_name).lower() != "generated":
                continue

            mapping_data = _extract_mapping_data(mapping_node)
            ramp_data = _extract_colorramp_data(ramp_node)
            if not mapping_data or not ramp_data:
                continue

            # Vector -> Fac in Blender takes X component
            fac_axis = 0

            return {
                "type": "generated_colorramp",
                "coord": "GENERATED",
                "fac_axis": fac_axis,
                "mapping": mapping_data,
                "ramp": ramp_data,
            }
    except Exception:
        return None

    return None


# -------------------------
# NEW: detect Mix(Multiply) Factor from ImageTexture Alpha -> Principled(BaseColor)
# -------------------------

def _is_mix_node(node) -> bool:
    """
    Blender 'Mix' node in newer versions is usually type 'MIX'.
    Keep compatibility with legacy 'MIX_RGB' too.
    """
    try:
        return bool(node and node.type in ("MIX", "MIX_RGB"))
    except Exception:
        return False


def _is_image_texture_node(node) -> bool:
    try:
        return bool(node and node.type == "TEX_IMAGE")
    except Exception:
        return False


def _get_input_socket_any(node, names):
    for nm in names:
        try:
            s = node.inputs.get(nm)
            if s:
                return s
        except Exception:
            pass
    return None


def _get_linked_from_node(node, input_names):
    """
    Returns (from_node, from_socket_name) for first found linked input socket among input_names.
    """
    sock = _get_input_socket_any(node, input_names)
    if not sock:
        return (None, None)
    try:
        if not sock.is_linked or not sock.links:
            return (None, None)
        ln = sock.links[0]
        return (ln.from_node, ln.from_socket.name if ln.from_socket else None)
    except Exception:
        return (None, None)


def _get_mix_factor_socket(node):
    return _get_input_socket_any(node, ["Factor", "Fac", "F"])


def _get_unlinked_mix_factor_value(node):
    """
    Returns Mix Factor default value when the factor socket is not linked.
    Returns None if socket is linked/unavailable/unreadable.
    """
    sock = _get_mix_factor_socket(node)
    if not sock:
        return None
    try:
        if sock.is_linked:
            return None
    except Exception:
        return None
    try:
        return float(sock.default_value)
    except Exception:
        return None


def _get_mix_blend_type(node):
    try:
        bt = getattr(node, "blend_type", None)
        if bt is None:
            return None
        bt = str(bt).upper().strip()
        if not bt:
            return None
        return bt
    except Exception:
        return None


def _mix_node_is_supported(node) -> bool:
    bt = _get_mix_blend_type(node)
    return bool(bt and bt in _SUPPORTED_MIX_BLEND_TYPES)


def _find_principled_base_color_mix_node(mat: bpy.types.Material):
    """
    Returns Mix/MixRGB node when chain is:
      (...) -> Mix(*) -> Principled(Base Color)
    otherwise None.
    """
    try:
        if not mat or not mat.use_nodes or not mat.node_tree:
            return None
        nodes = mat.node_tree.nodes
        if not nodes:
            return None

        for n in nodes:
            if not _is_principled_node(n):
                continue

            from_node, _from_sock = _node_link_from_input_socket(n, "Base Color")
            if not from_node or not _is_mix_node(from_node):
                continue

            mix_node = from_node
            if not _mix_node_is_supported(mix_node):
                continue

            return mix_node
    except Exception:
        return None
    return None


def _extract_mix_mode(mat: bpy.types.Material):
    """
    Detect chain:
      (...) -> Mix(*) -> Principled(Base Color)

    If Mix Factor is linked directly from an Image Texture socket, returns:
      '<BLEND_TYPE>_ALPHA'

    Otherwise returns:
      '<BLEND_TYPE>'

    Examples: MULTIPLY_ALPHA, MIX_ALPHA, ADD, OVERLAY
    """
    try:
        mix_node = _find_principled_base_color_mix_node(mat)
        if not mix_node:
            return None

        blend_type = _get_mix_blend_type(mix_node)
        if not blend_type:
            return None

        fac_from, fac_from_sock = _get_linked_from_node(mix_node, ["Factor", "Fac", "F"])
        if fac_from and _is_image_texture_node(fac_from):
            sock_name = str(fac_from_sock or "").strip().lower()
            if sock_name in ("alpha", "a", ""):
                return f"{blend_type}_ALPHA"

        return blend_type

    except Exception:
        return None

    return None


def _extract_mix_factor_value(mat: bpy.types.Material):
    """
    Detect chain:
      (...) -> Mix(*) -> Principled(Base Color)

    If Mix Factor is NOT linked, returns its default float value.
    If Factor is linked (for example from a texture), returns None.
    """
    try:
        mix_node = _find_principled_base_color_mix_node(mat)
        if not mix_node:
            return None
        return _get_unlinked_mix_factor_value(mix_node)
    except Exception:
        return None
    return None


def umz_scene_has_materials_missing_shaders_cp() -> bool:
    """
    UI warning:
    - base_color missing for materials where we can compute it
    OR
    - shader pattern detected but umz_shader not written
    OR
    - supported Mix pattern detected but umz_mix_mode not written
    OR
    - Mix factor default value detected but umz_mix_factor not written
    """
    try:
        for mat in bpy.data.materials:
            if not mat:
                continue

            col = _get_best_material_base_color(mat)
            if col is not None and MATERIAL_RGB_CP_KEY not in mat.keys():
                return True

            shader = _extract_generated_colorramp_shader(mat)
            if shader is not None and MATERIAL_SHADER_CP_KEY not in mat.keys():
                return True

            mix_mode = _extract_mix_mode(mat)
            if mix_mode is not None and MATERIAL_MIX_MODE_CP_KEY not in mat.keys():
                return True

            mix_factor = _extract_mix_factor_value(mat)
            if mix_factor is not None and MATERIAL_MIX_FACTOR_CP_KEY not in mat.keys():
                return True

    except Exception:
        return False

    return False


class UMZ_OT_set_material_shaders_cp(bpy.types.Operator):
    bl_idname = "umz.set_material_shaders_cp"
    bl_label = "Установить shaders (materials)"
    bl_description = (
        "Записывает Custom Properties материалов для three.js:\n"
        "- umz_base_color=[r,g,b] (RGB node -> Principled Base Color -> diffuse fallback)\n"
        "- umz_shader={...} если распознана схема Generated->Mapping->ColorRamp->Principled(BaseColor)\n"
        "- umz_mix_mode='MULTIPLY_ALPHA' если распознана схема Mix(Multiply) с фактором из Alpha текстуры\n"
        "- umz_mix_factor=float если у Mix(Multiply) Factor не подключен и используется локальное значение"
    )
    def execute(self, context):
        wrote_base = 0
        wrote_shader = 0
        wrote_mix = 0
        wrote_mix_factor = 0
        failed = 0

        for mat in bpy.data.materials:
            if not mat:
                continue

            # 1) base color CP
            col = _get_best_material_base_color(mat)
            if col is not None:
                try:
                    mat[MATERIAL_RGB_CP_KEY] = [col[0], col[1], col[2]]
                    wrote_base += 1
                except Exception:
                    failed += 1

            # 2) shader CP (pattern)
            shader = _extract_generated_colorramp_shader(mat)
            if shader is not None:
                try:
                    mat[MATERIAL_SHADER_CP_KEY] = shader
                    wrote_shader += 1
                except Exception:
                    failed += 1

            # 3) mix mode CP (supported Mix node -> Principled Base Color)
            mix_mode = _extract_mix_mode(mat)
            if mix_mode is not None:
                try:
                    mat[MATERIAL_MIX_MODE_CP_KEY] = str(mix_mode)
                    wrote_mix += 1
                except Exception:
                    failed += 1

            # 4) mix factor CP (supported Mix node with unlinked local factor)
            mix_factor = _extract_mix_factor_value(mat)
            if mix_factor is not None:
                try:
                    mat[MATERIAL_MIX_FACTOR_CP_KEY] = float(mix_factor)
                    wrote_mix_factor += 1
                except Exception:
                    failed += 1

        self.report({'INFO'}, f"Готово: base_color={wrote_base}, shaders={wrote_shader}, mix_mode={wrote_mix}, mix_factor={wrote_mix_factor}, ошибок={failed}")
        return {'FINISHED'}


def umz_scene_has_materials_missing_mix_factor_cp() -> bool:
    """
    UI warning specifically for Mix materials whose local Factor
    value should be exported into CP, but is still missing.
    """
    try:
        for mat in bpy.data.materials:
            if not mat:
                continue

            mix_factor = _extract_mix_factor_value(mat)
            if mix_factor is not None and MATERIAL_MIX_FACTOR_CP_KEY not in mat.keys():
                return True
    except Exception:
        return False

    return False


# =========================================================
# Animation ops (original full, with patched meta apply)
# =========================================================

def _capture_timeline_markers(scene):
    try:
        markers = scene.timeline_markers
        result = []
        for m in markers:
            result.append({"name": m.name, "frame": int(m.frame)})
        result.sort(key=lambda x: x["frame"])
        return result
    except Exception:
        return []


def _restore_timeline_markers(scene, markers_data):
    try:
        scene.timeline_markers.clear()
        for m_data in markers_data:
            name = m_data.get("name", "")
            frame = m_data.get("frame", 1)
            scene.timeline_markers.new(name=name, frame=frame)
    except Exception:
        pass


def _capture_text_editor_content():
    try:
        return read_active_text()
    except Exception:
        return None


def _restore_text_editor_content(content):
    try:
        if content is not None:
            write_active_text(content)
    except Exception:
        pass


def _get_scene_fps(scene):
    try:
        fps = float(scene.render.fps) / float(scene.render.fps_base or 1.0)
        return fps if fps > 0 else 24.0
    except Exception:
        return 24.0


def _normalize_outline_rule_data(rule):
    if not isinstance(rule, dict):
        return None

    try:
        enabled = bool(rule.get("enabled", True))
    except Exception:
        enabled = True

    try:
        frame = int(round(float(rule.get("frame", 0))))
    except Exception:
        frame = 0

    try:
        time_sec = float(rule.get("time_sec", 0.0))
    except Exception:
        time_sec = 0.0

    try:
        duration_sec = float(rule.get("duration_sec", OUTLINE_RULE_DEFAULT_DURATION_SEC))
    except Exception:
        duration_sec = OUTLINE_RULE_DEFAULT_DURATION_SEC

    try:
        interval_sec = float(rule.get("interval_sec", OUTLINE_RULE_DEFAULT_INTERVAL_SEC))
    except Exception:
        interval_sec = OUTLINE_RULE_DEFAULT_INTERVAL_SEC

    if duration_sec <= 0.0:
        duration_sec = OUTLINE_RULE_DEFAULT_DURATION_SEC
    if interval_sec <= 0.0:
        interval_sec = OUTLINE_RULE_DEFAULT_INTERVAL_SEC

    return {
        "enabled": enabled,
        "frame": frame,
        "time_sec": time_sec,
        "duration_sec": duration_sec,
        "interval_sec": interval_sec,
    }


def _normalize_outline_rules_data(rules):
    if isinstance(rules, dict):
        rules = [rules]
    if not isinstance(rules, (list, tuple)):
        return []

    out = []
    for rule in rules:
        norm = _normalize_outline_rule_data(rule)
        if norm:
            out.append(norm)

    out.sort(key=lambda r: (float(r.get("time_sec", 0.0)), int(r.get("frame", 0))))
    return out


def _sync_legacy_outline_rule_cp(obj, rules):
    last_rule = rules[-1] if rules else None
    if not last_rule:
        for key in (
            OUTLINE_RULE_ENABLED_CP_KEY,
            OUTLINE_RULE_FRAME_CP_KEY,
            OUTLINE_RULE_TIME_CP_KEY,
            OUTLINE_RULE_DURATION_CP_KEY,
            OUTLINE_RULE_INTERVAL_CP_KEY,
        ):
            try:
                if key in obj.keys():
                    del obj[key]
            except Exception:
                pass
        return

    try:
        obj[OUTLINE_RULE_ENABLED_CP_KEY] = bool(last_rule.get("enabled", True))
        obj[OUTLINE_RULE_FRAME_CP_KEY] = int(last_rule.get("frame", 0))
        obj[OUTLINE_RULE_TIME_CP_KEY] = float(last_rule.get("time_sec", 0.0))
        obj[OUTLINE_RULE_DURATION_CP_KEY] = float(last_rule.get("duration_sec", OUTLINE_RULE_DEFAULT_DURATION_SEC))
        obj[OUTLINE_RULE_INTERVAL_CP_KEY] = float(last_rule.get("interval_sec", OUTLINE_RULE_DEFAULT_INTERVAL_SEC))
    except Exception:
        pass


def _read_outline_rules_from_object(obj):
    if not obj:
        return []

    raw_json = None
    try:
        raw_json = obj.get(OUTLINE_RULES_JSON_CP_KEY)
    except Exception:
        raw_json = None

    if isinstance(raw_json, str) and raw_json.strip():
        try:
            parsed = json.loads(raw_json)
        except Exception:
            parsed = None
        rules = _normalize_outline_rules_data(parsed)
        if rules:
            return rules

    legacy_rule = None
    try:
        enabled = bool(obj.get(OUTLINE_RULE_ENABLED_CP_KEY, False))
    except Exception:
        enabled = False

    if enabled:
        legacy_rule = _normalize_outline_rule_data({
            "enabled": True,
            "frame": obj.get(OUTLINE_RULE_FRAME_CP_KEY, 0),
            "time_sec": obj.get(OUTLINE_RULE_TIME_CP_KEY, 0.0),
            "duration_sec": obj.get(OUTLINE_RULE_DURATION_CP_KEY, OUTLINE_RULE_DEFAULT_DURATION_SEC),
            "interval_sec": obj.get(OUTLINE_RULE_INTERVAL_CP_KEY, OUTLINE_RULE_DEFAULT_INTERVAL_SEC),
        })

    return [legacy_rule] if legacy_rule else []


def _write_outline_rules_to_object(obj, rules):
    if not obj:
        return False

    norm_rules = _normalize_outline_rules_data(rules)
    try:
        if norm_rules:
            obj[OUTLINE_RULES_JSON_CP_KEY] = json.dumps(norm_rules, ensure_ascii=False)
        else:
            if OUTLINE_RULES_JSON_CP_KEY in obj.keys():
                del obj[OUTLINE_RULES_JSON_CP_KEY]
    except Exception:
        return False

    _sync_legacy_outline_rule_cp(obj, norm_rules)
    return True


def _build_outline_rule(frame=None, time_sec=None, duration_sec=OUTLINE_RULE_DEFAULT_DURATION_SEC, interval_sec=OUTLINE_RULE_DEFAULT_INTERVAL_SEC):
    scene = bpy.context.scene
    if frame is None:
        try:
            frame = int(scene.frame_current)
        except Exception:
            frame = 0

    try:
        frame = int(round(float(frame)))
    except Exception:
        frame = 0

    if time_sec is None:
        fps = _get_scene_fps(scene)
        try:
            time_sec = float(frame) / float(fps)
        except Exception:
            time_sec = 0.0

    return _normalize_outline_rule_data({
        "enabled": True,
        "frame": frame,
        "time_sec": time_sec,
        "duration_sec": duration_sec,
        "interval_sec": interval_sec,
    })


def _get_outline_rule_from_object(obj):
    rules = _read_outline_rules_from_object(obj)
    return rules[-1] if rules else None


def get_object_outline_rule_data(obj):
    return _get_outline_rule_from_object(obj)


def get_object_outline_rules_data(obj):
    return _read_outline_rules_from_object(obj)


def _set_outline_rule_on_object(obj, frame=None, time_sec=None, duration_sec=OUTLINE_RULE_DEFAULT_DURATION_SEC, interval_sec=OUTLINE_RULE_DEFAULT_INTERVAL_SEC):
    if not obj:
        return False

    rule = _build_outline_rule(
        frame=frame,
        time_sec=time_sec,
        duration_sec=duration_sec,
        interval_sec=interval_sec,
    )
    if not rule:
        return False

    rules = _read_outline_rules_from_object(obj)
    rules.append(rule)
    return _write_outline_rules_to_object(obj, rules)


def set_outline_rule_on_objects(objs, frame=None, duration_sec=OUTLINE_RULE_DEFAULT_DURATION_SEC, interval_sec=OUTLINE_RULE_DEFAULT_INTERVAL_SEC):
    applied = 0
    for obj in (objs or []):
        if _set_outline_rule_on_object(obj, frame=frame, duration_sec=duration_sec, interval_sec=interval_sec):
            applied += 1
    return applied


def _clear_outline_rule_on_object(obj):
    if not obj:
        return False

    changed = False
    for key in (
        OUTLINE_RULES_JSON_CP_KEY,
        OUTLINE_RULE_ENABLED_CP_KEY,
        OUTLINE_RULE_FRAME_CP_KEY,
        OUTLINE_RULE_TIME_CP_KEY,
        OUTLINE_RULE_DURATION_CP_KEY,
        OUTLINE_RULE_INTERVAL_CP_KEY,
    ):
        try:
            if key in obj.keys():
                del obj[key]
                changed = True
        except Exception:
            pass
    return changed


def clear_outline_rule_on_objects(objs):
    cleared = 0
    for obj in (objs or []):
        if _clear_outline_rule_on_object(obj):
            cleared += 1
    return cleared


def _capture_object_rules(objs):
    out = {}
    for obj in (objs or []):
        try:
            rules = _read_outline_rules_from_object(obj)
        except Exception:
            rules = []
        if rules:
            out[obj.name] = rules
    return out


def _apply_object_rules(object_rules):
    if not isinstance(object_rules, dict):
        return 0

    total = 0
    for obj_name, rules in object_rules.items():
        if not isinstance(obj_name, str):
            continue
        targets = _find_objects_by_name_all(obj_name)
        if not targets:
            try:
                obj = bpy.context.scene.objects.get(obj_name)
                targets = [obj] if obj else []
            except Exception:
                targets = []

        norm_rules = _normalize_outline_rules_data(rules)
        if not norm_rules:
            continue

        for obj in targets:
            try:
                if _write_outline_rules_to_object(obj, norm_rules):
                    total += 1
            except Exception:
                pass

    return total


def create_animation_entry(name, description=""):
    return {
        "schema_version": TRANSFORM_SCHEMA_VERSION,
        "name": str(name or ""),
        "created_at": datetime.now().isoformat(),
        "description": str(description or ""),
        "tracks": [],
    }


def _clear_animation_on_object(obj):
    try:
        if obj.animation_data:
            try:
                obj.animation_data.action = None
            except Exception:
                pass
            try:
                for t in list(obj.animation_data.nla_tracks):
                    try:
                        obj.animation_data.nla_tracks.remove(t)
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                obj.animation_data_clear()
            except Exception:
                try:
                    obj.animation_data.action = None
                except Exception:
                    pass
    except Exception:
        pass


# -------------------------
# META save/load
# -------------------------

def _capture_meta_objects(objs):
    """
    Snapshot meta-fields per object.
    Format: { "ObjName": {key: value, ...}, ... }
    """
    out = {}
    for o in objs:
        try:
            keys = o.keys()
        except Exception:
            continue

        data = {}
        for k in META_KEYS_ORDER:
            try:
                if k in keys:
                    data[k] = o.get(k)
            except Exception:
                pass

        if data:
            out[o.name] = data
    return out


def _normalize_zip_value(v):
    try:
        s = str(v).strip()
    except Exception:
        return "О"
    if s in ("О", "Г", "О/Г"):
        return s
    # accept latin equivalents just in case
    if s in ("O", "G", "O/G"):
        return {"O": "О", "G": "Г", "O/G": "О/Г"}.get(s, "О")
    return "О"


def _apply_meta_to_object(obj, fields: dict):
    if not obj or not isinstance(fields, dict):
        return 0

    applied = 0
    for k, v in fields.items():
        if k not in META_KEYS_ORDER:
            continue
        try:
            if k == "zip":
                obj[k] = _normalize_zip_value(v)
            else:
                obj[k] = v
            applied += 1
        except Exception:
            pass

    return applied


def _find_objects_by_name_all(name: str):
    """
    Returns list of objects whose obj.name == name.
    (Unlike bpy.data.objects.get which returns only one.)
    """
    if not isinstance(name, str):
        return []
    n = name.strip()
    if not n:
        return []
    out = []
    for obj in bpy.data.objects:
        try:
            if obj and obj.name == n:
                out.append(obj)
        except Exception:
            pass
    return out


def _apply_meta_objects(meta_objects):
    """
    Restore meta-fields on scene objects by name.

    Applies to ALL objects with matching name (safety for rare duplicate-name cases).
    """
    if not isinstance(meta_objects, dict):
        return 0

    total_applied = 0

    for obj_name, fields in meta_objects.items():
        if not isinstance(obj_name, str) or not isinstance(fields, dict):
            continue

        targets = _find_objects_by_name_all(obj_name)
        if not targets:
            # fallback via scene.objects
            try:
                o = bpy.context.scene.objects.get(obj_name)
                if o:
                    targets = [o]
            except Exception:
                targets = []

        for obj in targets:
            total_applied += _apply_meta_to_object(obj, fields)

    return total_applied


def _sync_zip_enum_from_cp():
    """
    Ensure UI dropdown (Object.umz_zip_choice_obj) reflects CP 'zip' immediately.
    """
    try:
        for obj in bpy.data.objects:
            try:
                if "zip" not in obj.keys():
                    continue
                if not hasattr(obj, "umz_zip_choice_obj"):
                    continue
                obj.umz_zip_choice_obj = _normalize_zip_value(obj.get("zip"))
            except Exception:
                pass
    except Exception:
        pass


def _get_animation_capture_objects(only_selected=False):
    scene = bpy.context.scene
    if only_selected:
        return list(bpy.context.selected_objects)
    return list(scene.objects)


def _capture_world_aware_tracks(objs, frame_start, frame_end, scene=None):
    scene = scene or bpy.context.scene
    tracks = []
    for obj in objs:
        source_nla = serialize_nla_for_object(obj)
        if source_nla and nla_has_transform_curves(source_nla):
            track_anim = build_world_aware_animation_track(
                obj,
                frame_start,
                frame_end,
                source_nla=source_nla,
                scene=scene,
                sample_step=1,
            )
            tracks.append({"object_name": obj.name, "animation": track_anim})
    return tracks


def create_animation_from_scene(name, description="", only_selected=False):
    internal = read_internal_films()
    entry = create_animation_entry(name, description)
    scene = bpy.context.scene

    try:
        frame_start = int(scene.frame_start)
        frame_end = int(scene.frame_end)
    except Exception:
        frame_start = 0
        frame_end = 250

    objs = _get_animation_capture_objects(only_selected=only_selected)
    entry["visible_objects_mode"] = "SELECTED" if only_selected else "ALL"
    if only_selected:
        entry["visible_objects"] = [o.name for o in objs]
    else:
        entry.pop("visible_objects", None)

    try:
        entry["meta_objects"] = _capture_meta_objects(objs)
    except Exception:
        pass

    try:
        object_rules = _capture_object_rules(objs)
        if object_rules:
            entry["object_rules"] = object_rules
        else:
            entry.pop("object_rules", None)
    except Exception:
        pass

    entry["tracks"] = _capture_world_aware_tracks(objs, frame_start, frame_end, scene=scene)
    entry["frame_start"] = frame_start
    entry["frame_end"] = frame_end

    save_text_and_markers = getattr(scene, "umz_text_and_markers", False)
    if save_text_and_markers:
        try:
            markers = _capture_timeline_markers(scene)
            if markers:
                entry["timeline_markers"] = markers
        except Exception:
            pass
        try:
            text_content = _capture_text_editor_content()
            if text_content is not None:
                entry["text_editor_content"] = text_content
        except Exception:
            pass
    else:
        entry.pop("timeline_markers", None)
        entry.pop("text_editor_content", None)

    internal[name] = entry
    write_internal_films(internal)
    write_animation_to_file(name, entry)

    try:
        three_clip = build_three_clip_from_saved_entry(name, entry)
        folder = get_external_folder()
        write_three_animation_to_file(name, three_clip, folder)
    except Exception as e:
        print("[three-export ERROR]", repr(e))

    mark_cache_dirty()
    return True


def update_animation_from_scene(anim_name, only_selected=False, description=None):
    internal = read_internal_films()
    if anim_name not in internal:
        raise RuntimeError("Анимация не найдена.")

    entry = internal[anim_name]
    scene = bpy.context.scene
    try:
        frame_start = int(scene.frame_start)
        frame_end = int(scene.frame_end)
    except Exception:
        frame_start = 0
        frame_end = 250

    if description is not None:
        entry["description"] = str(description or "")

    objs = _get_animation_capture_objects(only_selected=only_selected)
    entry["visible_objects_mode"] = "SELECTED" if only_selected else "ALL"
    if only_selected:
        entry["visible_objects"] = [o.name for o in objs]
    else:
        entry.pop("visible_objects", None)

    try:
        entry["meta_objects"] = _capture_meta_objects(objs)
    except Exception:
        pass

    try:
        object_rules = _capture_object_rules(objs)
        if object_rules:
            entry["object_rules"] = object_rules
        else:
            entry.pop("object_rules", None)
    except Exception:
        pass

    entry["schema_version"] = TRANSFORM_SCHEMA_VERSION
    entry["tracks"] = _capture_world_aware_tracks(objs, frame_start, frame_end, scene=scene)
    entry["created_at"] = datetime.now().isoformat()
    entry["frame_start"] = frame_start
    entry["frame_end"] = frame_end

    save_text_and_markers = getattr(scene, "umz_text_and_markers", False)
    if save_text_and_markers:
        try:
            markers = _capture_timeline_markers(scene)
            if markers:
                entry["timeline_markers"] = markers
        except Exception:
            pass
        try:
            text_content = _capture_text_editor_content()
            if text_content is not None:
                entry["text_editor_content"] = text_content
        except Exception:
            pass
    else:
        entry.pop("timeline_markers", None)
        entry.pop("text_editor_content", None)

    internal[anim_name] = entry
    write_internal_films(internal)
    write_animation_to_file(anim_name, entry)

    try:
        three_clip = build_three_clip_from_saved_entry(anim_name, entry)
        folder = get_external_folder()
        write_three_animation_to_file(anim_name, three_clip, folder)
    except Exception as e:
        print("[three-export ERROR]", repr(e))

    mark_cache_dirty()
    return True


def delete_animation(anim_name, full_delete=False):
    internal = read_internal_films()
    entry = internal.get(anim_name)
    if not entry:
        ext = read_external_films()
        entry = ext.get(anim_name)

    action_names = set()
    if full_delete and entry:
        for tr in entry.get("tracks", []):
            anim = tr.get("animation", {}) or {}
            for t in anim.get("tracks", []):
                for s in t.get("strips", []):
                    act = s.get("action")
                    if isinstance(act, dict):
                        n = act.get("name")
                        if n:
                            action_names.add(n)

        for a_name in list(action_names):
            for obj in bpy.data.objects:
                ad = getattr(obj, "animation_data", None)
                if not ad:
                    continue
                try:
                    if ad.action and ad.action.name == a_name:
                        ad.action = None
                except Exception:
                    pass
                try:
                    for track in list(ad.nla_tracks):
                        for strip in list(track.strips):
                            try:
                                if strip.action and strip.action.name == a_name:
                                    track.strips.remove(strip)
                            except Exception:
                                pass
                        try:
                            if len(track.strips) == 0:
                                ad.nla_tracks.remove(track)
                        except Exception:
                            pass
                except Exception:
                    pass

        for a_name in list(action_names):
            a = bpy.data.actions.get(a_name)
            if a:
                try:
                    bpy.data.actions.remove(a)
                except Exception:
                    pass

    removed = False
    if anim_name in internal:
        del internal[anim_name]
        write_internal_films(internal)
        removed = True

    remove_animation_file(anim_name)

    folder = get_external_folder()
    if folder:
        try:
            p = os.path.join(folder, f"three_{anim_name}.json")
            if os.path.isfile(p):
                os.remove(p)
        except Exception:
            pass

    mark_cache_dirty()

    names = list(read_all_films_cached().keys())
    try:
        bpy.context.scene.umz_selected_animation = names[0] if names else ""
    except Exception:
        pass

    return removed


def _apply_visibility_from_entry(entry):
    mode = entry.get("visible_objects_mode", "ALL")

    if mode != "SELECTED":
        for obj in bpy.data.objects:
            try:
                obj.hide_set(False)
            except Exception:
                pass
            try:
                obj.hide_render = False
            except Exception:
                pass
        return

    visible = entry.get("visible_objects") or []
    visible_set = set([v for v in visible if isinstance(v, str)])

    for obj in bpy.data.objects:
        show = (obj.name in visible_set)
        try:
            obj.hide_set(not show)
        except Exception:
            pass
        try:
            obj.hide_render = (not show)
        except Exception:
            pass




# -------------------------
# Transform conflict detection / rewrite
# World-space authoritative comparison and repair.
# Snapshot selection is ignored for animation repair in schema v2.
# -------------------------

TRANSFORM_CONFLICT_LOCATION_EPS = 1e-4
TRANSFORM_CONFLICT_SCALE_EPS = 1e-4
TRANSFORM_CONFLICT_ROTATION_EPS = 1e-4


def get_animation_transform_conflicts(anim_name, snapshot_name=None, scene=None):
    if scene is None:
        scene = bpy.context.scene

    result = {
        'animation_name': str(anim_name or ''),
        'snapshot_name': '',
        'conflicts': [],
    }
    if not anim_name:
        return result

    all_films = read_all_films_cached() or {}
    film = all_films.get(anim_name)
    if not isinstance(film, dict):
        return result

    try:
        conflicts = detect_world_conflicts_for_entry(
            film,
            scene=scene,
            matrix_eps=1e-5,
            loc_eps=TRANSFORM_CONFLICT_LOCATION_EPS,
            rot_eps=TRANSFORM_CONFLICT_ROTATION_EPS,
            scale_eps=TRANSFORM_CONFLICT_SCALE_EPS,
        )
    except Exception as e:
        print('[UMZ][conflict] detect error:', repr(e))
        conflicts = []

    result['conflicts'] = conflicts
    return result


def rewrite_animation_conflicts_from_snapshot(anim_name, object_names=None, snapshot_name=None, scene=None):
    if scene is None:
        scene = bpy.context.scene
    if not anim_name:
        raise RuntimeError('Анимация не выбрана.')

    all_films = read_all_films_cached() or {}
    entry = all_films.get(anim_name)
    if not isinstance(entry, dict):
        raise RuntimeError('Анимация не найдена.')

    conflict_data = get_animation_transform_conflicts(anim_name, scene=scene)
    conflicts = conflict_data.get('conflicts') or []
    wanted = {str(n).strip() for n in (object_names or []) if str(n).strip()} if object_names else None
    changed_objects = []

    track_lookup = build_track_lookup(entry)
    repair_target_names = set(track_lookup.keys() if wanted is None else wanted)

    for obj_name, track_anim in track_lookup.items():
        if wanted is not None and obj_name not in wanted:
            continue
        obj = scene.objects.get(obj_name) or bpy.data.objects.get(obj_name)
        if obj is None:
            continue
        _clear_animation_on_object(obj)
        action = repair_world_animation_track(
            obj,
            anim_name,
            track_anim,
            track_lookup,
            repair_target_names=repair_target_names,
            scene=scene,
        )
        if action is not None:
            changed_objects.append(obj_name)
            print(f"[UMZ][repair] animation frame recalculated in world space: {obj_name}")

    return {
        'changed_objects': changed_objects,
        'keyframes_changed': len(changed_objects),
        'snapshot_name': '',
        'conflicts_before': conflicts,
    }


def apply_animation_to_scene(anim_name, remove_other_animations=True):
    scene = bpy.context.scene
    try:
        if int(scene.frame_current) != 0:
            scene.frame_set(0)
        bpy.context.view_layer.update()
    except Exception:
        pass

    all_films = read_all_films_cached() or {}
    film = all_films.get(anim_name)
    if not film:
        raise RuntimeError("Анимация не найдена.")

    try:
        _apply_meta_objects(film.get("meta_objects"))
    except Exception:
        pass
    try:
        _apply_object_rules(film.get("object_rules"))
    except Exception:
        pass

    try:
        scene.frame_start = int(film.get("frame_start", 0))
        scene.frame_end = int(film.get("frame_end", 250))
    except Exception:
        pass

    track_lookup = build_track_lookup(film)
    track_objs = set(track_lookup.keys())

    if remove_other_animations:
        for obj in list(scene.objects):
            if obj.name not in track_objs:
                _clear_animation_on_object(obj)

    applied = []
    for obj_name, track_anim in track_lookup.items():
        obj = scene.objects.get(obj_name) or bpy.data.objects.get(obj_name)
        if not obj:
            continue
        _clear_animation_on_object(obj)
        action = apply_local_samples_to_object(obj, anim_name, track_anim, scene=scene)
        if action is not None:
            applied.append(obj_name)

    try:
        bpy.context.view_layer.update()
    except Exception:
        pass

    conflicts = []
    try:
        conflict_data = get_animation_transform_conflicts(anim_name, scene=scene)
        conflicts = conflict_data.get("conflicts") or []
    except Exception:
        conflicts = []

    return {"applied": applied, "conflicts": conflicts, "snapshot_name": ""}
