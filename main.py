import asyncio
import base64
import json
import math
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import File, Plain, Record
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path


PLUGIN_NAME = "astrbot_plugin_wusound_tts"
DEFAULT_TTS_ENDPOINT = "https://v1.wusound.cn/api/tts/simple-generate"


@dataclass
class GeneratedAudio:
    name: str
    path: Path | None = None
    url: str | None = None


@register(
    PLUGIN_NAME,
    "Codex",
    "将 AstrBot 的短 AI 回复翻译为日语，并调用悟声 AI 实时生成音频文件。",
    "0.1.0",
)
class WusoundTtsPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = self._normalize_config(config)
        self.session: aiohttp.ClientSession | None = None
        self.semaphore = asyncio.Semaphore(self._get_int("max_concurrent_jobs", 2))

    async def initialize(self) -> None:
        self.config = self._normalize_config(self.config)
        timeout = aiohttp.ClientTimeout(total=self._get_int("timeout_seconds", 60))
        self.session = aiohttp.ClientSession(timeout=timeout)

    async def terminate(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent) -> None:
        if event.get_extra("_wusound_tts_sending", False):
            return
        if not self._get_bool("enabled", True):
            return

        source_text = self._extract_sent_plain_text(event)
        if not self._should_generate_audio(source_text):
            return

        async with self.semaphore:
            try:
                spoken_text = await self._translate_to_japanese(event, source_text)
                audio = await self._generate_audio(spoken_text)
                event.set_extra("_wusound_tts_sending", True)
                await self._send_audio(event, audio)
            except Exception as exc:
                logger.warning(f"悟声 TTS 音频生成失败: {exc}")

    def _extract_sent_plain_text(self, event: AstrMessageEvent) -> str:
        result = event.get_result()
        if result is None:
            return ""

        text_getter = getattr(result, "get_plain_text", None)
        if callable(text_getter):
            return self._clean_text(text_getter())

        chain = getattr(result, "chain", None)
        if not chain:
            return ""

        texts: list[str] = []
        for component in chain:
            if isinstance(component, Plain):
                texts.append(component.text)
        return self._clean_text("".join(texts))

    def _should_generate_audio(self, text: str) -> bool:
        if not text:
            return False
        token_count = self._estimate_token_count(text)
        if token_count > self._get_int("max_output_tokens", 80):
            return False
        if token_count < self._get_int("min_output_tokens", 1):
            return False
        return not self._looks_like_non_spoken_text(text)

    def _estimate_token_count(self, text: str) -> int:
        cjk_or_kana_count = len(re.findall(r"[\u3040-\u30ff\u3400-\u9fff]", text))
        latin_words = re.findall(r"[A-Za-z0-9_]+", text)
        latin_token_count = sum(max(1, math.ceil(len(word) / 4)) for word in latin_words)
        symbol_count = len(re.findall(r"[^\sA-Za-z0-9_\u3040-\u30ff\u3400-\u9fff]", text))
        return cjk_or_kana_count + latin_token_count + math.ceil(symbol_count / 2)

    async def _translate_to_japanese(
        self,
        event: AstrMessageEvent,
        text: str,
    ) -> str:
        if not self._get_bool("translate_to_japanese", True):
            return text

        provider = self.context.get_using_provider(event.unified_msg_origin)
        if provider is None:
            logger.warning("未找到当前会话 LLM，悟声 TTS 将直接使用原文。")
            return text

        prompt = self._get_str("translation_prompt", "")
        if not prompt:
            prompt = (
                "请将下面这段聊天回复翻译成自然、适合口语朗读的日语。"
                "只输出日语译文，不要解释，不要加引号，不要保留中文。\n\n{text}"
            )

        response = await provider.text_chat(
            prompt=prompt.replace("{text}", text),
            session_id="",
        )
        translated_text = getattr(response, "completion_text", None) or str(response)
        return self._clean_text(translated_text) or text

    async def _generate_audio(self, spoken_text: str) -> GeneratedAudio:
        if self.session is None or self.session.closed:
            await self.initialize()
        if self.session is None:
            raise RuntimeError("aiohttp session 初始化失败")

        api_key = self._get_secret("api_key", "WUSOUND_API_KEY")
        if not api_key:
            raise ValueError("未配置悟声 API Key")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = self._build_tts_payload(spoken_text)

        async with self.session.post(
            self._get_str("tts_endpoint", DEFAULT_TTS_ENDPOINT),
            headers=headers,
            json=payload,
        ) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            if content_type.startswith("audio/"):
                audio_bytes = await response.read()
                self._validate_audio_size(audio_bytes)
                return self._save_audio_file(audio_bytes)
            else:
                data = await response.json(content_type=None)
                return await self._read_audio_from_json(data)

    async def _send_audio(self, event: AstrMessageEvent, audio: GeneratedAudio) -> None:
        component = self._build_audio_component(audio)
        message_chain = MessageChain([component])

        if self._get_bool("use_context_send_message", True):
            await self.context.send_message(event.unified_msg_origin, message_chain)
            return

        await event.send(message_chain)

    def _build_audio_component(self, audio: GeneratedAudio) -> File | Record:
        send_as = self._get_str("send_as", "file").lower()
        if send_as == "record":
            if audio.url:
                return Record.fromURL(audio.url)
            if audio.path:
                return Record.fromFileSystem(str(audio.path))
            raise ValueError("没有可发送的语音来源")

        if audio.url and self._get_bool("prefer_remote_url", True):
            return File(name=audio.name, url=audio.url)
        if audio.path:
            return File(name=audio.name, file=str(audio.path))
        if audio.url:
            return File(name=audio.name, url=audio.url)
        raise ValueError("没有可发送的音频文件来源")

    def _build_tts_payload(self, text: str) -> dict[str, Any]:
        template = self._get_str("payload_template", "")
        if template:
            return self._render_payload_template(template, text)

        payload = {
            "text": text,
            "voiceId": self._get_str("voice_id", ""),
            "promptId": self._get_str("prompt_id", ""),
            "format": self._get_str("audio_format", "mp3"),
        }
        return {key: value for key, value in payload.items() if value not in ("", None)}

    def _render_payload_template(self, template: str, text: str) -> dict[str, Any]:
        # 先替换带引号的占位符，避免日语文本里的引号破坏 JSON 结构。
        rendered = template.replace(
            json.dumps("{{text}}"),
            json.dumps(text, ensure_ascii=False),
        )
        rendered = rendered.replace("{{text}}", text)
        payload = json.loads(rendered)
        if not isinstance(payload, dict):
            raise ValueError("payload_template 必须渲染为 JSON 对象")
        return payload

    async def _read_audio_from_json(self, data: dict[str, Any]) -> GeneratedAudio:
        audio_url = self._find_first_value(
            data,
            (
                "audioUrl",
                "audio_url",
                "fileUrl",
                "file_url",
                "downloadUrl",
                "download_url",
                "audio",
                "url",
            ),
        )
        if audio_url:
            audio_url_text = str(audio_url)
            if audio_url_text.startswith(("http://", "https://")):
                name = self._build_audio_name_from_url(audio_url_text)
                if self._get_bool("prefer_remote_url", True):
                    return GeneratedAudio(name=name, url=audio_url_text)
                audio_bytes = await self._download_audio(audio_url_text)
                return self._save_audio_file(audio_bytes, fallback_name=name)
            if audio_url_text.startswith("data:audio/"):
                audio_bytes = self._decode_base64_audio(audio_url_text)
                self._validate_audio_size(audio_bytes)
                return self._save_audio_file(audio_bytes)

        audio_base64 = self._find_first_value(
            data,
            ("audioBase64", "audio_base64", "audioContent", "audio_content"),
        )
        if audio_base64:
            audio_bytes = self._decode_base64_audio(str(audio_base64))
            self._validate_audio_size(audio_bytes)
            return self._save_audio_file(audio_bytes)

        if not audio_url:
            raise ValueError(f"悟声接口未返回音频 URL 或 base64: {data}")

        raise ValueError(f"悟声接口返回的音频地址不可识别: {audio_url}")

    async def _download_audio(self, audio_url: str) -> bytes:
        if self.session is None:
            raise RuntimeError("aiohttp session 未初始化")

        async with self.session.get(audio_url) as response:
            response.raise_for_status()
            audio_bytes = await response.read()
            self._validate_audio_size(audio_bytes)
            return audio_bytes

    def _decode_base64_audio(self, value: str) -> bytes:
        encoded_audio = value.split(",", 1)[-1].strip()
        encoded_audio = re.sub(r"\s+", "", encoded_audio)
        # 有些接口会省略 base64 的 = 补齐，这里按长度补齐后再解码。
        encoded_audio += "=" * (-len(encoded_audio) % 4)
        return base64.b64decode(encoded_audio)

    def _save_audio_file(
        self,
        audio_bytes: bytes,
        fallback_name: str | None = None,
    ) -> GeneratedAudio:
        output_dir = Path(get_astrbot_temp_path()) / "wusound_tts"
        output_dir.mkdir(parents=True, exist_ok=True)
        if fallback_name:
            suffix = Path(fallback_name).suffix.lstrip(".") or self._get_audio_suffix()
        else:
            suffix = self._get_audio_suffix()
        name = fallback_name or f"wusound_{uuid.uuid4().hex}.{suffix}"
        audio_path = output_dir / name
        audio_path.write_bytes(audio_bytes)
        return GeneratedAudio(name=audio_path.name, path=audio_path)

    def _build_audio_name_from_url(self, audio_url: str) -> str:
        name = Path(audio_url.split("?", 1)[0]).name
        if name and "." in name:
            return name
        return f"wusound_{uuid.uuid4().hex}.{self._get_audio_suffix()}"

    def _get_audio_suffix(self) -> str:
        return self._get_str("audio_format", "mp3").lstrip(".") or "mp3"

    def _validate_audio_size(self, audio_bytes: bytes) -> None:
        max_audio_bytes = self._get_int("max_audio_bytes", 10 * 1024 * 1024)
        if len(audio_bytes) > max_audio_bytes:
            raise ValueError("悟声返回的音频超过 max_audio_bytes 限制")

    def _find_first_value(self, value: Any, keys: tuple[str, ...]) -> Any:
        if isinstance(value, dict):
            for key in keys:
                if value.get(key):
                    return value[key]
            for item in value.values():
                found = self._find_first_value(item, keys)
                if found:
                    return found
        if isinstance(value, list):
            for item in value:
                found = self._find_first_value(item, keys)
                if found:
                    return found
        return None

    def _clean_text(self, text: str) -> str:
        compact_text = re.sub(r"\s+", " ", str(text or "")).strip()
        return compact_text.strip("\"'`“”‘’")

    def _looks_like_non_spoken_text(self, text: str) -> bool:
        lowered = text.lower()
        return (
            lowered.startswith("/")
            or lowered.startswith("http://")
            or lowered.startswith("https://")
            or "```" in lowered
            or "[CQ:" in text
        )

    def _get_secret(self, key: str, env_name: str) -> str:
        return self._get_str(key, "") or os.getenv(env_name, "")

    def _normalize_config(self, config: Any) -> Any:
        if hasattr(config, "get"):
            return config

        get_registered_star = getattr(self.context, "get_registered_star", None)
        if not callable(get_registered_star):
            return {}

        metadata = get_registered_star(PLUGIN_NAME)
        metadata_config = getattr(metadata, "config", None) if metadata else None
        if hasattr(metadata_config, "get"):
            return metadata_config
        return {}

    def _get_str(self, key: str, default: str) -> str:
        value = self.config.get(key, default)
        return str(value).strip() if value is not None else default

    def _get_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _get_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        return str(value).lower() in {"1", "true", "yes", "on", "是", "开启"}
