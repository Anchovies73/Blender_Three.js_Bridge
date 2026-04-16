import bpy
import json
import os

from mathutils import Quaternion
from .constants import (
    ROT_BAKE_STEP_FRAMES,
    CAMERA_BAKE_EVERY_FRAME,
    CAMERA_BAKE_STEP_FRAMES,
    GLTF_ID_PROP,
)
from .text_utils import get_active_text_datablock
from .blender_codec import META_KEYS_ORDER
from ..transform_utils import matrix_from_list

# =========================================================
# ЭКСПОРТ В THREE.JS (entry -> three_<name>.json)
# Здесь НЕТ логики хранения библиотеки (Text/внешние name.json).
# Мы принимаем entry (словарь) и строим clip для three.js.
#
# NEW:
# - out["meta_objects"] для парсинга на сайте (meta + gltf_id)
# =========================================================

# -------------------------
# Базовые хелперы времени
# -------------------------

def _get_scene_fps(scene):
    try:
        fps = float(scene.render.fps) / float(scene.render.fps_base or 1.0)
        return fps if fps > 0 else 24.0
    except Exception:
        return 24.0


def _frame_to_time(frame, frame_start, fps):
    return float(frame - frame_start) / float(fps)


# -------------------------
# Идентификаторы/камера
# -------------------------

def _safe_node_id(obj):
    """
    Возвращает node id для three clip track.
    Сначала пробует obj[GLTF_ID_PROP], иначе obj.name.
    """
    try:
        v = obj.get(GLTF_ID_PROP)
        if isinstance(v, str) and v.strip():
            return v.strip()
    except Exception:
        pass
    return obj.name


def _is_camera_object(obj):
    try:
        if obj and getattr(obj, "type", None) == "CAMERA":
            return True
    except Exception:
        pass
    try:
        return bool(obj and isinstance(obj.name, str) and obj.name.startswith("Camera"))
    except Exception:
        return False


# -------------------------
# NEW: meta objects for site parsing
# -------------------------

def _get_obj_gltf_id(obj):
    try:
        v = obj.get(GLTF_ID_PROP)
        if isinstance(v, str) and v.strip():
            return v.strip()
    except Exception:
        pass
    return ""


def _collect_outline_rules_for_three(entry):
    """
    Convert entry["object_rules"] keyed by Blender object names into a stable list
    keyed by node_id / gltf_id for the website runtime.

    New format per object:
      {
        "node_id": "...",
        "gltf_id": "...",
        "name": "...",
        "type": "outline_blink_group",
        "rules": [{...}, {...}]
      }

    Legacy single-rule input is still accepted and normalized into rules=[...].
    """
    object_rules = (entry or {}).get("object_rules")
    if not isinstance(object_rules, dict) or not object_rules:
        return []

    out = []
    for obj_name, raw_rules in object_rules.items():
        if isinstance(raw_rules, dict):
            raw_rules = [raw_rules]
        if not isinstance(raw_rules, (list, tuple)) or not raw_rules:
            continue

        obj = bpy.data.objects.get(obj_name)
        gltf_id = _get_obj_gltf_id(obj) if obj else ""
        node_id = gltf_id if gltf_id else obj_name

        norm_rules = []
        for rule in raw_rules:
            if not isinstance(rule, dict):
                continue

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
                duration_sec = float(rule.get("duration_sec", 4.0))
            except Exception:
                duration_sec = 4.0

            try:
                interval_sec = float(rule.get("interval_sec", 1.0))
            except Exception:
                interval_sec = 1.0

            if duration_sec <= 0.0:
                duration_sec = 4.0
            if interval_sec <= 0.0:
                interval_sec = 1.0

            norm_rules.append({
                "enabled": enabled,
                "frame": frame,
                "time_sec": time_sec,
                "duration_sec": duration_sec,
                "interval_sec": interval_sec,
            })

        if not norm_rules:
            continue

        try:
            norm_rules.sort(key=lambda x: (float(x.get("time_sec", 0.0)), int(x.get("frame", 0))))
        except Exception:
            pass

        out.append({
            "node_id": node_id,
            "gltf_id": gltf_id,
            "name": obj_name,
            "type": "outline_blink_group",
            "rules": norm_rules,
        })

    try:
        out.sort(key=lambda x: str(x.get("node_id", "")))
    except Exception:
        pass

    return out


