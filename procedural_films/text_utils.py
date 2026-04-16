import bpy


def get_active_text_datablock():
    """
    Return active bpy.types.Text from any open TEXT_EDITOR area/space, else None.
    """
    try:
        wm = bpy.context.window_manager
        for window in wm.windows:
            screen = window.screen
            if not screen:
                continue
            for area in screen.areas:
                if area.type != "TEXT_EDITOR":
                    continue
                space = area.spaces.active
                txt = getattr(space, "text", None)
                if txt:
                    return txt
    except Exception:
        pass
    return None


def read_active_text():
    """
    Returns active text editor content as string, or None.
    """
    try:
        txt = get_active_text_datablock()
        if txt:
            return txt.as_string()
    except Exception:
        pass
    return None


def write_active_text(content: str) -> bool:
    """
    Overwrites CURRENT active text datablock with content; returns True/False.
    No uncaught exceptions.
    """
    try:
        txt = get_active_text_datablock()
        if not txt or content is None:
            return False
        txt.clear()
        txt.write(str(content))
        return True
    except Exception:
        return False
