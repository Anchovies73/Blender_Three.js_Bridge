import bpy
from mathutils import Matrix

from ..transform_utils import (
    capture_local_matrix,
    capture_world_matrix,
    matrix_to_list,
    matrix_from_list,
    decompose_matrix,
    matrices_almost_equal,
    matrix_delta_report,
    compute_local_from_world,
)
from .blender_codec import pushdown_action_to_nla

TRANSFORM_PATHS = {"location", "rotation_euler", "rotation_quaternion", "scale"}
TRANSFORM_SCHEMA_VERSION = 2


def log_debug(message: str):
    print(f"[UMZ][worldspace] {message}")


def _scene_ctx(scene=None):
    scene = scene or bpy.context.scene
    return scene, bpy.context.view_layer


def _iter_source_fcurves(source_nla):
    if not isinstance(source_nla, dict):
        return
    for nla_track in (source_nla.get("tracks") or []):
        if not isinstance(nla_track, dict):
            continue
        for strip in (nla_track.get("strips") or []):
            if not isinstance(strip, dict):
                continue
            act = strip.get("action") or {}
            for fc in (act.get("fcurves") or []):
                if isinstance(fc, dict):
                    yield fc


def extract_non_transform_fcurves(source_nla):
    out = []
    for fc in _iter_source_fcurves(source_nla):
        if str(fc.get("data_path") or "") in TRANSFORM_PATHS:
            continue
        out.append(fc)
    return out


def capture_animation_samples_for_object(obj, frame_start, frame_end, scene=None, sample_step=1):
    scene, view_layer = _scene_ctx(scene)
    current_frame = int(scene.frame_current)
    samples = []

    for frame in range(int(frame_start), int(frame_end) + 1, max(1, int(sample_step))):
        scene.frame_set(frame)
        try:
            view_layer.update()
        except Exception:
            pass

        local_matrix = capture_local_matrix(obj)
        world_matrix = capture_world_matrix(obj)
        sample = {
            "frame": int(frame),
            "matrix_local": matrix_to_list(local_matrix),
            "matrix_world": matrix_to_list(world_matrix),
            "local": decompose_matrix(local_matrix),
            "world": decompose_matrix(world_matrix),
        }
        samples.append(sample)

    scene.frame_set(current_frame)
    try:
        view_layer.update()
    except Exception:
        pass

    return samples


def build_world_aware_animation_track(obj, frame_start, frame_end, source_nla=None, scene=None, sample_step=1):
    return {
        "schema_version": TRANSFORM_SCHEMA_VERSION,
        "type": "matrix_samples_v2",
        "sample_step": int(max(1, sample_step)),
        "frame_start": int(frame_start),
        "frame_end": int(frame_end),
        "captured_parent_name": obj.parent.name if getattr(obj, "parent", None) else "",
        "captured_rotation_mode": str(getattr(obj, "rotation_mode", "XYZ") or "XYZ"),
        "samples": capture_animation_samples_for_object(obj, frame_start, frame_end, scene=scene, sample_step=sample_step),
        "source_nla": source_nla if isinstance(source_nla, dict) else None,
    }


def build_track_lookup(entry_or_tracks):
    tracks = entry_or_tracks.get("tracks") if isinstance(entry_or_tracks, dict) else entry_or_tracks
    out = {}
    for tr in (tracks or []):
        if not isinstance(tr, dict):
            continue
        name = str(tr.get("object_name") or "").strip()
        if not name:
            continue
        anim = tr.get("animation") or {}
        if isinstance(anim, dict):
            out[name] = anim
    return out


def _sample_map(track_anim):
    res = {}
    for sample in (track_anim.get("samples") or []):
        if not isinstance(sample, dict):
            continue
        try:
            frame = int(sample.get("frame"))
        except Exception:
            continue
        res[frame] = sample
    return res


def get_saved_world_matrix_at_frame(track_anim, frame):
    s = _sample_map(track_anim).get(int(frame))
    if not s:
        return None
    return matrix_from_list(s.get("matrix_world"))


def _ensure_action_name(name):
    existing = bpy.data.actions.get(name)
    if existing and existing.users == 0:
        try:
            bpy.data.actions.remove(existing)
        except Exception:
            pass
        existing = bpy.data.actions.get(name)
    if existing is None:
        return name
    base = name
    i = 1
    while bpy.data.actions.get(f"{base}_{i}") is not None:
        i += 1
    return f"{base}_{i}"


