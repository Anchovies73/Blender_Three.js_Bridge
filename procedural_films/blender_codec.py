import bpy

# =========================================================
# КОДЕК БИБЛИОТЕКИ АНИМАЦИЙ (Blender -> JSON entry -> Blender)
# Здесь только логика "как устроен entry" и как его собрать/восстановить.
# Файлы/кеш/папки — НЕ здесь (это в storage.py).
# =========================================================

# -------------------------
# Object meta ("m"): сериализация/восстановление
# -------------------------

# Фиксированный порядок мета-полей для массива "m" (НЕ менять без миграции)
META_KEYS_ORDER = (
    "position",           # 0 Позиция
    "oboznachenie",       # 1 Обозначение
    "naimenovanie",       # 2 Наименование
    "count_in_animation", # 3 Кол-во в анимации
    "count_in_zip",       # 4 Кол-во в ZIP
    "zip",                # 5 ZIP
    "fnn",                # 6 ФНН
    "proizvoditel",       # 7 Производитель
    "link",               # 8 Ссылка
)


def _as_str_or_empty(v):
    if v is None:
        return ""
    try:
        # IDProperty может быть числом/строкой/и т.п. — приводим к строке
        return str(v)
    except Exception:
        return ""


def serialize_object_meta(obj):
    """
    Возвращает массив m длиной 9 (строки) в фиксированном порядке META_KEYS_ORDER,
    но только если хотя бы одно поле (кроме имени и gltf_id) НЕ пустое.
    Если все пустые — возвращает None.
    """
    if obj is None:
        return None

    values = []
    has_any = False

    for k in META_KEYS_ORDER:
        v = ""
        try:
            # CP хранится как IDProperty: obj["key"]
            if k in obj.keys():
                v = _as_str_or_empty(obj.get(k))
            else:
                v = ""
        except Exception:
            v = ""

        if v.strip():
            has_any = True
        values.append(v)

    return values if has_any else None


def apply_object_meta(obj, meta_list):
    """
    Восстанавливает мета-поля из meta_list (массив "m") в CP объекта.
    """
    if obj is None:
        return
    if not meta_list or not isinstance(meta_list, (list, tuple)):
        return

    # Если список короче — заполним недостающее пустыми (на всякий случай)
    ml = list(meta_list) + ([""] * (len(META_KEYS_ORDER) - len(meta_list)))

    for idx, k in enumerate(META_KEYS_ORDER):
        try:
            obj[k] = _as_str_or_empty(ml[idx])
        except Exception:
            pass


# -------------------------
# Action: сериализация/десериализация
# -------------------------

def serialize_action(action):
    """Сериализует bpy.types.Action в словарь (включая fcurves и keyframes)."""
    if action is None:
        return None

    out = {"name": action.name, "frame_range": list(action.frame_range), "fcurves": []}

    for fc in action.fcurves:
        fc_out = {"data_path": fc.data_path, "array_index": fc.array_index, "keyframes": []}
        for kp in fc.keyframe_points:
            fc_out["keyframes"].append({
                "co": [kp.co.x, kp.co.y],
                "interpolation": kp.interpolation
            })
        out["fcurves"].append(fc_out)

    return out


def _clear_action_fcurves(action):
    if action is None:
        return
    try:
        for fc in list(action.fcurves):
            try:
                action.fcurves.remove(fc)
            except Exception:
                pass
    except Exception:
        pass


