"""AstrBot Pixiv and check-in plugin.

Legacy package paths are registered here so existing preview scripts keep
working without leaving compatibility wrapper files in the project root.
"""

from importlib import import_module
import sys


_LEGACY_MODULES = {
    "ai_commenter": ".pixiv.commenter",
    "checkin_background": ".checkin.background",
    "checkin_birthday": ".checkin.birthday",
    "checkin_cache": ".checkin.cache",
    "checkin_card": ".checkin.card",
    "checkin_content": ".checkin.content",
    "checkin_greeting": ".checkin.greeting",
    "downloader": ".pixiv.downloader",
    "holiday_calendar": ".checkin.holiday",
    "image_index": ".pixiv.index",
    "pixiv_client": ".pixiv.client",
}

for _legacy_name, _module_path in _LEGACY_MODULES.items():
    sys.modules[f"{__name__}.{_legacy_name}"] = import_module(
        _module_path, package=__name__
    )

del _legacy_name, _module_path
