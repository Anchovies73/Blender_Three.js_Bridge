import os
import bpy
from bpy.props import StringProperty, BoolProperty, IntProperty, CollectionProperty, EnumProperty

from .constants import MODULE_ID, MODULE_NAME
from . import storage
from .ops import collect_snapshot, delete_snapshot, restore_snapshot, find_missing_from_blend
from .utils import format_created


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
            name = (getattr(it, "name", "") or "").lower()
            flt_flags.append(self.bitflag_filter_item if filter_str in name else 0)
        return flt_flags, flt_neworder


def snapshot_items(self, context):
    snaps = storage.get_cached_snapshots()
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
    snaps = storage.get_cached_snapshots() or {}
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

    if prev and prev in names:
        idx = names.index(prev)
    else:
        idx = max(0, min(prev_idx, len(names) - 1))

    scene.umz_snapshot_list_index = idx
    scene.umz_selected_snapshot = names[idx]


def register_scene_props():
    if not hasattr(bpy.types.Scene, "umz_selected_snapshot"):
        bpy.types.Scene.umz_selected_snapshot = EnumProperty(name="Снимок", items=snapshot_items)
    if not hasattr(bpy.types.Scene, "umz_snapshot_list"):
        bpy.types.Scene.umz_snapshot_list = CollectionProperty(type=UMZ_SnapshotItem)
    if not hasattr(bpy.types.Scene, "umz_snapshot_list_index"):
        bpy.types.Scene.umz_snapshot_list_index = IntProperty(default=0, update=_on_snapshot_list_index_changed)
    if not hasattr(bpy.types.Scene, "umz_missing_objects_count"):
        bpy.types.Scene.umz_missing_objects_count = IntProperty(name="Потерянные объекты", default=0)
    if not hasattr(bpy.types.Scene, "umz_missing_snapshot_name"):
        bpy.types.Scene.umz_missing_snapshot_name = StringProperty(name="Снимок с потерями", default="")
    if not hasattr(bpy.types.Scene, "umz_snapshot_keep_new"):
        bpy.types.Scene.umz_snapshot_keep_new = BoolProperty(
            name="Не удалять новое",
            description="При загрузке снимка не удалять объекты, созданные после снимка",
            default=False,
        )
    if not hasattr(bpy.types.Scene, "umz_ui_snapshot_open"):
        bpy.types.Scene.umz_ui_snapshot_open = BoolProperty(name="Сохранение сцен", default=True)


def unregister_scene_props():
    for prop in (
        "umz_selected_snapshot",
        "umz_snapshot_list",
        "umz_snapshot_list_index",
        "umz_missing_objects_count",
        "umz_missing_snapshot_name",
        "umz_snapshot_keep_new",
        "umz_ui_snapshot_open",
    ):
        if hasattr(bpy.types.Scene, prop):
            try:
                delattr(bpy.types.Scene, prop)
            except Exception:
                pass


class SNAPSHOT_OT_save(bpy.types.Operator):
    bl_idname = "umz.snapshot_save"
    bl_label = "Сохранить сцену"

    name: StringProperty(name="Имя", default="snapshot1")

    def execute(self, context):
        try:
            collect_snapshot(self.name)
            _rebuild_snapshot_list(context.scene, prefer_name=self.name)
            try:
                context.scene.umz_selected_snapshot = self.name
            except Exception:
                pass
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

        storage.mark_cache_dirty()
        try:
            _rebuild_snapshot_list(context.scene)
        except Exception:
            pass
        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


class SNAPSHOT_OT_clear_dir(bpy.types.Operator):
    bl_idname = "umz.snapshot_clear_directory"
    bl_label = "Очистить папку"

    def execute(self, context):
        ok = storage.clear_external_folder_pref()
        if not ok:
            self.report({'ERROR'}, "Не удалось очистить настройку папки.")
            return {'CANCELLED'}
        storage.mark_cache_dirty()
        try:
            _rebuild_snapshot_list(context.scene)
        except Exception:
            pass
        return {'FINISHED'}