def _collect_meta_objects_for_three(entry):
    """
    Возвращает list[dict] в стабильном формате для сайта.

    Берём поля из entry["meta_objects"] (снимок на момент сохранения анимации),
    а gltf_id подтягиваем из текущих объектов Blender (если объект есть),
    чтобы соответствовало экспорту glTF.
    """
    meta_objects = (entry or {}).get("meta_objects")
    if not isinstance(meta_objects, dict) or not meta_objects:
        return []

    out = []
    for obj_name, fields in meta_objects.items():
        if not isinstance(fields, dict):
            continue

        obj = bpy.data.objects.get(obj_name)
        gltf_id = _get_obj_gltf_id(obj) if obj else ""

        # node_id = то, что будет в треках three.js (gltf_id если есть)
        node_id = gltf_id if gltf_id else obj_name

        # нормализуем поля: только META_KEYS_ORDER
        norm_fields = {}
        for k in META_KEYS_ORDER:
            if k in fields:
                try:
                    norm_fields[k] = fields.get(k)
                except Exception:
                    pass

        # доп. "нечитабельная" строка (если вдруг нужно простое хранение)
        # формат: key=value;key=value
        try:
            parts = []
            for k in META_KEYS_ORDER:
                if k in norm_fields:
                    parts.append(f"{k}={norm_fields.get(k)}")
            fields_csv = ";".join(parts)
        except Exception:
            fields_csv = ""

        out.append({
            "node_id": node_id,
            "gltf_id": gltf_id,
            "name": obj_name,
            "fields": norm_fields,
            "fields_csv": fields_csv,
        })

    # стабильный порядок (удобнее для git diff и сайта)
    try:
        out.sort(key=lambda x: str(x.get("node_id", "")))
    except Exception:
        pass

    return out


# -------------------------
# Ключи/кадры из сериализованного entry (NLA)
# -------------------------

def _collect_keyframes(action_fcurves, data_path, array_index, frame_start, frame_end):
    """
    Возвращает отсортированный список (frame, value) для указанного канала.
    action_fcurves может быть:
      - реальный fcurve объект Blender, либо
      - сериализованный dict (как у нас в entry)
    """
    for fc in action_fcurves:
        if hasattr(fc, "data_path"):
            dp = fc.data_path
            idx = int(getattr(fc, "array_index", -1))
        else:
            dp = fc.get("data_path")
            idx = int(fc.get("array_index", -1))

        if dp == data_path and idx == int(array_index):
            pts = []

            # Blender fcurve
            if hasattr(fc, "keyframe_points"):
                for kp in fc.keyframe_points:
                    try:
                        fr = float(kp.co.x)
                        val = float(kp.co.y)
                    except Exception:
                        continue
                    if fr < frame_start or fr > frame_end:
                        continue
                    pts.append((fr, val))

            # сериализованный dict
            else:
                for kp in fc.get("keyframes", []):
                    co = kp.get("co")
                    if not co or len(co) < 2:
                        continue
                    fr = float(co[0])
                    val = float(co[1])
                    if fr < frame_start or fr > frame_end:
                        continue
                    pts.append((fr, val))

            pts.sort(key=lambda x: x[0])
            return pts

    return []


def _union_frames(*channels):
    frames = set()
    for ch in channels:
        for fr, _ in ch:
            frames.add(int(round(float(fr))))
    return sorted(frames)


def _collect_nla_keyframes(anim, data_path, array_index, frame_start, frame_end):
    """
    Собирает ключи из сериализованных NLA strips и мапит Action frames -> Scene frames.
    Возвращает список (scene_frame, value).
    """
    pts = []

    for nla_track in (anim.get("tracks") or []):
        for strip in (nla_track.get("strips") or []):
            if strip.get("muted"):
                continue

            act = strip.get("action") or {}
            fcurves = act.get("fcurves") or []
            if not fcurves:
                continue

            # ключи в action-space
            ch = _collect_keyframes(fcurves, data_path, array_index, -10**12, 10**12)
            if not ch:
                continue

            s_frame_start = float(strip.get("frame_start", 0.0) or 0.0)
            a_frame_start = float(strip.get("action_frame_start", 0.0) or 0.0)
            s_scale = float(strip.get("scale", 1.0) or 1.0)

            # базовый маппинг (reverse/repeat пока не поддержаны)
            for a_fr, val in ch:
                scene_fr = s_frame_start + (float(a_fr) - a_frame_start) * s_scale
                scene_fr = int(round(scene_fr))
                if scene_fr < frame_start or scene_fr > frame_end:
                    continue
                pts.append((scene_fr, float(val)))

    pts.sort(key=lambda x: x[0])
    return pts


