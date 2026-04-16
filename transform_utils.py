import math
from mathutils import Matrix, Quaternion

MATRIX_EPS = 1e-5
LOCATION_EPS = 1e-4
ROTATION_EPS = 1e-4
SCALE_EPS = 1e-4


def log_debug(message: str):
    print(f"[UMZ][transform] {message}")


def matrix_to_list(mat):
    try:
        return [list(row) for row in mat]
    except Exception:
        return [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]


def matrix_from_list(data):
    try:
        return Matrix(data)
    except Exception:
        return Matrix.Identity(4)


def capture_local_matrix(obj):
    try:
        return obj.matrix_local.copy()
    except Exception:
        try:
            return obj.matrix_basis.copy()
        except Exception:
            return Matrix.Identity(4)


def capture_world_matrix(obj):
    try:
        return obj.matrix_world.copy()
    except Exception:
        return Matrix.Identity(4)


def decompose_matrix(mat):
    loc, rot, scale = mat.decompose()
    q = rot.to_quaternion() if hasattr(rot, "to_quaternion") else rot
    q.normalize()
    e = q.to_euler('XYZ')
    return {
        "location": [float(loc.x), float(loc.y), float(loc.z)],
        "rotation_quaternion": [float(q.w), float(q.x), float(q.y), float(q.z)],
        "rotation_euler": [float(e.x), float(e.y), float(e.z)],
        "scale": [float(scale.x), float(scale.y), float(scale.z)],
    }


def matrices_almost_equal(a, b, eps=MATRIX_EPS):
    try:
        for r in range(4):
            for c in range(4):
                if abs(float(a[r][c]) - float(b[r][c])) > eps:
                    return False
        return True
    except Exception:
        return False


def quaternion_angle(a, b):
    try:
        qa = Quaternion((float(a[0]), float(a[1]), float(a[2]), float(a[3]))) if not hasattr(a, "rotation_difference") else a
        qb = Quaternion((float(b[0]), float(b[1]), float(b[2]), float(b[3]))) if not hasattr(b, "rotation_difference") else b
        return float(qa.rotation_difference(qb).angle)
    except Exception:
        return math.pi


def matrix_delta_report(current, saved):
    cur = decompose_matrix(current)
    tgt = decompose_matrix(saved)
    loc_delta = [tgt["location"][i] - cur["location"][i] for i in range(3)]
    scale_delta = [tgt["scale"][i] - cur["scale"][i] for i in range(3)]
    rot_angle = quaternion_angle(cur["rotation_quaternion"], tgt["rotation_quaternion"])
    return {
        "location_delta": loc_delta,
        "location_error": max(abs(v) for v in loc_delta) if loc_delta else 0.0,
        "scale_delta": scale_delta,
        "scale_error": max(abs(v) for v in scale_delta) if scale_delta else 0.0,
        "rotation_error": rot_angle,
        "current": cur,
        "saved": tgt,
    }


def compute_local_from_world(parent_world_matrix, saved_world_matrix):
    if parent_world_matrix is None:
        return saved_world_matrix.copy()
    try:
        return parent_world_matrix.inverted() @ saved_world_matrix
    except Exception:
        return saved_world_matrix.copy()


def apply_local_matrix_to_object(obj, local_matrix, force_quaternion=True):
    """
    Apply a local matrix as the object's local transform relative to its current parent.
    To avoid parent-inverse drift, matrix_parent_inverse is normalized to Identity.
    """
    try:
        obj.matrix_parent_inverse = Matrix.Identity(4)
    except Exception:
        pass

    loc, rot, scale = local_matrix.decompose()
    try:
        obj.location = loc
    except Exception:
        pass
    try:
        obj.scale = scale
    except Exception:
        pass

    if force_quaternion:
        try:
            obj.rotation_mode = 'QUATERNION'
        except Exception:
            pass
        try:
            q = rot.to_quaternion() if hasattr(rot, "to_quaternion") else rot
            q.normalize()
            obj.rotation_quaternion = q
        except Exception:
            pass
    else:
        try:
            obj.rotation_euler = rot.to_euler(getattr(obj, "rotation_mode", 'XYZ') or 'XYZ')
        except Exception:
            try:
                obj.rotation_mode = 'QUATERNION'
                q = rot.to_quaternion() if hasattr(rot, "to_quaternion") else rot
                q.normalize()
                obj.rotation_quaternion = q
            except Exception:
                pass


def detect_world_transform_conflict(obj, saved_world_matrix, eps=MATRIX_EPS):
    current = capture_world_matrix(obj)
    mismatch = not matrices_almost_equal(current, saved_world_matrix, eps=eps)
    report = matrix_delta_report(current, saved_world_matrix)
    report["world_mismatch"] = bool(mismatch)
    return report


def repair_object_to_saved_world(obj, saved_world_matrix, parent_world_matrix=None):
    local_matrix = compute_local_from_world(parent_world_matrix, saved_world_matrix)
    apply_local_matrix_to_object(obj, local_matrix)
    return local_matrix