class SNAPSHOT_OT_refresh_list(bpy.types.Operator):
    bl_idname = "umz.snapshot_refresh_list"
    bl_label = "Синхронизировать"

    def execute(self, context):
        storage.mark_cache_dirty()
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
                try:
                    _rebuild_snapshot_list(context.scene)
                except Exception:
                    pass
                all_names = list(storage.get_cached_snapshots().keys())
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

        snap_name = getattr(scene, "umz_selected_snapshot", "")
        if not storage.get_cached_snapshots().get(snap_name):
            self.report({'ERROR'}, "Снимок для восстановления не найден.")
            return {'CANCELLED'}

        restored = find_missing_from_blend(snap_name, blend_path)
        if restored > 0:
            self.report({'INFO'}, f"Восстановлено объектов: {restored}")
        else:
            self.report({'INFO'}, "Совпадающих объектов в выбранном .blend не найдено.")
        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


def draw_snapshot_content(layout, context):
    scene = context.scene

    addon = __name__.split('.')[0]
    prefs = None
    try:
        prefs = bpy.context.preferences.addons[addon].preferences
    except Exception:
        pass

    folder = bpy.path.abspath(getattr(prefs, 'external_snapshots_folder', '')) if (prefs and getattr(prefs, 'external_snapshots_folder', '')) else ""
    row_dir = layout.row(align=True)
    if folder:
        row_dir.label(text=folder, icon='FILE_FOLDER')
        row_dir.operator("umz.snapshot_set_directory", text="", icon='FILE_FOLDER')
        row_dir.operator("umz.snapshot_clear_directory", text="", icon='X')
    else:
        row_dir.scale_y = 1.2
        row_dir.operator("umz.snapshot_set_directory", text="Папка для сцен", icon='FILE_FOLDER')

    layout.prop(scene, "umz_snapshot_keep_new", text="Не удалять новое")

    snaps = storage.get_cached_snapshots()
    sel_name = getattr(scene, "umz_selected_snapshot", "")
    if sel_name and sel_name not in snaps:
        new_sel = next(iter(snaps.keys()), "")
        try:
            scene.umz_selected_snapshot = new_sel
        except Exception:
            pass
        sel_name = new_sel

    try:
        _rebuild_snapshot_list(scene, prefer_name=sel_name)
    except Exception:
        pass

    row = layout.row()
    row.template_list(
        "SNAPSHOT_UL_umz_list",
        "",
        scene,
        "umz_snapshot_list",
        scene,
        "umz_snapshot_list_index",
        rows=6,
    )

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

    if not snaps:
        layout.label(text="Сцен нет")
    else:
        snap_data = snaps.get(getattr(scene, 'umz_selected_snapshot', ''))
        if snap_data:
            created = snap_data.get("created_at", "(нет даты)")
            layout.label(text=f"Создано: {format_created(created)}")

    missing_count = getattr(scene, "umz_missing_objects_count", 0)
    missing_for = getattr(scene, "umz_missing_snapshot_name", "")
    current_snap = getattr(scene, "umz_selected_snapshot", "")

    if missing_count > 0 and missing_for and (missing_for == current_snap):
        warn_box = layout.box()
        row = warn_box.row(align=True)
        row.label(text=f"Потерянные объекты: {missing_count}", icon='ERROR')
        row.operator("umz.snapshot_find_missing_from_blend", text="Найти в .blend", icon='FILE_FOLDER')


def draw_ui(layout, context):
    scene = context.scene

    header = layout.row(align=True)
    is_open = bool(getattr(scene, "umz_ui_snapshot_open", True))
    icon = 'TRIA_DOWN' if is_open else 'TRIA_RIGHT'
    header.prop(scene, "умз_ui_snapshot_open" if False else "umz_ui_snapshot_open", text="Сохранение сцен", icon=icon, emboss=False)

    if not is_open:
        return

    draw_snapshot_content(layout, context)


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
    global _registered, _register_cb
    if _registered:
        return
    for c in classes:
        bpy.utils.register_class(c)
    register_scene_props()
    storage.mark_cache_dirty()
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
    global _registered, _register_cb
    if not _registered:
        return
    for c in reversed(classes):
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass
    unregister_scene_props()
    _registered = False
    _register_cb = None