def _collect_nla_keyframes_frames(anim, data_path, array_index, frame_start, frame_end):
    """
    Собирает только кадры ключей (без значений) из сериализованных NLA strips.
    Возвращает sorted list[int] кадры сцены.
    """
    frames = set()

    for nla_track in (anim.get("tracks") or []):
        for strip in (nla_track.get("strips") or []):
            if strip.get("muted"):
                continue

            act = strip.get("action") or {}
            fcurves = act.get("fcurves") or []
            if not fcurves:
                continue

            ch = _collect_keyframes(fcurves, data_path, array_index, -10**12, 10**12)
            if not ch:
                continue

            s_frame_start = float(strip.get("frame_start", 0.0) or 0.0)
            a_frame_start = float(strip.get("action_frame_start", 0.0) or 0.0)
            s_scale = float(strip.get("scale", 1.0) or 1.0)

            for a_fr, _val in ch:
                scene_fr = s_frame_start + (float(a_fr) - a_frame_start) * s_scale
                scene_fr = int(round(scene_fr))
                if scene_fr < frame_start or scene_fr > frame_end:
                    continue
                frames.add(scene_fr)

    return sorted(frames)


def _build_number_track(obj_name, prop_path, frames, frame_start, fps, get_value):
    times = [_frame_to_time(fr, frame_start, fps) for fr in frames]
    values = [float(get_value(fr)) for fr in frames]
    return {"type": "number", "name": f"{obj_name}.{prop_path}", "times": times, "values": values}


# -------------------------
# alpha_tracks (ручной runtime в three.js)
# -------------------------

def _collect_nla_number_track(anim, data_path, array_index, frame_start, frame_end, fps):
    """
    Строит number track (times/values) из сериализованных NLA для 1 канала.
    Дедуп по кадру сцены (последний wins).
    """
    frame_to_value = {}

    for nla_track in (anim.get("tracks") or []):
        for strip in (nla_track.get("strips") or []):
            if strip.get("muted"):
                continue

            act = strip.get("action") or {}
            fcurves = act.get("fcurves") or []
            if not fcurves:
                continue

            ch = _collect_keyframes(fcurves, data_path, array_index, -10**12, 10**12)
            if not ch:
                continue

            s_frame_start = float(strip.get("frame_start", 0.0) or 0.0)
            a_frame_start = float(strip.get("action_frame_start", 0.0) or 0.0)
            s_scale = float(strip.get("scale", 1.0) or 1.0)

            for a_fr, val in ch:
                scene_fr = s_frame_start + (float(a_fr) - a_frame_start) * s_scale
                scene_fr = int(round(scene_fr))
                if scene_fr < frame_start or scene_fr > frame_end:
                    continue
                frame_to_value[scene_fr] = float(val)

    if not frame_to_value:
        return None

    frames = sorted(frame_to_value.keys())
    times = [_frame_to_time(fr, frame_start, fps) for fr in frames]
    values = [float(frame_to_value[fr]) for fr in frames]
    return {"frames": frames, "times": times, "values": values}


def _build_alpha_tracks_for_object(node_id, anim, frame_start, frame_end, fps):
    """
    Возвращает dict alpha_track или None.
    Приоритет:
      1) Custom Property ["alpha"] index=0
      2) Object Color alpha: data_path="color" index=3
    """
    # 1) CP ["alpha"]
    t = _collect_nla_number_track(anim, '["alpha"]', 0, frame_start, frame_end, fps)
    if t:
        vals = [max(0.0, min(1.0, v)) for v in t["values"]]
        return {"node": node_id, "times": t["times"], "values": vals, "source": '["alpha"]'}

    # 2) Object.color[3]
    t = _collect_nla_number_track(anim, "color", 3, frame_start, frame_end, fps)
    if t:
        vals = [max(0.0, min(1.0, v)) for v in t["values"]]
        return {"node": node_id, "times": t["times"], "values": vals, "source": "color[3]"}

    return None


# -------------------------
# Timeline markers and text editor helpers
# -------------------------

def _parse_text_blocks(text_content):
    if not text_content:
        return []

    blocks = []
    current_block = []

    for line in text_content.split('\n'):
        stripped = line.strip()
        if not stripped:
            if current_block:
                block_text = '\n'.join(current_block).strip()
                if block_text:
                    blocks.append(block_text)
                current_block = []
        else:
            current_block.append(line)

    if current_block:
        block_text = '\n'.join(current_block).strip()
        if block_text:
            blocks.append(block_text)

    return blocks