def deserialize_action(action_data, prefer_name=None, overwrite_existing=False):
    """
    Восстанавливает Action из словаря.

    По умолчанию ведёт себя по-старому: если Action уже существует,
    возвращает его как есть.

    Если overwrite_existing=True, то существующий Action с тем же именем
    ПЕРЕЗАПИСЫВАЕТСЯ данными из JSON. Это критично для кейса,
    когда мы уже исправили ключи в библиотеке, но Blender продолжает
    использовать старый datablock Action из памяти.
    """
    if not action_data:
        return None

    orig_name = action_data.get("name")
    desired = orig_name or prefer_name or "action"

    existing = None
    if orig_name and bpy.data.actions.get(orig_name):
        existing = bpy.data.actions.get(orig_name)
    elif prefer_name and bpy.data.actions.get(prefer_name):
        existing = bpy.data.actions.get(prefer_name)

    action = None
    created_new = False

    try:
        if existing is not None:
            if not overwrite_existing:
                return existing
            action = existing
            _clear_action_fcurves(action)
        else:
            name = desired
            base = name
            i = 1
            while bpy.data.actions.get(name) is not None:
                name = f"{base}_{i}"
                i += 1
            action = bpy.data.actions.new(name)
            created_new = True

        for fc in action_data.get("fcurves", []):
            dp = fc.get("data_path")
            idx = fc.get("array_index", 0)

            try:
                fcurve = action.fcurves.new(data_path=dp, index=idx)
            except Exception:
                continue

            for kp in fc.get("keyframes", []):
                co = kp.get("co", [0.0, 0.0])
                kfp = fcurve.keyframe_points.insert(
                    frame=co[0],
                    value=co[1],
                    options={'FAST'}
                )
                interp = kp.get("interpolation")
                if interp:
                    try:
                        kfp.interpolation = interp
                    except Exception:
                        pass

            try:
                fcurve.update()
            except Exception:
                pass

        try:
            action.update_tag()
        except Exception:
            pass

        return action

    except Exception:
        if action and created_new:
            try:
                bpy.data.actions.remove(action)
            except Exception:
                pass
        return None


# -------------------------
# NLA: сериализация/восстановление для объекта
# -------------------------

def serialize_nla_for_object(obj):
    """Сериализует NLA-треки объекта в словарь."""
    out_tracks = []
    ad = getattr(obj, "animation_data", None)

    if not ad:
        return {"active_action_name": None, "tracks": out_tracks}

    active_action_name = ad.action.name if ad.action else None

    for track in ad.nla_tracks:
        t = {"name": track.name, "strips": []}

        for strip in track.strips:
            s = {
                "name": strip.name,
                "frame_start": strip.frame_start,
                "frame_end": strip.frame_end,
                "action_frame_start": getattr(strip, "action_frame_start", None),
                "action_frame_end": getattr(strip, "action_frame_end", None),
                "action": serialize_action(strip.action) if strip.action else None,
                "repeat": getattr(strip, "repeat", None),
                "scale": getattr(strip, "scale", None),
                "influence": getattr(strip, "influence", None),
                "muted": getattr(strip, "mute", False),
                "blend_type": getattr(strip, "blend_type", "REPLACE"),
                "use_reverse": getattr(strip, "use_reverse", False),
            }
            t["strips"].append(s)

        out_tracks.append(t)

    return {"active_action_name": active_action_name, "tracks": out_tracks}


