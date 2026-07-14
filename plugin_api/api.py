from __future__ import annotations

import asyncio
import base64
from datetime import datetime
import math
from pathlib import Path
from typing import Any

from astrbot.api.all import logger
from quart import jsonify, request, send_file

try:
    from ..checkin import load_checkin_snapshot_json
    from ..pixiv.downloader import cleanup, pick_image_url_exact
    from ..pixiv.safety import (
        BUILTIN_SAFETY_TERMS,
        illustration_texts,
        match_safety_term,
        normalize_safety_text,
        normalized_builtin_terms,
    )
except ImportError:  # Direct imports used by the test suite.
    from checkin import load_checkin_snapshot_json
    from pixiv.downloader import cleanup, pick_image_url_exact
    from pixiv.safety import (
        BUILTIN_SAFETY_TERMS,
        illustration_texts,
        match_safety_term,
        normalize_safety_text,
        normalized_builtin_terms,
    )


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
                "image-blacklist/thumb-data",
                self.image_blacklist_thumb_data,
                ["GET"],
                "Get blacklist thumbnail",
            ),
            (
                "image-blacklist/thumb-data-batch",
                self.image_blacklist_thumb_data_batch,
                ["POST"],
                "Get blacklist thumbnails",
            ),
            ("checkin-export", self.checkin_export, ["GET"], "Export check-in backup"),
            ("checkin-import", self.checkin_import, ["POST"], "Import check-in backup"),
            ("config", self.config, ["GET"], "Get management center configuration"),
        )
        for path, handler, methods, description in routes:
            self.context.register_web_api(
                f"/{self.plugin_name}/{path}", handler, methods, description
            )

    def internal_error(self, action: str, exc: Exception):
        logger.error(
            f"{self.log_prefix} Web API {action}失败: {type(exc).__name__}: {exc}"
        )
        return jsonify({"success": False, "error": self.internal_error_message}), 500

    async def overview(self):
        if self.plugin.checkin_store is None or self.plugin.image_index is None:
            return self._unavailable()
        try:
            checkin, blacklist, custom_terms = await asyncio.gather(
                self.plugin.checkin_store.get_checkin_overview(),
                self.plugin.image_index.list_blacklist_illusts(),
                self.plugin.image_index.list_safety_terms(),
            )
            return jsonify(
                {
                    "success": True,
                    **checkin,
                    "blacklist_count": len(blacklist),
                    "builtin_term_count": len(BUILTIN_SAFETY_TERMS),
                    "custom_term_count": len(custom_terms),
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
            )
            before = result["before"]
            member = result["member"]
            logger.info(
                f"{self.log_prefix} 管理页调整签到成员数值: user_id={user_id} "
                f"coins={before['coins']}->{member['coins']} "
                f"affection={before['affection']}->{member['affection']} "
                f"total_days={before['total_days']}->{member['total_days']} "
                f"streak_days={before['streak_days']}->{member['streak_days']}"
            )
            return jsonify({"success": True, "member": member})
        except LookupError as exc:
            return jsonify({"success": False, "error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        except Exception as exc:
            return self.internal_error("调整签到成员数值", exc)

    async def content_safety(self):
        if self.plugin.image_index is None:
            return self._unavailable("内容安全数据尚未初始化")
        try:
            custom_terms = await self.plugin.image_index.list_safety_terms()
            return jsonify(
                {
                    "success": True,
                    "rating_policy": "general_only",
                    "rating_label": "仅允许普通作品",
                    "builtin_terms": list(BUILTIN_SAFETY_TERMS),
                    "custom_terms": custom_terms,
                }
            )
        except Exception as exc:
            return self.internal_error("读取内容安全策略", exc)

    async def content_safety_term_add(self):
        if self.plugin.image_index is None:
            return self._unavailable("内容安全数据尚未初始化")
        payload = await self._request_json_object()
        if payload is None:
            return jsonify({"success": False, "error": "请求内容必须是对象"}), 400
        term = str(payload.get("term") or "").strip()
        if normalize_safety_text(term) in {
            normalize_safety_text(item) for item in BUILTIN_SAFETY_TERMS
        }:
            return jsonify({"success": False, "error": "该词已经属于内置安全词"}), 400
        try:
            await self.plugin.image_index.add_safety_term(term, added_by="web")
            return jsonify({"success": True, "term": term})
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        except Exception as exc:
            return self.internal_error("添加自定义安全词", exc)

    async def content_safety_term_remove(self):
        if self.plugin.image_index is None:
            return self._unavailable("内容安全数据尚未初始化")
        payload = await self._request_json_object()
        if payload is None:
            return jsonify({"success": False, "error": "请求内容必须是对象"}), 400
        term = str(payload.get("term") or "").strip()
        if normalize_safety_text(term) in {
            normalize_safety_text(item) for item in BUILTIN_SAFETY_TERMS
        }:
            return jsonify({"success": False, "error": "内置安全词不能删除"}), 400
        try:
            removed = await self.plugin.image_index.remove_safety_term(term)
            if not removed:
                return jsonify({"success": False, "error": "自定义安全词不存在"}), 404
            return jsonify({"success": True, "term": term})
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        except Exception as exc:
            return self.internal_error("删除自定义安全词", exc)

    async def image_blacklist(self):
        if self.plugin.image_index is None:
            return self._unavailable("作品黑名单尚未初始化")
        try:
            records = await self.plugin.image_index.list_blacklist_illusts()
            return jsonify({"success": True, "records": records})
        except Exception as exc:
            return self.internal_error("读取作品黑名单", exc)

    async def image_blacklist_add(self):
        if self.plugin.image_index is None:
            return self._unavailable("作品黑名单尚未初始化")
        payload = await self._request_json_object()
        if payload is None:
            return jsonify({"success": False, "error": "请求内容必须是对象"}), 400
        illust_id = str(payload.get("illust_id") or "").strip()
        if not illust_id.isdigit() or int(illust_id) <= 0:
            return jsonify(
                {"success": False, "error": "请输入有效的 Pixiv 作品 ID"}
            ), 400
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
                    f"illust_id={illust_id} error={type(exc).__name__}"
                )
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
            return self.internal_error("添加作品黑名单", exc)

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
            temp_path = await downloader.download(
                url,
                proxy=self.plugin._cfg_str("pixiv_proxy_url", ""),
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
                f"illust_id={illust_id} error={type(exc).__name__}"
            )
        finally:
            cleanup(temp_path)

    async def _thumbnail_is_safe(self, illust: dict[str, Any]) -> bool:
        if int(illust.get("x_restrict", 0) or 0) != 0:
            return False
        try:
            terms = set(normalized_builtin_terms())
            terms.update(await self.plugin.image_index.get_custom_safety_terms())
        except Exception as exc:
            logger.warning(
                f"{self.log_prefix} 黑名单缩略图安全检查失败: "
                f"error={type(exc).__name__}"
            )
            return False
        return not any(
            match_safety_term(value, terms) for value in illustration_texts(illust)
        )

    async def image_blacklist_remove(self):
        if self.plugin.image_index is None:
            return self._unavailable("作品黑名单尚未初始化")
        payload = await self._request_json_object()
        if payload is None:
            return jsonify({"success": False, "error": "请求内容必须是对象"}), 400
        illust_id = str(payload.get("illust_id") or "").strip()
        if not illust_id.isdigit() or int(illust_id) <= 0:
            return jsonify(
                {"success": False, "error": "请输入有效的 Pixiv 作品 ID"}
            ), 400
        try:
            removed = await self.plugin.image_index.remove_blacklist_illust(illust_id)
            if removed is None:
                return jsonify({"success": False, "error": "黑名单记录不存在"}), 404
            return jsonify({"success": True, "record": removed})
        except Exception as exc:
            return self.internal_error("解除作品黑名单", exc)

    async def image_blacklist_thumb_data(self):
        if self.plugin.image_index is None:
            return self._unavailable("作品黑名单尚未初始化")
        illust_id = request.args.get("id", "").strip()
        path = await self.plugin.image_index.get_blacklist_thumbnail_path(illust_id)
        if path is None:
            return jsonify({"success": False, "error": "缩略图不存在"}), 404
        try:
            raw = await asyncio.to_thread(path.read_bytes)
            encoded = base64.b64encode(raw).decode("ascii")
        except OSError as exc:
            return self.internal_error("读取黑名单缩略图", exc)
        return jsonify(
            {
                "success": True,
                "illust_id": illust_id,
                "data_url": f"data:image/jpeg;base64,{encoded}",
            }
        )

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

    async def checkin_export(self):
        if self.plugin.checkin_store is None:
            return self._unavailable("签到数据尚未初始化")
        try:
            path = await self.plugin._write_checkin_snapshot_backup(
                prefix="checkin-export"
            )
            return await send_file(
                str(path),
                mimetype="application/json",
                as_attachment=True,
                attachment_filename=path.name,
            )
        except Exception as exc:
            return self.internal_error("导出签到备份", exc)

    async def checkin_import(self):
        if self.plugin.checkin_store is None:
            return self._unavailable("签到数据尚未初始化")
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
            if not filename.lower().endswith(".json"):
                return jsonify({"success": False, "error": "只支持 JSON 备份文件"}), 400
            raw = await self.plugin._read_uploaded_file_bytes(upload)
            snapshot = load_checkin_snapshot_json(raw)
            rollback_path = await self.plugin._write_checkin_snapshot_backup(
                prefix="checkin-import-backup"
            )
            result = await self.plugin.checkin_store.import_snapshot(snapshot)
            return jsonify(
                {
                    "success": True,
                    "filename": filename,
                    **result,
                    "rollback_file": Path(rollback_path).name,
                }
            )
        except ValueError as exc:
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