def _parse_id_and_text(block):
    if not block:
        return None, None

    space_idx = block.find(' ')

    if space_idx == -1:
        token = block
        text = ''
    else:
        token = block[:space_idx]
        text = block[space_idx + 1:]

    if not token.endswith('.'):
        return None, None

    parts = token[:-1].split('.')
    if not parts:
        return None, None

    for part in parts:
        if not part:
            return None, None
        if not part.isdigit():
            return None, None

    return token, text


def _get_parent_id(node_id):
    if not node_id or not node_id.endswith('.'):
        return None

    parts = node_id[:-1].split('.')
    if len(parts) <= 1:
        return None

    return '.'.join(parts[:-1]) + '.'


def _build_hierarchical_tree(flat_blocks):
    if not flat_blocks:
        return []

    nodes = {}
    for block in flat_blocks:
        node_id = block['id']
        nodes[node_id] = {
            'id': node_id,
            'start': block['start'],
            'end': block['end'],
            'text': block['text'],
            'children': []
        }

    roots = []
    for node_id, node in nodes.items():
        parent_id = _get_parent_id(node_id)
        if parent_id is None:
            roots.append(node)
        elif parent_id in nodes:
            nodes[parent_id]['children'].append(node)

    return roots


def _build_markers_text(scene, fps):
    try:
        markers = scene.timeline_markers
        if len(markers) < 2:
            return []

        sorted_markers = sorted(markers, key=lambda m: m.frame)

        text_datablock = get_active_text_datablock()
        if not text_datablock:
            return []

        text_content = text_datablock.as_string()
        if not text_content:
            return []

        raw_blocks = _parse_text_blocks(text_content)
        if not raw_blocks:
            return []

        parsed_blocks = []
        for block in raw_blocks:
            node_id, text = _parse_id_and_text(block)
            if node_id is not None:
                parsed_blocks.append({'id': node_id, 'text': text})

        if not parsed_blocks:
            return []

        flat_blocks = []
        fps_real = fps

        for i in range(len(sorted_markers) - 1):
            if i >= len(parsed_blocks):
                break

            parsed_block = parsed_blocks[i]
            marker_start = sorted_markers[i]
            marker_end = sorted_markers[i + 1]

            start_seconds = float(marker_start.frame) / fps_real
            end_seconds = float(marker_end.frame) / fps_real

            start_seconds = round(start_seconds, 2)
            end_seconds = round(end_seconds, 2)

            flat_blocks.append({
                'id': parsed_block['id'],
                'start': start_seconds,
                'end': end_seconds,
                'text': parsed_block['text']
            })

        tree = _build_hierarchical_tree(flat_blocks)
        return tree

    except Exception:
        return []


# -------------------------
# Baking helpers (evaluated local transform)
# -------------------------

def _is_quaternion_constant(values, eps=1e-6):
    if not values:
        return True
    vs = [float(v) for v in values]
    if len(vs) < 8:
        return True
    base = vs[0:4]
    for i in range(4, len(vs), 4):
        if any(abs(vs[i + j] - base[j]) > eps for j in range(4)):
            return False
    return True


def _eval_local_matrix(obj, depsgraph):
    obj_eval = obj.evaluated_get(depsgraph)
    mw = obj_eval.matrix_world.copy()

    if obj.parent:
        p_eval = obj.parent.evaluated_get(depsgraph)
        pmw = p_eval.matrix_world.copy()
        try:
            return pmw.inverted() @ mw
        except Exception:
            return mw

    return mw


# -------------------------
# Основная сборка клипа
# -------------------------