def deserialize_nla_for_object(obj, nla_tracks_struct):
    """
    Восстанавливает NLA-треки/стрипы объекта из словаря.

    Возвращает:
      (created_actions, saved_active_action_name)
    """
    created_actions = []
    if not nla_tracks_struct:
        return created_actions, None

    nla_tracks_data = nla_tracks_struct.get("tracks", [])
    active_action_name_saved = nla_tracks_struct.get("active_action_name")

    if not obj.animation_data:
        obj.animation_data_create()

    ad = obj.animation_data

    # Удаляем все существующие NLA треки
    try:
        for t in list(ad.nla_tracks):
            ad.nla_tracks.remove(t)
    except Exception:
        pass

    # Создаём новые треки/стрипы
    for t_idx, tdata in enumerate(nla_tracks_data):
        try:
            track = ad.nla_tracks.new()
            track.name = tdata.get("name", f"Track_{t_idx}")
        except Exception:
            continue

        for s_idx, sdata in enumerate(tdata.get("strips") or []):
            action_data = sdata.get("action")
            action_obj = None

            # ВАЖНО:
            # даже если Action уже существует в bpy.data, нам нужно
            # пересобрать его из JSON, иначе после исправления конфликта
            # Blender продолжит использовать старые ключи из памяти.
            if action_data:
                action_obj = deserialize_action(
                    action_data,
                    prefer_name=None,
                    overwrite_existing=True,
                )

            try:
                start = int(round(sdata.get("frame_start", 1.0)))
            except Exception:
                start = 1

            # Создаём strip
            try:
                strip = track.strips.new(sdata.get("name", f"Strip_{s_idx}"), start, action_obj)
            except Exception:
                try:
                    strip = track.strips.new(sdata.get("name", f"Strip_{s_idx}"), start, None)
                except Exception:
                    continue

            # На некоторых версиях Blender action может не назначиться с первого раза
            try:
                if action_obj and getattr(strip, "action", None) is None:
                    strip.action = action_obj
            except Exception:
                pass

            # Восстанавливаем дополнительные параметры (если они поддерживаются)
            try:
                if sdata.get("action_frame_start") is not None and hasattr(strip, "action_frame_start"):
                    strip.action_frame_start = sdata.get("action_frame_start")
            except Exception:
                pass
            try:
                if sdata.get("action_frame_end") is not None and hasattr(strip, "action_frame_end"):
                    strip.action_frame_end = sdata.get("action_frame_end")
            except Exception:
                pass
            try:
                if sdata.get("repeat") is not None and hasattr(strip, "repeat"):
                    strip.repeat = sdata.get("repeat")
            except Exception:
                pass
            try:
                if sdata.get("scale") is not None and hasattr(strip, "scale"):
                    strip.scale = sdata.get("scale")
            except Exception:
                pass
            try:
                if sdata.get("influence") is not None and hasattr(strip, "influence"):
                    strip.influence = sdata.get("influence")
            except Exception:
                pass
            try:
                if hasattr(strip, "mute"):
                    strip.mute = sdata.get("muted", False)
            except Exception:
                pass
            try:
                strip.blend_type = sdata.get("blend_type", "REPLACE")
            except Exception:
                pass
            try:
                if hasattr(strip, "use_reverse"):
                    strip.use_reverse = sdata.get("use_reverse", False)
            except Exception:
                pass

            if action_obj and action_obj not in created_actions:
                created_actions.append(action_obj)

    return created_actions, active_action_name_saved


def pushdown_action_to_nla(obj, action, start_frame=None):
    """Пушит Action в NLA объекта (создаёт новый трек и strip)."""
    if obj is None or action is None:
        return None

    if not obj.animation_data:
        obj.animation_data_create()

    if start_frame is None:
        try:
            fr = action.frame_range
            start_frame = fr[0] if fr else 1.0
        except Exception:
            start_frame = 1.0

    try:
        start = int(round(start_frame))
    except Exception:
        start = 1

    try:
        track = obj.animation_data.nla_tracks.new()
        track.name = f"Track_{action.name}"
        strip = track.strips.new(action.name, start, action)
        return strip
    except Exception:
        return None


# -------------------------
# Фильтр: надо ли включать объект в библиотеку
# -------------------------

def nla_has_transform_curves(nla_struct):
    """
    Проверяет, есть ли в NLA структуре хоть какие-то кривые,
    ради которых объект имеет смысл сохранять в библиотеку.

    Сейчас считаем важными:
      - location / rotation_* / scale
      - color или ["alpha"] (для alpha_tracks в three_*.json)
    """
    if not nla_struct:
        return False

    for tr in (nla_struct.get("tracks") or []):
        for st in (tr.get("strips") or []):
            act = (st.get("action") or {})
            for fc in (act.get("fcurves") or []):
                dp = fc.get("data_path")
                if dp in ("location", "rotation_euler", "rotation_quaternion", "scale"):
                    return True
                if dp == "color" or dp == '["alpha"]':
                    return True

    return False