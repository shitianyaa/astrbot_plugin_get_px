from __future__ import annotations

from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context

LOG_PREFIX = "[GetPx]"


class AiCommenter:
    def __init__(self, context: Context):
        self.context = context

    async def comment(
        self,
        event: AstrMessageEvent,
        image_path: str,
        vision_pid: str,
        comment_pid: str,
        vision_prompt: str,
        comment_prompt: str,
    ) -> str:
        image_url = Path(image_path).as_uri()
        umo = event.unified_msg_origin

        v_pid = await self._resolve_provider(vision_pid, umo, prefer_vision=True)
        if not v_pid:
            raise RuntimeError("未配置视觉模型，无法进行 AI 识图")
        logger.info(f"{LOG_PREFIX} [AI] 识图模型: {v_pid}")

        vision_resp = await self.context.llm_generate(
            chat_provider_id=v_pid,
            prompt=vision_prompt,
            image_urls=[image_url],
        )
        description = (vision_resp.completion_text or "").strip()
        if not description:
            raise RuntimeError("视觉模型返回空结果")
        logger.info(f"{LOG_PREFIX} [AI] 识图结果: {description[:80]}...")

        c_pid = await self._resolve_provider(comment_pid, umo)
        if not c_pid:
            raise RuntimeError("未配置评论模型")
        logger.info(f"{LOG_PREFIX} [AI] 评论模型: {c_pid}")

        final_prompt = comment_prompt.replace("{description}", description)
        comment_resp = await self.context.llm_generate(
            chat_provider_id=c_pid,
            prompt=final_prompt,
        )
        return (comment_resp.completion_text or "").strip()

    async def _resolve_provider(self, config_pid: str, umo: str, prefer_vision: bool = False) -> str:
        if config_pid:
            return config_pid
        if prefer_vision:
            try:
                cfg = self.context.get_config()
                vlm_id = str((cfg.get("provider_settings") or {}).get("default_image_caption_provider_id", "") or "").strip()
                if vlm_id:
                    return vlm_id
            except Exception:
                pass
        try:
            pid = await self.context.get_current_chat_provider_id(umo=umo)
            if pid:
                return str(pid).strip()
        except Exception:
            pass
        return ""