def build_three_clip_from_saved_entry(entry_name, entry):
    scene = bpy.context.scene

    fps = _get_scene_fps(scene)
    export_alpha = bool(getattr(scene, "umz_export_alpha_tracks", True))

    try:
        frame_start = int(entry.get("frame_start", scene.frame_start))
    except Exception:
        frame_start = int(scene.frame_start)

    try:
        frame_end = int(entry.get("frame_end", scene.frame_end))
    except Exception:
        frame_end = int(scene.frame_end)

    duration = float(frame_end - frame_start) / float(fps)
    tracks_out = []
    alpha_tracks_out = []

    visible_nodes_mode = "ALL"
    visible_nodes = None
    mode = entry.get("visible_objects_mode", "ALL")
    if mode == "SELECTED":
        names = entry.get("visible_objects") or []
        if isinstance(names, list) and names:
            visible_nodes_mode = "SELECTED"
            scene_objects = {o.name: o for o in bpy.data.objects}
            tmp = []
            for n in names:
                obj = scene_objects.get(n)
                if obj:
                    tmp.append(_safe_node_id(obj))
            visible_nodes = tmp

    scene_objects = {o.name: o for o in bpy.data.objects}

    for tr in entry.get("tracks", []):
        obj_name_blender = tr.get("object_name")
        anim = tr.get("animation") or {}
        if not obj_name_blender or not isinstance(anim, dict):
            continue

        obj = scene_objects.get(obj_name_blender)
        node_id = _safe_node_id(obj) if obj else obj_name_blender
        source_nla = anim.get("source_nla") or {}
        samples = anim.get("samples") or []
        if not samples:
            continue

        times = []
        pos_values = []
        quat_values = []
        prev_q = None
        pos_changed = False
        rot_changed = False
        first_loc = None
        first_quat = None

        for sample in samples:
            try:
                frame = int(sample.get("frame", 0))
            except Exception:
                continue
            times.append(_frame_to_time(frame, frame_start, fps))

            local = sample.get("local")
            if not isinstance(local, dict):
                local = None
                try:
                    local = sample.get("matrix_local") and matrix_from_list(sample.get("matrix_local"))
                except Exception:
                    local = None
                if local is not None:
                    loc, rot, sca = local.decompose()
                    q = rot.to_quaternion() if hasattr(rot, 'to_quaternion') else rot
                    q.normalize()
                    local = {
                        'location': [float(loc.x), float(loc.y), float(loc.z)],
                        'rotation_quaternion': [float(q.w), float(q.x), float(q.y), float(q.z)],
                    }
            if not isinstance(local, dict):
                continue

            loc = [float(v) for v in (local.get('location') or [0.0, 0.0, 0.0])[:3]]
            quat_wxyz = [float(v) for v in (local.get('rotation_quaternion') or [1.0, 0.0, 0.0, 0.0])[:4]]
            q = Quaternion((quat_wxyz[0], quat_wxyz[1], quat_wxyz[2], quat_wxyz[3]))
            q.normalize()
            if prev_q is not None and prev_q.dot(q) < 0.0:
                q = Quaternion((-q.w, -q.x, -q.y, -q.z))
            prev_q = q.copy()

            pos_values.extend(loc)
            quat_values.extend([float(q.x), float(q.y), float(q.z), float(q.w)])

            if first_loc is None:
                first_loc = tuple(loc)
            elif any(abs(loc[i] - first_loc[i]) > 1e-6 for i in range(3)):
                pos_changed = True

            if first_quat is None:
                first_quat = (q.w, q.x, q.y, q.z)
            elif any(abs(v - first_quat[i]) > 1e-6 for i, v in enumerate((q.w, q.x, q.y, q.z))):
                rot_changed = True

        if times and (pos_changed or len(times) == 1):
            tracks_out.append({
                "type": "vector",
                "name": f"{node_id}.position",
                "times": times,
                "values": pos_values,
            })

        if times and (rot_changed or len(times) == 1):
            tracks_out.append({
                "type": "quaternion",
                "name": f"{node_id}.quaternion",
                "times": times,
                "values": quat_values,
            })

        if export_alpha and source_nla:
            at = _build_alpha_tracks_for_object(node_id, source_nla, frame_start, frame_end, fps)
            if at:
                alpha_tracks_out.append(at)

    out = {
        "name": entry_name,
        "fps": fps,
        "frame_start": frame_start,
        "frame_end": frame_end,
        "duration": duration,
        "tracks": tracks_out,
        "alpha_tracks": alpha_tracks_out,
        "visible_nodes_mode": visible_nodes_mode,
    }

    if visible_nodes_mode == "SELECTED":
        out["visible_nodes"] = visible_nodes or []

    markers_text = _build_markers_text(scene, fps)
    if markers_text:
        out["markers_text"] = markers_text

    try:
        out["meta_objects"] = _collect_meta_objects_for_three(entry)
    except Exception:
        out["meta_objects"] = []

    try:
        out["outline_rules"] = _collect_outline_rules_for_three(entry)
    except Exception:
        out["outline_rules"] = []

    return out


def write_three_animation_to_file(name, clip, folder):
    """
    Записывает three_<name>.json в папку folder.
    Папку мы передаём снаружи (обычно из storage.get_external_folder()).
    """
    if not folder:
        return False
    try:
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"three_{name}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(clip, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False