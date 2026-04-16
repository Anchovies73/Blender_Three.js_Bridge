# =========================================================
# КОНСТАНТЫ/НАСТРОЙКИ МОДУЛЯ "БИБЛИОТЕКА АНИМАЦИЙ"
# Здесь только значения и имена (никакой логики bpy).
# =========================================================

MODULE_ID = "procedural_films"
MODULE_NAME = "Библиотека анимаций"

# Имя Text-блока внутри .blend для хранения библиотеки
FILMS_TEXT_NAME = "procedural_animations.json"

# Настройки экспорта в three.js
ROT_BAKE_STEP_FRAMES = 24

# Камеру обычно лучше печь чаще (для гладкого движения)
CAMERA_BAKE_EVERY_FRAME = True
CAMERA_BAKE_STEP_FRAMES = 6

# Имя custom property для стабильного id ноды (для glTF/three)
GLTF_ID_PROP = "gltf_id"