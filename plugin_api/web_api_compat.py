"""AstrBot Web API registry 的唯一兼容边界。

当前 AstrBot stable 提供 ``Context.register_web_api()``，但没有公开的
``unregister_web_api()``。因此这里严格适配当前已验证的 registry 形状：
``registered_web_apis`` 是一个列表，元素前三项分别是 route、handler、methods。
如果 AstrBot 以后提供正式的解绑 API，应在这里替换实现，而不是把内部格式
判断扩散到插件业务代码或生命周期 harness。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


RegisteredRoute = tuple[str, object, tuple[str, ...]]


def unregister_web_apis(
    context: Any,
    owned_routes: Sequence[RegisteredRoute],
) -> None:
    """从当前 AstrBot registry 移除本实例拥有的 Web API。"""

    if not owned_routes:
        return

    registered_web_apis = getattr(context, "registered_web_apis", None)
    if not isinstance(registered_web_apis, list):
        raise TypeError(
            "当前 AstrBot 不提供兼容的 registered_web_apis list，"
            "无法安全解绑插件 Web API"
        )

    for registration in registered_web_apis:
        if not isinstance(registration, tuple) or len(registration) < 3:
            raise TypeError(
                "当前 AstrBot registered_web_apis 元素不是 "
                "(route, handler, methods, ...) tuple"
            )
        if not isinstance(registration[2], (list, tuple)):
            raise TypeError("当前 AstrBot registered_web_apis.methods 不是 list/tuple")

    def is_owned(registration: tuple[object, ...]) -> bool:
        route, handler, methods = registration[:3]
        return any(
            route == owned_route
            and handler is owned_handler
            and tuple(methods) == owned_methods
            for owned_route, owned_handler, owned_methods in owned_routes
        )

    registered_web_apis[:] = [
        registration
        for registration in registered_web_apis
        if not is_owned(registration)
    ]