def _create_action_with_transform_keys(obj, action_name, keyed_samples, extra_fcurves=None):
    action_name = _ensure_action_name(action_name)
    action = bpy.data.actions.new(action_name)

    fc_loc = [action.fcurves.new(data_path="location", index=i) for i in range(3)]
    fc_rot = [action.fcurves.new(data_path="rotation_quaternion", index=i) for i in range(4)]
    fc_sca = [action.fcurves.new(data_path="scale", index=i) for i in range(3)]

    for sample in keyed_samples:
        frame = float(sample["frame"])
        loc = sample["location"]
        quat = sample["rotation_quaternion"]
        scale = sample["scale"]

        for i in range(3):
            k = fc_loc[i].keyframe_points.insert(frame=frame, value=float(loc[i]), options={'FAST'})
            k.interpolation = 'LINEAR'
        for i in range(4):
            k = fc_rot[i].keyframe_points.insert(frame=frame, value=float(quat[i]), options={'FAST'})
            k.interpolation = 'LINEAR'
        for i in range(3):
            k = fc_sca[i].keyframe_points.insert(frame=frame, value=float(scale[i]), options={'FAST'})
            k.interpolation = 'LINEAR'

    for fc in fc_loc + fc_rot + fc_sca:
        try:
            fc.update()
        except Exception:
            pass

    for fc_data in (extra_fcurves or []):
        try:
            dp = str(fc_data.get("data_path") or "")
            idx = int(fc_data.get("array_index", 0))
            fcurve = action.fcurves.new(data_path=dp, index=idx)
        except Exception:
            continue
        for kp in (fc_data.get("keyframes") or []):
            try:
                co = kp.get("co") or [0.0, 0.0]
                point = fcurve.keyframe_points.insert(frame=float(co[0]), value=float(co[1]), options={'FAST'})
                interp = kp.get("interpolation") or 'LINEAR'
                point.interpolation = interp
            except Exception:
                continue
        try:
            fcurve.update()
        except Exception:
            pass

    return action


def _keyed_samples_from_local_matrices(samples):
    keyed = []
    for sample in (samples or []):
        if not isinstance(sample, dict):
            continue
        local_matrix = matrix_from_list(sample.get("matrix_local"))
        local = decompose_matrix(local_matrix)
        keyed.append({
            "frame": int(sample.get("frame", 0)),
            "location": local["location"],
            "rotation_quaternion": local["rotation_quaternion"],
            "scale": local["scale"],
        })
    return keyed


def _current_parent_world_at_frame(parent_obj, frame, scene, view_layer):
    if not parent_obj:
        return None
    current_frame = int(scene.frame_current)
    scene.frame_set(int(frame))
    try:
        view_layer.update()
    except Exception:
        pass
    out = parent_obj.matrix_world.copy()
    scene.frame_set(current_frame)
    try:
        view_layer.update()
    except Exception:
        pass
    return out


def _keyed_samples_from_saved_world(obj, track_anim, track_lookup, repair_target_names=None, scene=None):
    scene, view_layer = _scene_ctx(scene)
    repair_target_names = set(repair_target_names or [])
    keyed = []
    parent_obj = getattr(obj, "parent", None)
    parent_track_anim = track_lookup.get(parent_obj.name) if parent_obj else None

    for sample in (track_anim.get("samples") or []):
        if not isinstance(sample, dict):
            continue
        frame = int(sample.get("frame", 0))
        saved_world = matrix_from_list(sample.get("matrix_world"))

        if parent_obj is None:
            parent_world = None
        elif parent_track_anim and parent_obj.name in repair_target_names:
            parent_world = get_saved_world_matrix_at_frame(parent_track_anim, frame)
            if parent_world is None:
                parent_world = _current_parent_world_at_frame(parent_obj, frame, scene, view_layer)
        else:
            parent_world = _current_parent_world_at_frame(parent_obj, frame, scene, view_layer)

        local_matrix = compute_local_from_world(parent_world, saved_world)
        local = decompose_matrix(local_matrix)
        keyed.append({
            "frame": frame,
            "location": local["location"],
            "rotation_quaternion": local["rotation_quaternion"],
            "scale": local["scale"],
        })

    return keyed


def apply_local_samples_to_object(obj, anim_name, track_anim, scene=None):
    extra_fcurves = extract_non_transform_fcurves(track_anim.get("source_nla"))
    keyed_samples = _keyed_samples_from_local_matrices(track_anim.get("samples"))
    if not keyed_samples:
        return None

    try:
        obj.rotation_mode = 'QUATERNION'
    except Exception:
        pass

    action = _create_action_with_transform_keys(
        obj,
        f"UMZ__{anim_name}__{obj.name}__LOCAL",
        keyed_samples,
        extra_fcurves=extra_fcurves,
    )

    if not obj.animation_data:
        obj.animation_data_create()
    obj.animation_data.action = action
    pushdown_action_to_nla(obj, action, start_frame=keyed_samples[0]["frame"])
    try:
        obj.animation_data.action = None
    except Exception:
        pass
    return action


