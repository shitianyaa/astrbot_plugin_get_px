from __future__ import annotations

from dataclasses import dataclass
import math

from PIL import Image

from .downloader import MAX_DOWNLOAD_BYTES


SELECTION_PARAMETERS = frozenset({
    "orientation", "aspect_ratio", "ratio_tolerance",
    "min_width", "min_height", "max_size_mib",
})


@dataclass(frozen=True)
class ImageSelection:
    """本次取图的附加约束；不修改画质、安全规则或管理员配置。"""

    orientation: str = "any"
    aspect_ratio: float | None = None
    ratio_tolerance: float = 0.1
    min_width: int = 0
    min_height: int = 0
    max_size_mib: float | None = None

    @classmethod
    def from_args(cls, args: dict) -> ImageSelection:
        orientation = args.get("orientation", "any")
        if not isinstance(orientation, str) or orientation not in (
            "any", "landscape", "portrait", "square",
        ):
            raise ValueError("方向必须为 any、landscape、portrait 或 square")
        for key in ("min_width", "min_height"):
            if key in args and (type(args[key]) is not int or args[key] < 0):
                raise ValueError("最小宽高必须为非负整数")
        for key in ("aspect_ratio", "ratio_tolerance", "max_size_mib"):
            if key not in args:
                continue
            value = args[key]
            try:
                finite = type(value) in (int, float) and math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite or value < 0 or (key != "ratio_tolerance" and value == 0):
                raise ValueError("比例与大小必须为有限正数，容差可为零")
        if "ratio_tolerance" in args and "aspect_ratio" not in args:
            raise ValueError("比例容差需要同时指定宽高比")
        if (
            "aspect_ratio" in args
            and "ratio_tolerance" in args
            and args["ratio_tolerance"] >= args["aspect_ratio"]
        ):
            raise ValueError("比例容差必须小于目标宽高比")
        if "max_size_mib" in args and args["max_size_mib"] < 1 / (1024 * 1024):
            raise ValueError("文件大小上限不能小于一个字节")
        values = {key: args[key] for key in SELECTION_PARAMETERS if key in args}
        if "aspect_ratio" in args and "ratio_tolerance" not in args:
            values["ratio_tolerance"] = min(0.1, args["aspect_ratio"] / 2)
        return cls(**values)

    @property
    def dimensions_required(self) -> bool:
        return (
            self.orientation != "any" or self.aspect_ratio is not None
            or self.min_width > 0 or self.min_height > 0
        )

    @property
    def active(self) -> bool:
        return self.dimensions_required or self.max_size_mib is not None

    def matches_dimensions(self, width: object, height: object) -> bool:
        if not self.dimensions_required:
            return True
        if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
            return False
        if width < self.min_width or height < self.min_height:
            return False
        if self.orientation == "landscape" and width <= height:
            return False
        if self.orientation == "portrait" and width >= height:
            return False
        if self.orientation == "square" and width != height:
            return False
        if self.aspect_ratio is not None:
            ratio = width / height
            return (
                self.aspect_ratio - self.ratio_tolerance
                <= ratio <= self.aspect_ratio + self.ratio_tolerance
            )
        return True

    def matches_file(self, path: str) -> bool:
        if not self.dimensions_required:
            return True
        # 只读取下载文件头的尺寸，不解码画面或调用视觉模型。
        with Image.open(path) as image:
            return self.matches_dimensions(*image.size)

    def size_limit_bytes(self, admin_limit_bytes: int) -> int:
        if self.max_size_mib is None:
            return admin_limit_bytes
        limit = (
            min(admin_limit_bytes, MAX_DOWNLOAD_BYTES)
            if admin_limit_bytes > 0 else MAX_DOWNLOAD_BYTES
        )
        # 先收紧到硬上限再换算，避免极大的有限输入乘法溢出。
        requested_bytes = int(
            min(self.max_size_mib, MAX_DOWNLOAD_BYTES / (1024 * 1024)) * 1024 * 1024
        )
        return min(limit, requested_bytes)
