bl_info = {
    "name": "УМЗ Панель",
    "author": "Vlad",
    "version": (0, 1, 0),
    "blender": (4, 0, 0),
    "location": "3D View > Sidebar > УМЗ Панель",
    "description": "Унифицированная панель: Сохранение сцен и Библиотека анимаций (NLA).",
    "category": "3D View",
}

import bpy
from bpy.props import StringProperty

# --- Простой реестр модулей ---
_REGISTERED_MODULES = []


def register_module(info: dict):
    if any(m.get("id") == info.get("id") for m in _REGISTERED_MODULES):
        return
    _REGISTERED_MODULES.append(info)


def unregister_module(mod_id: str):
    global _REGISTERED_MODULES
    _REGISTERED_MODULES = [m for m in _REGISTERED_MODULES if m.get("id") != mod_id]


def get_registered_modules():
    return list(_REGISTERED_MODULES)


# --- Preferences ---
class UMZPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    external_snapshots_folder: StringProperty(
        name="Папка для сцен (JSON)",
        subtype='DIR_PATH',
        default="",
        description="Папка для внешних JSON-файлов сценовых снимков"
    )
    external_animations_folder: StringProperty(
        name="Папка для анимаций (JSON)",
        subtype='DIR_PATH',
        default="",
        description="Папка для внешних JSON-файлов анимаций"
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "external_snapshots_folder")
        layout.prop(self, "external_animations_folder")


# --- Главная панель ---
class UMZ_PT_panel(bpy.types.Panel):
    bl_label = "УМЗ Панель"
    bl_idname = "UMZ_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'УМЗ Панель'

    def draw(self, context):
        layout = self.layout
        modules = get_registered_modules()
        if not modules:
            layout.label(text="(модули не зарегистрированы)")
            return

        for idx, mod in enumerate(modules):
            draw_fn = mod.get("draw")
            if callable(draw_fn):
                try:
                    draw_fn(layout, context)
                except Exception as e:
                    box = layout.box()
                    box.label(text=f"Ошибка UI: {e}")
            else:
                box = layout.box()
                box.label(text=f"{mod.get('name', 'Модуль')}: UI не предоставлен")

            if idx < len(modules) - 1:
                layout.separator(factor=0.5)


# --- Импорт модулей (они регистрируют себя через register_module) ---
from . import snapshot_module
from . import procedural_films_module

# --- Регистрация/отмена регистрации аддона ---
classes = (
    UMZPreferences,
    UMZ_PT_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    # register modules
    try:
        snapshot_module.register(register_module)
    except Exception as e:
        print("UMZ: snapshot module registration error:", e)
    try:
        procedural_films_module.register(register_module)
    except Exception as e:
        print("UMZ: animations module registration error:", e)


def unregister():
    try:
        snapshot_module.unregister()
    except Exception:
        pass
    try:
        procedural_films_module.unregister()
    except Exception:
        pass

    global _REGISTERED_MODULES
    _REGISTERED_MODULES = []

    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass


if __name__ == "__main__":
    register()