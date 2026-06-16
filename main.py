import asyncio
import base64
import json
import math
import os
import re
import struct
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import File, Plain
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
        if event.get_extra("_wusound_tts_skip_auto", False):
            return
        if not self._get_bool("enabled", True):
            return
        if not self._is_event_allowed(event):
            return

        source_text = self._extract_sent_plain_text(event)
        if not self._should_generate_audio(source_text):
            return

        async with self.semaphore:
            try:
                if self._get_bool("mock_mode", False):
                    audio = self._generate_mock_audio()
                else:
                    spoken_text = await self._translate_to_japanese(event, source_text)
                    audio = await self._generate_audio(spoken_text)
                event.set_extra("_wusound_tts_sending", True)
                await self._send_audio(event, audio)
            except Exception as exc:
                logger.warning(f"悟声 TTS 音频生成失败: {exc}")

    @filter.command("wusound_test")
    async def wusound_test(self, event: AstrMessageEvent):
        """发送一条 mock 音频，发送方式使用当前 send_as 配置。"""
        async for result in self._run_mock_send_test(event):
            yield result

    @filter.command("wusound_file_test")
    async def wusound_file_test(self, event: AstrMessageEvent):
        """发送一条 mock 音频文件，用于确认文件发送链路。"""
        async for result in self._run_mock_send_test(event, send_as="file"):
            yield result

    @filter.command("wusound_record_test")
    async def wusound_record_test(self, event: AstrMessageEvent):
        """发送一条 mock QQ 语音，用于确认 record 语音链路。"""
        async for result in self._run_mock_send_test(event, send_as="record"):
            yield result

    @filter.command("wusound_where")
    async def wusound_where(self, event: AstrMessageEvent):
        """显示当前会话标识，方便配置白名单。"""
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        group_id = self._extract_group_id(event, origin)
        user_id = self._extract_user_id(event)
        is_allowed = self._is_event_allowed(event)
        group_mode = self._get_str("group_filter_mode", "none")
        user_mode = self._get_str("user_filter_mode", "none")
        yield event.plain_result(
            "当前悟声 TTS 会话信息：\n"
            f"group_id: {group_id or '未识别'}\n"
            f"user_id: {user_id or '未识别'}\n"
            f"origin: {origin or '未识别'}\n"
            f"group_filter: {group_mode}\n"
            f"user_filter: {user_mode}\n"
            f"allowed: {is_allowed}"
        )

    @filter.command("wusound_preview")
    async def wusound_preview(self, event: AstrMessageEvent):
        """预览翻译结果，不调用悟声 API，用于排查文本质量。"""
        if not self._get_bool("enabled", True):
            yield event.plain_result("插件未启用。")
            return
        if not self._is_event_allowed(event):
            yield event.plain_result("当前会话不在白名单中。")
            return

        source_text = self._extract_sent_plain_text(event)
        if not source_text:
            yield event.plain_result("未读取到 AI 回复文本。")
            return

        token_count = self._estimate_token_count(source_text)
        would_trigger = "会" if self._should_generate_audio(source_text) else "不会"

        if self._get_bool("translate_to_japanese", True):
            yield event.plain_result("正在翻译，请稍候...")
            translated = await self._translate_to_japanese(event, source_text)
        else:
            translated = "(翻译已关闭)"

        yield event.plain_result(
            "----- 悟声 TTS 文本预览 -----\n"
            f"【原始回复】(token: {token_count}, {would_trigger}触发TTS)\n"
            f"{source_text}\n\n"
            f"【翻译结果】\n"
            f"{translated}"
        )

    async def _run_mock_send_test(
        self,
        event: AstrMessageEvent,
        send_as: str | None = None,
    ):
        event.set_extra("_wusound_tts_skip_auto", True)
        if not self._is_event_allowed(event):
            yield event.plain_result("当前会话不在悟声 TTS 白名单中，已跳过。")
            return
        try:
            audio = self._generate_mock_audio()
            await self._send_audio(event, audio, send_as=send_as)
            actual_send_as = send_as or self._get_str("send_as", "file")
            yield event.plain_result(f"已发送 mock {actual_send_as} 音频：{audio.name}")
        except Exception as exc:
            logger.warning(f"悟声 TTS mock 发送测试失败: {exc}")
            yield event.plain_result(f"mock 音频发送失败：{exc}")

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

    def _is_event_allowed(self, event: AstrMessageEvent) -> bool:
        # -- 群聊过滤 --
        group_mode = self._get_str("group_filter_mode", "none")
        if group_mode != "none":
            origin = str(getattr(event, "unified_msg_origin", "") or "")
            group_id = self._extract_group_id(event, origin)
            group_list = self._get_list("group_filter_list")
            is_in = any(
                self._is_group_match(origin, group_id, item) for item in group_list
            )
            if group_mode == "whitelist" and not is_in:
                logger.debug(
                    f"悟声 TTS 不在群白名单: origin={origin}, group_id={group_id}"
                )
                return False
            if group_mode == "blacklist" and is_in:
                logger.debug(
                    f"悟声 TTS 在群黑名单: origin={origin}, group_id={group_id}"
                )
                return False

        # -- 用户过滤 --
        user_mode = self._get_str("user_filter_mode", "none")
        if user_mode != "none":
            user_id = self._extract_user_id(event)
            user_list = self._get_list("user_filter_list")
            is_in = bool(user_id and user_id in user_list)
            if user_mode == "whitelist" and not is_in:
                logger.debug(f"悟声 TTS 不在用户白名单: user_id={user_id}")
                return False
            if user_mode == "blacklist" and is_in:
                logger.debug(f"悟声 TTS 在用户黑名单: user_id={user_id}")
                return False

        return True

    def _extract_group_id(self, event: AstrMessageEvent, origin: str) -> str:
        for method_name in ("get_group_id", "get_groupid"):
            method = getattr(event, method_name, None)
            if callable(method):
                group_id = method()
                if group_id:
                    return str(group_id)

        message_obj = getattr(event, "message_obj", None)
        if message_obj is not None:
            for attr_name in ("group_id", "groupid"):
                group_id = getattr(message_obj, attr_name, None)
                if group_id:
                    return str(group_id)

        match = re.search(r"(?:GroupMessage|group|group_id)[:_](\d+)", origin)
        if match:
            return match.group(1)

        numeric_parts = re.findall(r"\d+", origin)
        return numeric_parts[-1] if numeric_parts else ""

    def _extract_user_id(self, event: AstrMessageEvent) -> str:
        """从事件中提取用户 ID。"""
        # 优先从 message_obj 中获取 sender 的 user_id
        message_obj = getattr(event, "message_obj", None)
        if message_obj is not None:
            sender = getattr(message_obj, "sender", None)
            if sender is not None:
                user_id = getattr(sender, "user_id", None)
                if user_id:
                    return str(user_id)
            raw_message = getattr(message_obj, "raw_message", None)
            if isinstance(raw_message, dict):
                user_id = raw_message.get("user_id") or raw_message.get("sender_user_id")
                if user_id:
                    return str(user_id)

        # 尝试直接从 event 获取
        for attr_name in ("get_user_id", "user_id", "sender_user_id"):
            method = getattr(event, attr_name, None)
            if callable(method):
                user_id = method()
                if user_id:
                    return str(user_id)
            if method is not None and not callable(method):
                return str(method)

        return ""
    
    def _is_group_match(self, origin: str, group_id: str, item: str) -> bool:
        """智能匹配群聊标识，支持完整 UMO 和纯群号两种格式。"""
        item = str(item).strip()
        if not item:
            return False
        if item == origin or item == group_id:
            return True
        # UMO 格式匹配：onebot:GroupMessage:123456 对 123456
        if ":" in item:
            parts = item.rsplit(":", 1)
            if parts[-1] == group_id:
                return True
        # 纯数字匹配：123456 对 onebot:GroupMessage:123456
        if ":" in origin and origin.rsplit(":", 1)[-1] == item:
            return True
        return False

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
                "You are a strict translator. Translate the following text into "
                "natural spoken Japanese.\n\n"
                "Rules:\n"
                "1. Output ONLY the Japanese translation, nothing else\n"
                "2. Do NOT add prefixes like 'Translation:' or 'Japanese:'\n"
                "3. Do NOT explain your translation choices\n"
                "4. Do NOT use quotes, brackets, or markdown formatting\n"
                "5. Do NOT output multiple versions\n\n"
                "Text: {text}"
            )

        response = await provider.text_chat(
            prompt=prompt.replace("{text}", text),
            session_id="",
        )
        translated_text = getattr(response, "completion_text", None) or str(response)
        translated_text = self._extract_japanese(translated_text) or text
        return self._clean_text(translated_text) or text

    async def _generate_audio(self, spoken_text: str) -> GeneratedAudio:
        if self.session is None or self.session.closed:
            await self.initialize()
        if self.session is None:
            raise RuntimeError("aiohttp session 初始化失败")

        logger.info(f"悟声 TTS 请求文本({len(spoken_text)}字): {spoken_text}")

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

    def _generate_mock_audio(self) -> GeneratedAudio:
        mock_audio_url = self._get_str("mock_audio_url", "")
        if mock_audio_url.startswith(("http://", "https://")):
            return GeneratedAudio(
                name=self._build_audio_name_from_url(mock_audio_url),
                url=mock_audio_url,
            )
        if mock_audio_url:
            logger.warning(f"mock_audio_url 不是有效 URL，已改用本地 WAV: {mock_audio_url}")

        output_dir = Path(get_astrbot_temp_path()) / "wusound_tts_mock"
        output_dir.mkdir(parents=True, exist_ok=True)
        audio_path = output_dir / f"mock_tts_{uuid.uuid4().hex}.wav"
        self._write_mock_wave(audio_path)
        return GeneratedAudio(name=audio_path.name, path=audio_path)

    def _write_mock_wave(self, audio_path: Path) -> None:
        sample_rate = 16000
        duration_seconds = max(1, self._get_int("mock_duration_seconds", 1))
        frequency = max(120, self._get_int("mock_frequency_hz", 440))
        amplitude = 12000
        frame_count = sample_rate * duration_seconds

        with wave.open(str(audio_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            for sample_index in range(frame_count):
                value = int(
                    amplitude
                    * math.sin(2 * math.pi * frequency * sample_index / sample_rate)
                )
                wav_file.writeframesraw(struct.pack("<h", value))

    async def _send_audio(
        self,
        event: AstrMessageEvent,
        audio: GeneratedAudio,
        send_as: str | None = None,
    ) -> None:
        send_as = (send_as or self._get_str("send_as", "file")).lower()
        # record 模式需要本地文件，不能直接用远程 URL
        if send_as == "record" and audio.url and not audio.path:
            audio_bytes = await self._download_audio(audio.url)
            audio = self._save_audio_file(audio_bytes, fallback_name=audio.name)
        component = self._build_audio_component(audio, send_as=send_as)
        message_chain = MessageChain([component])

        try:
            if self._get_bool("use_context_send_message", True):
                await self.context.send_message(event.unified_msg_origin, message_chain)
            else:
                await event.send(message_chain)
        finally:
            # 发送后清理本地音频文件，避免磁盘堆积
            if audio.path and audio.path.exists():
                try:
                    audio.path.unlink()
                except OSError:
                    logger.debug(f"清理音频文件失败: {audio.path}")

    def _build_audio_component(
        self,
        audio: GeneratedAudio,
        send_as: str | None = None,
    ) -> Any:
        send_as = (send_as or self._get_str("send_as", "file")).lower()
        if send_as == "record":
            from astrbot.api.message_components import Record

            if audio.path:
                return Record.fromFileSystem(str(audio.path))
            if audio.url:
                return Record.fromURL(audio.url)
            raise ValueError("没有可发送的语音来源")

        if audio.url and self._is_http_url(audio.url) and self._get_bool(
            "prefer_remote_url",
            True,
        ):
            return File(name=audio.name, url=audio.url)
        if audio.path:
            return File(name=audio.name, file=str(audio.path))
        if audio.url and self._is_http_url(audio.url):
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

    def _extract_japanese(self, text: str) -> str:
        """从 LLM 翻译结果中提取纯日语，过滤掉解释说明等中文杂质。"""
        if not text:
            return ""
        # 1) 优先提取「」中的日语文本（LLM 常把译文放在括号内）
        bracketed = re.findall(r"「([^」]*)」", text)
        if bracketed:
            return bracketed[0]
        # 2) 去除 Markdown 格式
        text = re.sub(r"\*{1,3}|_{1,3}|`+", "", text)
        # 3) 去除常见中文前缀
        text = re.sub(r"^(日语)?翻译[：:]\s*", "", text)
        text = re.sub(r"^(日文|译文|翻译结果)[：:]\s*", "", text)
        # 4) 按行取第一条含假名的行
        for line in text.split("\n"):
            line = line.strip()
            if line and re.search(r"[\u3040-\u30ff]", line):
                return line
        # 5) 兜底：取含假名的连续片段
        match = re.search(r"[\u3040-\u30ff\u4e00-\u9fff～〜！？。、…\w]+", text)
        if match:
            return match.group(0)
        return text

    def _looks_like_non_spoken_text(self, text: str) -> bool:
        lowered = text.lower()
        return (
            lowered.startswith("/")
            or lowered.startswith("http://")
            or lowered.startswith("https://")
            or "```" in lowered
            or "[CQ:" in text
        )

    def _is_http_url(self, value: str) -> bool:
        return str(value).startswith(("http://", "https://"))

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

    def _get_list(self, key: str) -> list[str]:
        value = self.config.get(key, [])
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return [
            item.strip()
            for item in re.split(r"[\n,，]+", str(value))
            if item.strip()
        ]
