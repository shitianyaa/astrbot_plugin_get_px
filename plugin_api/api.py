from __future__ import annotations

import asyncio
import base64
from typing import Any

from astrbot.api.all import logger
from quart import jsonify, request, send_file

try:
    from ..checkin import load_checkin_snapshot_json
    from ..pixiv.history import DEFAULT_HISTORY_LIMIT
except ImportError:  # Direct imports used by the test suite.
    from checkin import load_checkin_snapshot_json
    from pixiv.history import DEFAULT_HISTORY_LIMIT


class PluginWebApi:
    """Register and serve plugin management endpoints."""

    def __init__(self, plugin: Any, *, plugin_name: str, log_prefix: str) -> None:
        self.plugin = plugin
        self.plugin_name = plugin_name
        self.log_prefix = log_prefix

    def __getattr__(self, name: str) -> Any:
        return getattr(self.plugin, name)

    def register(self) -> None:
        routes = (
            ("image-history", self.image_history, ["GET"], "List sent Pixiv image history"),
            ("image-history/thumb", self.image_history_thumb, ["GET"], "Get image history thumbnail"),
            ("image-history/thumb-data", self.image_history_thumb_data, ["GET"], "Get image history thumbnail as data URL"),
            ("image-history/thumb-data-batch", self.image_history_thumb_data_batch, ["POST"], "Get image history thumbnails as data URLs"),
            ("image-history/clear", self.image_history_clear, ["POST"], "Clear sent Pixiv image history"),
            ("image-history/delete", self.image_history_delete, ["POST"], "Delete one sent Pixiv image history record"),
            ("image-history/blacklist", self.image_history_blacklist, ["POST"], "Blacklist one Pixiv illustration from image history"),
            ("image-blacklist", self.image_blacklist, ["GET"], "List blacklisted Pixiv illustrations"),
            ("image-blacklist/thumb-data", self.image_blacklist_thumb_data, ["GET"], "Get blacklisted Pixiv illustration thumbnail as data URL"),
            ("image-blacklist/thumb-data-batch", self.image_blacklist_thumb_data_batch, ["POST"], "Get blacklisted Pixiv illustration thumbnails as data URLs"),
            ("image-blacklist/remove", self.image_blacklist_remove, ["POST"], "Remove one Pixiv illustration from blacklist"),
            ("config", self.config, ["GET"], "Get plugin web UI configuration"),
            ("checkin-import", self.checkin_import, ["POST"], "Import checkin backup snapshot"),
        )
        for path, handler, methods, description in routes:
            self.context.register_web_api(
                f"/{self.plugin_name}/{path}", handler, methods, description
            )

    def internal_error(self, action: str, exc: Exception):
        logger.error(f"{self.log_prefix} Web API {action} 失败: {exc}")
        return jsonify({"success": False, "error": self.plugin.WEB_INTERNAL_ERROR_MESSAGE if hasattr(self.plugin, "WEB_INTERNAL_ERROR_MESSAGE") else "服务内部错误，请稍后重试"}), 500

    async def image_history(self):
        if self.plugin.image_history is None:
            return jsonify({"success": False, "error": "图片历史尚未初始化"}), 503
        try:
            records = await self.plugin.image_history.list_records()
            return jsonify({"success": True, "records": records, "limit": DEFAULT_HISTORY_LIMIT})
        except Exception as exc:
            return self.internal_error("读取图片历史", exc)

    async def image_history_thumb(self):
        if self.plugin.image_history is None:
            return jsonify({"success": False, "error": "图片历史尚未初始化"}), 503
        record_id = request.args.get("id", "").strip()
        path = await self.plugin.image_history.get_thumbnail_path(record_id)
        if path is None:
            return jsonify({"success": False, "error": "缩略图不存在"}), 404
        try:
            return await send_file(str(path), mimetype="image/jpeg")
        except Exception as exc:
            return self.internal_error("读取图片历史缩略图", exc)

    async def image_history_thumb_data(self):
        if self.plugin.image_history is None:
            return jsonify({"success": False, "error": "图片历史尚未初始化"}), 503
        record_id = request.args.get("id", "").strip()
        path = await self.plugin.image_history.get_thumbnail_path(record_id)
        if path is None:
            return jsonify({"success": False, "error": "缩略图不存在"}), 404
        try:
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError as exc:
            return self.internal_error("读取图片历史缩略图数据", exc)
        return jsonify({"success": True, "record_id": record_id, "data_url": f"data:image/jpeg;base64,{encoded}"})

    async def image_history_thumb_data_batch(self):
        if self.plugin.image_history is None:
            return jsonify({"success": False, "error": "图片历史尚未初始化"}), 503
        payload = await request.get_json(silent=True) or {}
        ids = self._normalize_request_ids(payload.get("ids"))
        if not ids:
            return jsonify({"success": True, "thumbs": {}, "missing": []})
        paths = await self.plugin.image_history.get_thumbnail_paths(ids)
        try:
            thumbs = await asyncio.to_thread(self._encode_thumb_data_urls, paths)
        except OSError as exc:
            return self.internal_error("批量读取图片历史缩略图数据", exc)
        return jsonify({"success": True, "thumbs": thumbs, "missing": [item for item in ids if item not in thumbs]})

    async def image_history_clear(self):
        if self.plugin.image_history is None:
            return jsonify({"success": False, "error": "图片历史尚未初始化"}), 503
        try:
            deleted = await self.plugin.image_history.clear()
            logger.info(f"{self.log_prefix} WebUI 已清空图片历史: deleted={deleted}")
            return jsonify({"success": True, "deleted": deleted})
        except Exception as exc:
            return self.internal_error("清空图片历史", exc)

    async def image_history_delete(self):
        if self.plugin.image_history is None:
            return jsonify({"success": False, "error": "图片历史尚未初始化"}), 503
        payload = await request.get_json(silent=True) or {}
        record_id = str(payload.get("record_id") or "").strip()
        if not record_id:
            return jsonify({"success": False, "error": "record_id is required"}), 400
        try:
            deleted = await self.plugin.image_history.delete_record(record_id)
            if deleted is None:
                return jsonify({"success": False, "error": "记录不存在"}), 404
            logger.info(f"{self.log_prefix} WebUI 已删除图片历史记录: record_id={record_id}, illust_id={deleted.get('illust_id', '')}")
            return jsonify({"success": True, "record": deleted})
        except Exception as exc:
            return self.internal_error("删除图片历史记录", exc)

    async def image_history_blacklist(self):
        if self.plugin.image_history is None or self.plugin.image_index is None:
            return jsonify({"success": False, "error": "图片索引尚未初始化"}), 503
        payload = await request.get_json(silent=True) or {}
        record_id = str(payload.get("record_id") or "").strip()
        illust_id = str(payload.get("illust_id") or "").strip()
        record = await self.plugin.image_history.get_record(record_id) if record_id else None
        if record is not None:
            illust_id = str(record.get("illust_id") or illust_id).strip()
        if not illust_id:
            return jsonify({"success": False, "error": "illust_id is required"}), 400
        try:
            thumb_path = await self.plugin.image_history.get_thumbnail_path(record_id) if record_id else None
            thumb_id = await self.plugin.image_index.save_blacklist_thumbnail(illust_id=illust_id, source_path=thumb_path)
            await self.plugin.image_index.add_blacklist_illust(
                illust_id=illust_id,
                title=str((record or {}).get("title") or payload.get("title") or ""),
                author=str((record or {}).get("author") or payload.get("author") or ""),
                source=str((record or {}).get("source") or payload.get("source") or ""),
                record_id=record_id,
                thumb_id=thumb_id,
            )
            deleted = await self.plugin.image_history.delete_records_by_illust_id(illust_id)
            blacklist_record = next(
                (item for item in await self.plugin.image_index.list_blacklist_illusts() if str(item.get("illust_id") or "") == illust_id),
                None,
            )
            logger.info(f"{self.log_prefix} WebUI 已加入图片黑名单并清理历史记录: illust_id={illust_id}, deleted={len(deleted)}, thumb_id={thumb_id}")
            return jsonify({"success": True, "illust_id": illust_id, "deleted": len(deleted), "record": blacklist_record})
        except Exception as exc:
            return self.internal_error("加入作品黑名单", exc)

    async def image_blacklist(self):
        if self.plugin.image_index is None:
            return jsonify({"success": False, "error": "图片索引尚未初始化"}), 503
        try:
            records = await self.plugin.image_index.list_blacklist_illusts()
            return jsonify({"success": True, "records": records})
        except Exception as exc:
            return self.internal_error("读取作品黑名单", exc)

    async def image_blacklist_thumb_data(self):
        if self.plugin.image_index is None:
            return jsonify({"success": False, "error": "图片索引尚未初始化"}), 503
        illust_id = request.args.get("id", "").strip()
        path = await self.plugin.image_index.get_blacklist_thumbnail_path(illust_id)
        if path is None:
            return jsonify({"success": False, "error": "缩略图不存在"}), 404
        try:
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError as exc:
            return self.internal_error("读取黑名单缩略图数据", exc)
        return jsonify({"success": True, "illust_id": illust_id, "data_url": f"data:image/jpeg;base64,{encoded}"})

    async def image_blacklist_thumb_data_batch(self):
        if self.plugin.image_index is None:
            return jsonify({"success": False, "error": "图片索引尚未初始化"}), 503
        payload = await request.get_json(silent=True) or {}
        ids = self._normalize_request_ids(payload.get("ids"))
        if not ids:
            return jsonify({"success": True, "thumbs": {}, "missing": []})
        paths = await self.plugin.image_index.get_blacklist_thumbnail_paths(ids)
        try:
            thumbs = await asyncio.to_thread(self._encode_thumb_data_urls, paths)
        except OSError as exc:
            return self.internal_error("批量读取黑名单缩略图数据", exc)
        return jsonify({"success": True, "thumbs": thumbs, "missing": [item for item in ids if item not in thumbs]})

    async def image_blacklist_remove(self):
        if self.plugin.image_index is None:
            return jsonify({"success": False, "error": "图片索引尚未初始化"}), 503
        payload = await request.get_json(silent=True) or {}
        illust_id = str(payload.get("illust_id") or "").strip()
        if not illust_id:
            return jsonify({"success": False, "error": "illust_id is required"}), 400
        try:
            removed = await self.plugin.image_index.remove_blacklist_illust(illust_id)
            if removed is None:
                return jsonify({"success": False, "error": "黑名单记录不存在"}), 404
            return jsonify({"success": True, "record": removed})
        except Exception as exc:
            return self.internal_error("移出作品黑名单", exc)

    async def config(self):
        font_source = self.plugin._cfg_str("webui_font_source", "mirror")
        font_urls = {
            "mirror": "https://fonts.googleapis.cn/css2?family=Noto+Sans+SC:wght@400;500;700&family=Noto+Serif+SC:wght@400;500;700&display=swap",
            "official": "https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400;500;700&family=Noto+Serif+SC:wght@400;500;700&display=swap",
        }
        return jsonify({"success": True, "font_source": font_source, "font_url": font_urls.get(font_source, "")})

    async def checkin_import(self):
        if self.plugin.checkin_store is None:
            return jsonify({"success": False, "error": "签到数据尚未初始化"}), 503
        try:
            files = await request.files
            uploaded = []
            for key in files.keys():
                uploaded.extend(files.getlist(key))
            if not uploaded:
                return jsonify({"success": False, "error": "缺少备份文件"}), 400
            if len(uploaded) != 1:
                return jsonify({"success": False, "error": "一次只能上传 1 个备份文件"}), 400
            upload = uploaded[0]
            filename = str(getattr(upload, "filename", "") or "").strip()
            if not filename:
                return jsonify({"success": False, "error": "备份文件名不能为空"}), 400
            if not filename.lower().endswith(".json"):
                return jsonify({"success": False, "error": "只支持导入 JSON 备份文件"}), 400
            raw = await self.plugin._read_uploaded_file_bytes(upload)
            snapshot = load_checkin_snapshot_json(raw)
            rollback_path = await self.plugin._write_checkin_snapshot_backup(prefix="checkin-import-backup")
            result = await self.plugin.checkin_store.import_snapshot(snapshot)
            return jsonify({
                "success": True,
                "filename": filename,
                "profiles": result["profiles"],
                "records": result["records"],
                "preferences": result.get("preferences", 0),
                "global_events": result.get("global_events", 0),
                "achievements": result.get("achievements", 0),
                "exported_at": result["exported_at"],
                "imported_at": result["imported_at"],
                "rollback_path": str(rollback_path),
            })
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        except Exception as exc:
            return self.internal_error("导入签到备份", exc)

    @staticmethod
    def _normalize_request_ids(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        result: list[str] = []
        seen: set[str] = set()
        for item in value:
            normalized = str(item or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
            if len(result) >= 32:
                break
        return result

    @staticmethod
    def _encode_thumb_data_urls(paths: dict[str, object]) -> dict[str, str]:
        result: dict[str, str] = {}
        for record_id, path in paths.items():
            try:
                encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            except (AttributeError, OSError):
                continue
            result[str(record_id)] = f"data:image/jpeg;base64,{encoded}"
        return result
