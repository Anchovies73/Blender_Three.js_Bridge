bl_info = {
    "name": "Модуль: Библиотека анимаций",
    "author": "Vlad",
    "version": (0, 2, 2),
    "blender": (4, 0, 0),
}

# Тонкая обёртка для совместимости:
# главный __init__.py аддона импортирует procedural_films_module и зовёт register().
from .procedural_films.module_api import register, unregister