from __future__ import annotations

import asyncio
import base64
from datetime import datetime
from io import BytesIO
import json
import math
from pathlib import Path
import time
from typing import Any

from astrbot.api import logger
from quart import jsonify, request, send_file

try:
    from ..checkin import load_checkin_snapshot_json
    from ..pixiv.downloader import cleanup, format_log_size, pick_image_url_exact
    from ..pixiv.safety import matched_illustration_term, normalize_illust_ids
    from ..group_safety import TERMS_KEY, IDS_KEY
    from .web_api_compat import unregister_web_apis
    from .settings import save_settings, settings_snapshot
except ImportError:
    # Direct imports used by the test suite.
    from checkin import load_checkin_snapshot_json
    from pixiv.downloader import cleanup, format_log_size, pick_image_url_exact
    from pixiv.safety import matched_illustration_term, normalize_illust_ids
    from group_safety import TERMS_KEY, IDS_KEY
    from plugin_api.web_api_compat import unregister_web_apis
    from plugin_api.settings import save_settings, settings_snapshot


class PluginWebApi:
    """Register and serve the plugin management center endpoints."""

    def __init__(
        self,
        plugin: Any,
        *,
        plugin_name: str,
        log_prefix: str,
        internal_error_message: str,
    ) -> None:
        self.plugin = plugin
        self.plugin_name = plugin_name
        self.log_prefix = log_prefix
        self.internal_error_message = internal_error_message
        self._registered_routes: list[tuple[str, object, tuple[str, ...]]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.plugin, name)

    def register(self) -> None:
        routes = (
            ("overview", self.overview, ["GET"], "Get management overview"),
            ("checkin-groups", self.checkin_groups, ["GET"], "List check-in groups"),
            (
                "checkin-ranking",
                self.checkin_ranking,
                ["GET"],
                "Get group check-in ranking",
            ),
            ("checkin-trend", self.checkin_trend, ["GET"], "Get group check-in trend"),
            (
                "checkin-members",
                self.checkin_members,
                ["GET"],
                "List check-in member profiles",
            ),
            (
                "checkin-members/update",
                self.checkin_member_update,
                ["POST"],
                "Update check-in member profile values",
            ),
            (
                "content-safety",
                self.content_safety,
                ["GET"],
                "Get content safety policy",
            ),
            ("content-safety/global", self.content_safety_global, ["POST"], "Update global safety rules"),
            ("content-safety/groups/save", self.content_safety_group_save, ["POST"], "Save a safety group"),
            ("content-safety/groups/remove", self.content_safety_group_remove, ["POST"], "Remove a safety group"),
            (
                "content-safety/terms/add",
                self.content_safety_term_add,
                ["POST"],
                "Add custom safety term",
            ),
            (
                "content-safety/terms/remove",
                self.content_safety_term_remove,
                ["POST"],
                "Remove custom safety term",
            ),
            (
                "image-blacklist",
                self.image_blacklist,
                ["GET"],
                "List blacklisted Pixiv illustrations",
            ),
            (
                "image-blacklist/add",
                self.image_blacklist_add,
                ["POST"],
                "Add Pixiv illustration to blacklist",
            ),
            (
                "image-blacklist/remove",
                self.image_blacklist_remove,
                ["POST"],
                "Remove Pixiv illustration from blacklist",
            ),
            (
                "image-blacklist/thumb-data-batch",
                self.image_blacklist_thumb_data_batch,
                ["POST"],
                "Get blacklist thumbnails",
            ),
            ("data-export", self.data_export, ["GET"], "Export plugin data and legacy safety archives"),
            ("checkin-import", self.checkin_import, ["POST"], "Import check-in backup"),
            (
                "cache-storage",
                self.cache_storage,
                ["GET"],
                "Get cache storage statistics",
            ),
            (
                "cache-storage/clear",
                self.cache_storage_clear,
                ["POST"],
                "Clear temporary cache storage",
            ),
            ("config", self.config, ["GET"], "Get management center configuration"),
            ("settings", self.settings, ["GET"], "Get plugin settings for the management center"),
            ("settings/update", self.settings_update, ["POST"], "Update plugin settings from the management center"),
        )
        for path, handler, methods, description in routes:
            route = f"/{self.plugin_name}/{path}"
            self.context.register_web_api(route, handler, methods, description)
            self._registered_routes.append((route, handler, tuple(methods)))

    def unregister(self) -> None:
        """移除本实例注册的 Web API 路由。

        AstrBot 当前没有公开的 ``unregister_web_api()``；唯一的 registry
        兼容处理集中在 ``plugin_api.web_api_compat``，避免业务代码散落
        AstrBot 内部存储格式假设。
        """
        if not self._registered_routes:
            return

        unregister_web_apis(self.context, tuple(self._registered_routes))
        self._registered_routes.clear()

    def internal_error(self, action: str, exc: Exception):
        logger.error(
            f"{self.log_prefix} Web API {action}失败: "
            f"error_type={type(exc).__name__}"
        )
        return jsonify({"success": False, "error": self.internal_error_message}), 500

    async def overview(self):
        if self.plugin.checkin_store is None or self.plugin.image_index is None:
            return self._unavailable()
        try:
            checkin, safety = await asyncio.gather(
                self.plugin.checkin_store.get_checkin_overview(),
                self.plugin.safety_service.settings(),
            )
            return jsonify(
                {
                    "success": True,
                    **checkin,
                    "blacklist_count": len(safety[IDS_KEY]),
                    "custom_term_count": len(safety[TERMS_KEY]),
                    "latest_backup_at": self._latest_backup_at(),
                }
            )
        except Exception as exc:
            return self.internal_error("读取概览", exc)

    async def checkin_groups(self):
        if self.plugin.checkin_store is None:
            return self._unavailable("签到数据尚未初始化")
        try:
            groups = await self.plugin.checkin_store.list_checkin_groups()
            return jsonify({"success": True, "groups": groups})
        except Exception as exc:
            return self.internal_error("读取群列表", exc)

    async def checkin_ranking(self):
        if self.plugin.checkin_store is None:
            return self._unavailable("签到数据尚未初始化")
        try:
            limit = self._parse_int(request.args.get("limit", "10"), 1, 100)
            group_id = await self._require_known_group(request.args.get("group_id", ""))
            result = await self.plugin.checkin_store.get_group_ranking(
                group_id=group_id,
                ranking_type=request.args.get("type", "today"),
                month=request.args.get("month", ""),
                limit=limit,
            )
            result.pop("all_entries", None)
            return jsonify({"success": True, **result})
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        except Exception as exc:
            return self.internal_error("读取签到排行", exc)

    async def checkin_trend(self):
        if self.plugin.checkin_store is None:
            return self._unavailable("签到数据尚未初始化")
        try:
            days = self._parse_int(request.args.get("days", "7"), 7, 30)
            if days not in (7, 30):
                raise ValueError("days must be 7 or 30")
            group_id = await self._require_known_group(request.args.get("group_id", ""))
            trend = await self.plugin.checkin_store.get_group_trend(
                group_id=group_id, days=days
            )
            return jsonify(
                {
                    "success": True,
                    "group_id": group_id,
                    "days": days,
                    "trend": trend,
                }
            )
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        except Exception as exc:
            return self.internal_error("读取签到趋势", exc)

    async def checkin_members(self):
        if self.plugin.checkin_store is None:
            return self._unavailable("签到数据尚未初始化")
        try:
            limit = self._parse_int(request.args.get("limit", "50"), 1, 100)
            offset = self._parse_int(request.args.get("offset", "0"), 0, 1_000_000)
            query = str(request.args.get("query", "") or "").strip()
            result = await self.plugin.checkin_store.list_checkin_members(
                query=query,
                limit=limit,
                offset=offset,
            )
            return jsonify(
                {
                    "success": True,
                    "query": query,
                    "limit": limit,
                    "offset": offset,
                    **result,
                }
            )
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        except Exception as exc:
            return self.internal_error("读取签到成员", exc)

    async def checkin_member_update(self):
        if self.plugin.checkin_store is None:
            return self._unavailable("签到数据尚未初始化")
        payload = await self._request_json_object()
        if payload is None:
            return jsonify({"success": False, "error": "请求内容必须是对象"}), 400
        user_id = str(payload.get("user_id") or "").strip()
        boost_action = str(payload.get("boost_action") or "keep").strip()
        try:
            result = await self.plugin.checkin_store.update_checkin_member(
                user_id=user_id,
                coins=self._parse_profile_integer(payload.get("coins"), "金币"),
                affection=self._parse_profile_affection(payload.get("affection")),
                total_days=self._parse_profile_integer(
                    payload.get("total_days"), "累计签到"
                ),
                streak_days=self._parse_profile_integer(
                    payload.get("streak_days"), "连续签到"
                ),
                boost_action=boost_action,
            )
            before = result["before"]
            member = result["member"]
            logger.info(
                f"{self.log_prefix} 管理页调整签到成员数值: "
                f"coins={before['coins']}->{member['coins']} "
                f"affection={before['affection']}->{member['affection']} "
                f"total_days={before['total_days']}->{member['total_days']} "
                f"streak_days={before['streak_days']}->{member['streak_days']} "
                f"boost_status={before.get('boost_status')}->{member.get('boost_status')}"
            )
            return jsonify({"success": True, "member": member})
        except LookupError as exc:
            return jsonify({"success": False, "error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        except Exception as exc:
            return self.internal_error("调整签到成员数值", exc)

    async def content_safety(self):
        service = getattr(self.plugin, "safety_service", None)
        if service is None:
            return self._unavailable("安全策略尚未初始化")
        try:
            return jsonify({"success": True, **await service.settings()})
        except Exception as exc:
            return self.internal_error("读取安全策略", exc)

    async def _safety_update(self, action: str):
        service = getattr(self.plugin, "safety_service", None)
        if service is None:
            return self._unavailable("安全策略尚未初始化")
        payload = await self._request_json_object()
        if payload is None:
            return jsonify({"success": False, "error": "请求内容必须是对象"}), 400
        try:
            if action == "global":
                result = await service.update_global(payload)
            elif action == "group":
                result = {"group": await service.save_group(payload)}
            elif action == "remove":
                await service.remove_group(payload.get("group_id"))
                result = {}
            else:
                result = await service.change_global_item(TERMS_KEY, payload.get("term"), remove=action == "term_remove")
            return jsonify({"success": True, **result})
        except LookupError as exc:
            return jsonify({"success": False, "error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        except Exception as exc:
            return self.internal_error("保存安全策略", exc)

    async def content_safety_global(self):
        return await self._safety_update("global")

    async def content_safety_group_save(self):
        return await self._safety_update("group")

    async def content_safety_group_remove(self):
        return await self._safety_update("remove")

    async def content_safety_term_add(self):
        return await self._safety_update("term_add")

    async def content_safety_term_remove(self):
        return await self._safety_update("term_remove")

    async def image_blacklist(self):
        if self.plugin.image_index is None:
            return self._unavailable("作品黑名单尚未初始化")
        try:
            settings = await self.plugin.safety_service.settings()
            metadata = {item["illust_id"]: item for item in await self.plugin.image_index.list_blacklist_illusts()}
            records = [metadata.get(illust_id, {"illust_id": illust_id}) for illust_id in settings[IDS_KEY]]
            return jsonify({"success": True, "records": records})
        except Exception as exc:
            return self.internal_error("读取作品黑名单", exc)

    async def image_blacklist_add(self):
        if self.plugin.image_index is None:
            return self._unavailable("作品黑名单尚未初始化")
        payload = await self._request_json_object()
        if payload is None:
            return jsonify({"success": False, "error": "请求内容必须是对象"}), 400
        try:
            illust_id = normalize_illust_ids([payload.get("illust_id")])[0]
        except ValueError:
            return jsonify({"success": False, "error": "请输入有效的 Pixiv 作品 ID"}), 400
        reason = str(payload.get("reason") or "").strip()
        if len(reason) > 200:
            return jsonify({"success": False, "error": "原因不能超过 200 个字符"}), 400
        title = author = ""
        illust = None
        if self.plugin.client is not None:
            try:
                illust = await self.plugin.client.illust_detail(int(illust_id))
                if illust:
                    title = str(illust.get("title") or "")
                    author = str((illust.get("user") or {}).get("name") or "")
            except Exception as exc:
                logger.warning(
                    f"{self.log_prefix} 黑名单作品信息获取失败: "
                    f"illust_id={illust_id} error_type={type(exc).__name__}"
                )
        try:
            await self.plugin.safety_service.change_global_item(IDS_KEY, illust_id)
        except Exception as exc:
            return self.internal_error("保存作品黑名单", exc)
        try:
            await self.plugin.image_index.add_blacklist_illust(
                illust_id=illust_id,
                title=title,
                author=author,
                source="manual",
                reason=reason,
                added_by="web",
            )
            if illust is not None:
                await self._try_save_blacklist_thumbnail(illust_id, illust)
            record = next(
                (
                    item
                    for item in await self.plugin.image_index.list_blacklist_illusts()
                    if str(item.get("illust_id") or "") == illust_id
                ),
                None,
            )
            return jsonify({"success": True, "record": record})
        except Exception as exc:
            logger.warning(f"{self.log_prefix} 黑名单已保存，辅助信息更新失败: illust_id={illust_id} error_type={type(exc).__name__}")
            return jsonify({"success": True, "record": {"illust_id": illust_id}, "warning": "黑名单已生效，作品辅助信息未保存"})

    async def _try_save_blacklist_thumbnail(
        self, illust_id: str, illust: dict[str, Any]
    ) -> None:
        downloader = getattr(self.plugin, "downloader", None)
        if downloader is None or not await self._thumbnail_is_safe(illust):
            return
        url = next(
            (
                candidate
                for quality in ("square_medium", "medium", "large")
                if (candidate := pick_image_url_exact(illust, quality))
            ),
            "",
        )
        if not url:
            return
        temp_path = ""
        try:
            timeout = 30.0
            cfg_float = getattr(self.plugin, "_cfg_float", None)
            if callable(cfg_float):
                timeout = cfg_float("request_timeout", 30.0, 5.0, 120.0)
            temp_path, _ = await downloader.download(
                url,
                timeout=timeout,
            )
            thumb_id = await self.plugin.image_index.save_blacklist_thumbnail(
                illust_id=illust_id,
                source_path=temp_path,
            )
            if thumb_id:
                await self.plugin.image_index.add_blacklist_illust(
                    illust_id=illust_id,
                    thumb_id=thumb_id,
                )
        except Exception as exc:
            logger.warning(
                f"{self.log_prefix} 黑名单缩略图获取失败: "
                f"illust_id={illust_id} error_type={type(exc).__name__}"
            )
        finally:
            cleanup(temp_path)

    async def _thumbnail_is_safe(self, illust: dict[str, Any]) -> bool:
        if int(illust.get("x_restrict", 0) or 0) != 0:
            return False
        try:
            policy = await self.plugin.safety_service.global_policy()
        except Exception as exc:
            logger.warning(
                f"{self.log_prefix} 黑名单缩略图安全检查失败: "
                f"error_type={type(exc).__name__}"
            )
            return False
        return not matched_illustration_term(illust, policy)

    async def image_blacklist_remove(self):
        if self.plugin.image_index is None:
            return self._unavailable("作品黑名单尚未初始化")
        payload = await self._request_json_object()
        if payload is None:
            return jsonify({"success": False, "error": "请求内容必须是对象"}), 400
        try:
            illust_id = normalize_illust_ids([payload.get("illust_id")])[0]
        except ValueError:
            return jsonify({"success": False, "error": "请输入有效的 Pixiv 作品 ID"}), 400
        try:
            await self.plugin.safety_service.change_global_item(IDS_KEY, illust_id, remove=True)
        except Exception as exc:
            return self.internal_error("解除作品黑名单", exc)
        try:
            # SQLite 仅存辅助信息，不决定黑名单是否生效。
            removed = await self.plugin.image_index.remove_blacklist_illust(illust_id)
            return jsonify({"success": True, "record": removed or {"illust_id": illust_id}})
        except Exception as exc:
            logger.warning(f"{self.log_prefix} 黑名单已解除，辅助信息清理失败: illust_id={illust_id} error_type={type(exc).__name__}")
            return jsonify({"success": True, "record": {"illust_id": illust_id}, "warning": "黑名单已解除，辅助信息未清理"})

    async def image_blacklist_thumb_data_batch(self):
        if self.plugin.image_index is None:
            return self._unavailable("作品黑名单尚未初始化")
        payload = await self._request_json_object()
        if payload is None:
            return jsonify({"success": False, "error": "请求内容必须是对象"}), 400
        ids = self._normalize_request_ids(payload.get("ids"))
        if not ids:
            return jsonify({"success": True, "thumbs": {}, "missing": []})
        paths = await self.plugin.image_index.get_blacklist_thumbnail_paths(ids)
        thumbs = await asyncio.to_thread(self._encode_thumb_data_urls, paths)
        return jsonify(
            {
                "success": True,
                "thumbs": thumbs,
                "missing": [item for item in ids if item not in thumbs],
            }
        )

    async def data_export(self):
        if self.plugin.checkin_store is None:
            return self._unavailable("签到数据尚未初始化")
        try:
            package = {
                "schema_version": 1,
                "plugin_name": self.plugin_name,
                "kind": "plugin_data_export",
                "exported_at": datetime.now().astimezone().isoformat(),
                "checkin": await self.plugin.checkin_store.export_snapshot(),
                "safety": await self.plugin.safety_service.settings(),
                "blacklist_metadata": await self.plugin.image_index.list_blacklist_illusts(),
                "legacy_safety_archives": await self.plugin.safety_service.legacy_archives(),
            }
            data = json.dumps(package, ensure_ascii=False, indent=2).encode("utf-8")
            return await send_file(BytesIO(data), mimetype="application/json", as_attachment=True, attachment_filename="get-px-data.json")
        except Exception as exc:
            return self.internal_error("导出插件数据", exc)

    async def checkin_import(self):
        if self.plugin.checkin_store is None:
            return self._unavailable("签到数据尚未初始化")
        filename = ""
        try:
            files = await request.files
            uploaded = []
            for key in files.keys():
                uploaded.extend(files.getlist(key))
            if not uploaded:
                return jsonify({"success": False, "error": "缺少备份文件"}), 400
            if len(uploaded) != 1:
                return jsonify(
                    {"success": False, "error": "一次只能上传一个备份文件"}
                ), 400
            upload = uploaded[0]
            filename = str(getattr(upload, "filename", "") or "").strip()
            if Path(filename).suffix.lower() != ".json":
                return jsonify({"success": False, "error": "只支持 JSON 备份文件"}), 400
            raw = await self.plugin._read_uploaded_file_bytes(upload)
            logger.info(
                f"{self.log_prefix} 签到 JSON 备份请求已接收: "
                f"size={format_log_size(len(raw))}"
            )
            parsed = json.loads(raw.decode("utf-8-sig"))
            if isinstance(parsed, dict) and parsed.get("kind") == "plugin_data_export":
                if parsed.get("schema_version") != 1 or parsed.get("plugin_name") != self.plugin_name:
                    raise ValueError("不支持的插件数据包")
                # 只恢复签到；安全配置与旧档案均不允许通过导入改变。
                raw = json.dumps(parsed.get("checkin"), ensure_ascii=False)
            snapshot = load_checkin_snapshot_json(raw)
            rollback_path, result = (
                await self.plugin.checkin_store.import_snapshot_with_rollback(
                    snapshot,
                    lambda rollback: self.plugin._write_checkin_snapshot_file(
                        rollback, prefix="checkin-import-backup"
                    ),
                )
            )
            logger.info(
                f"{self.log_prefix} 现有签到数据已备份: "
                f"file={Path(rollback_path).name!r}"
            )
            logger.warning(
                f"{self.log_prefix} 现有签到数据已由 JSON 备份覆盖: "
                f"users={result['users']} records={result['records']}"
            )
            return jsonify(
                {
                    "success": True,
                    "filename": filename,
                    **result,
                    "rollback_file": Path(rollback_path).name,
                }
            )
        except ValueError as exc:
            logger.info(
                f"{self.log_prefix} 签到数据导入被拒绝: "
                f"error_type={type(exc).__name__}"
            )
            return jsonify({"success": False, "error": str(exc)}), 400
        except Exception as exc:
            return self.internal_error("导入签到备份", exc)

    async def config(self):
        font_source = self.plugin._cfg_str("webui_font_source", "mirror")
        font_urls = {
            "mirror": "https://fonts.googleapis.cn/css2?family=Noto+Sans+SC:wght@400;500;600;700&display=swap",
            "official": "https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400;500;600;700&display=swap",
        }
        return jsonify(
            {
                "success": True,
                "font_source": font_source,
                "font_url": font_urls.get(font_source, ""),
            }
        )

    async def settings(self):
        try:
            return jsonify(settings_snapshot(self.plugin))
        except Exception as exc:
            return self.internal_error("读取插件设置", exc)

    async def settings_update(self):
        try:
            payload = await request.get_json(silent=True)
            if not isinstance(payload, dict) or not isinstance(payload.get("groups"), dict):
                raise ValueError("设置内容格式不正确")
            save_settings(self.plugin, payload["groups"])
            return jsonify({"success": True, "reload_required": True})
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        except Exception as exc:
            return self.internal_error("保存插件设置", exc)

    async def cache_storage(self):
        try:
            stats = await asyncio.to_thread(self._get_cache_storage_stats_sync)
            return jsonify({"success": True, **stats})
        except Exception as exc:
            return self.internal_error("读取缓存统计", exc)

    async def cache_storage_clear(self):
        try:
            result = await asyncio.to_thread(self._clear_cache_storage_sync)
            logger.info(
                f"{self.log_prefix} 管理页清理临时图片与卡片缓存: "
                f"freed_bytes={result['freed_bytes']} deleted_files={result['deleted_files']}"
            )
            return jsonify({"success": True, **result})
        except Exception as exc:
            return self.internal_error("清理缓存数据", exc)

    @staticmethod
    def _format_bytes(size: int) -> str:
        if size < 1024:
            return f"{size} B"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        if size < 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024):.1f} MB"
        return f"{size / (1024 * 1024 * 1024):.2f} GB"

    def _get_cache_storage_stats_sync(self) -> dict[str, Any]:
        card_bytes = 0
        card_count = 0
        temp_bytes = 0
        temp_count = 0

        # Check checkin card cache
        checkin_cache = getattr(self.plugin, "checkin_cache", None)
        if checkin_cache and hasattr(checkin_cache, "root") and checkin_cache.root.exists():
            for p in checkin_cache.root.rglob("*"):
                if p.is_file():
                    try:
                        card_bytes += p.stat().st_size
                        card_count += 1
                    except OSError as exc:
                        logger.debug(
                            f"{self.log_prefix} 缓存统计跳过文件: "
                            f"path={p}, error_type={type(exc).__name__}"
                        )

        # Check temporary downloaded images (get_px_*)
        data_dir = getattr(self.plugin, "data_dir", None)
        tmp_dir = Path(data_dir) / "temp" if data_dir is not None else None
        if tmp_dir is not None and tmp_dir.exists():
            for p in tmp_dir.glob("get_px_*"):
                if p.is_file():
                    try:
                        temp_bytes += p.stat().st_size
                        temp_count += 1
                    except OSError as exc:
                        logger.debug(
                            f"{self.log_prefix} 临时文件统计跳过: "
                            f"path={p}, error_type={type(exc).__name__}"
                        )

        total_bytes = card_bytes + temp_bytes
        total_count = card_count + temp_count
        return {
            "card_cache_bytes": card_bytes,
            "card_cache_count": card_count,
            "card_cache_human": self._format_bytes(card_bytes),
            "temp_download_bytes": temp_bytes,
            "temp_download_count": temp_count,
            "temp_download_human": self._format_bytes(temp_bytes),
            "total_bytes": total_bytes,
            "total_count": total_count,
            "total_human": self._format_bytes(total_bytes),
        }

    def _clear_cache_storage_sync(self) -> dict[str, Any]:
        freed_bytes = 0
        deleted_files = 0
        skipped_active = 0
        # ponytail: 10 分钟内修改过的临时文件视为活跃下载，跳过避免中断进行中的 /p 请求
        staleness_threshold = time.time() - 600

        # Clear checkin card cache (rendered JPEGs, never actively in use)
        checkin_cache = getattr(self.plugin, "checkin_cache", None)
        if checkin_cache and hasattr(checkin_cache, "root") and checkin_cache.root.exists():
            for p in list(checkin_cache.root.rglob("*")):
                if p.is_file():
                    try:
                        size = p.stat().st_size
                        p.unlink(missing_ok=True)
                        freed_bytes += size
                        deleted_files += 1
                    except OSError as exc:
                        logger.debug(
                            f"{self.log_prefix} 清理卡片缓存跳过文件: "
                            f"path={p}, error_type={type(exc).__name__}"
                        )
            # Remove empty subdirectories
            for d in sorted(checkin_cache.root.glob("*"), reverse=True):
                if d.is_dir():
                    try:
                        d.rmdir()
                    except OSError:
                        pass

        # Clear temporary downloaded images (skip files modified in last 10 min)
        data_dir = getattr(self.plugin, "data_dir", None)
        tmp_dir = Path(data_dir) / "temp" if data_dir is not None else None
        if tmp_dir is not None and tmp_dir.exists():
            for p in tmp_dir.glob("get_px_*"):
                if p.is_file():
                    try:
                        st = p.stat()
                        if st.st_mtime > staleness_threshold:
                            skipped_active += 1
                            continue
                        p.unlink(missing_ok=True)
                        freed_bytes += st.st_size
                        deleted_files += 1
                    except OSError as exc:
                        logger.debug(
                            f"{self.log_prefix} 清理临时下载跳过文件: "
                            f"path={p}, error_type={type(exc).__name__}"
                        )

        return {
            "freed_bytes": freed_bytes,
            "deleted_files": deleted_files,
            "skipped_active": skipped_active,
            "freed_human": self._format_bytes(freed_bytes),
        }

    def _latest_backup_at(self) -> str:
        data_dir = getattr(self.plugin, "data_dir", None)
        if not data_dir:
            return ""
        backup_dir = Path(data_dir) / "checkin_backups"
        try:
            latest = max(
                (item for item in backup_dir.glob("*.json") if item.is_file()),
                key=lambda item: item.stat().st_mtime,
            )
        except (OSError, ValueError):
            return ""
        return (
            datetime.fromtimestamp(latest.stat().st_mtime)
            .astimezone()
            .isoformat(timespec="seconds")
        )

    async def _require_known_group(self, value: object) -> str:
        group_id = str(value or "").strip()
        if not group_id:
            raise ValueError("缺少 group_id")
        groups = await self.plugin.checkin_store.list_checkin_groups()
        if not any(str(item.get("group_id") or "") == group_id for item in groups):
            raise ValueError("指定的群尚无签到记录")
        return group_id

    @staticmethod
    def _parse_int(value: object, minimum: int, maximum: int) -> int:
        try:
            parsed = int(str(value))
        except (TypeError, ValueError) as exc:
            raise ValueError("参数必须是整数") from exc
        if not minimum <= parsed <= maximum:
            raise ValueError(f"参数范围必须是 {minimum} 至 {maximum}")
        return parsed

    @staticmethod
    async def _request_json_object() -> dict[str, Any] | None:
        payload = await request.get_json(silent=True)
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _parse_profile_integer(value: object, label: str) -> int:
        try:
            if isinstance(value, bool):
                raise ValueError
            parsed = int(str(value))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}必须是整数") from exc
        if not 0 <= parsed <= 2_147_483_647:
            raise ValueError(f"{label}必须在 0 至 2147483647 之间")
        return parsed

    @staticmethod
    def _parse_profile_affection(value: object) -> float:
        try:
            if isinstance(value, bool):
                raise ValueError
            parsed = float(str(value))
        except (TypeError, ValueError) as exc:
            raise ValueError("好感度必须是数字") from exc
        if not math.isfinite(parsed):
            raise ValueError("好感度必须是有限数字")
        if not -10 <= parsed <= 1_000_000:
            raise ValueError("好感度必须在 -10 至 1000000 之间")
        return round(parsed, 2)

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

    @staticmethod
    def _unavailable(message: str = "插件数据尚未初始化"):
        return jsonify({"success": False, "error": message}), 503