def repair_world_animation_track(obj, anim_name, track_anim, track_lookup, repair_target_names=None, scene=None):
    extra_fcurves = extract_non_transform_fcurves(track_anim.get("source_nla"))
    keyed_samples = _keyed_samples_from_saved_world(
        obj,
        track_anim,
        track_lookup,
        repair_target_names=repair_target_names,
        scene=scene,
    )
    if not keyed_samples:
        return None

    try:
        obj.rotation_mode = 'QUATERNION'
    except Exception:
        pass

    action = _create_action_with_transform_keys(
        obj,
        f"UMZ__{anim_name}__{obj.name}__WORLD",
        keyed_samples,
        extra_fcurves=extra_fcurves,
    )

    if not obj.animation_data:
        obj.animation_data_create()
    obj.animation_data.action = action
    pushdown_action_to_nla(obj, action, start_frame=keyed_samples[0]["frame"])
    try:
        obj.animation_data.action = None
    except Exception:
        pass
    return action


def detect_world_conflicts_for_entry(entry, scene=None, object_names=None, matrix_eps=1e-5, loc_eps=1e-4, rot_eps=1e-4, scale_eps=1e-4):
    scene, view_layer = _scene_ctx(scene)
    track_lookup = build_track_lookup(entry)
    wanted = {str(n).strip() for n in (object_names or []) if str(n).strip()} if object_names else None
    results = []
    current_frame = int(scene.frame_current)

    for obj_name, track_anim in track_lookup.items():
        if wanted is not None and obj_name not in wanted:
            continue
        obj = scene.objects.get(obj_name) or bpy.data.objects.get(obj_name)
        if obj is None:
            continue

        samples = track_anim.get("samples") or []
        if not samples:
            continue

        parent_saved = str(track_anim.get("captured_parent_name") or "")
        parent_current = obj.parent.name if getattr(obj, "parent", None) else ""
        parent_conflict = parent_saved != parent_current

        worst = None
        conflicts_count = 0
        first_frame = None
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            frame = int(sample.get("frame", 0))
            saved_world = matrix_from_list(sample.get("matrix_world"))
            scene.frame_set(frame)
            try:
                view_layer.update()
            except Exception:
                pass
            current_world = obj.matrix_world.copy()
            if matrices_almost_equal(current_world, saved_world, eps=matrix_eps):
                continue
            report = matrix_delta_report(current_world, saved_world)
            report["frame"] = frame
            conflicts_count += 1
            if first_frame is None:
                first_frame = frame
            if worst is None or (report["location_error"] + report["rotation_error"] + report["scale_error"]) > (worst["location_error"] + worst["rotation_error"] + worst["scale_error"]):
                worst = report

        if first_frame is None and not parent_conflict:
            continue

        scene.frame_set(current_frame)
        try:
            view_layer.update()
        except Exception:
            pass

        worst = worst or {
            "location_delta": [0.0, 0.0, 0.0],
            "scale_delta": [0.0, 0.0, 0.0],
            "location_error": 0.0,
            "rotation_error": 0.0,
            "scale_error": 0.0,
            "saved": {"rotation_euler": [0.0, 0.0, 0.0]},
            "current": {"rotation_euler": [0.0, 0.0, 0.0]},
        }

        location_conflict = worst["location_error"] > loc_eps
        rotation_conflict = worst["rotation_error"] > rot_eps
        scale_conflict = worst["scale_error"] > scale_eps

        results.append({
            "object_name": obj_name,
            "snapshot_parent": parent_saved,
            "current_parent": parent_current,
            "parent_conflict": bool(parent_conflict),
            "first_conflict_frame": int(first_frame if first_frame is not None else 0),
            "world_frames_checked": len(samples),
            "world_frames_conflicted": int(conflicts_count),
            "location_conflict": bool(location_conflict),
            "scale_conflict": bool(scale_conflict),
            "rotation_conflict": bool(rotation_conflict),
            "delta_location": [float(v) for v in worst["location_delta"]],
            "scale_delta": [float(v) for v in worst["scale_delta"]],
            "delta_rotation_euler": [
                float(worst["saved"]["rotation_euler"][i] - worst["current"]["rotation_euler"][i])
                for i in range(3)
            ],
            "max_location_error": float(worst["location_error"]),
            "max_rotation_error": float(worst["rotation_error"]),
            "max_scale_error": float(worst["scale_error"]),
            "world_conflict": bool(first_frame is not None),
        })

    return results
