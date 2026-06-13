# AstrBot 悟声 TTS 插件

这个插件会在 AstrBot 发送 AI 回复后读取本次回复文本。当回复的估算 token 数低于配置阈值时，插件会先调用当前会话使用的 LLM 把内容翻译成日语，再调用悟声 AI 实时 TTS 接口生成音频，并把音频作为文件发送到当前会话。

当前版本按“文件”发送音频，后续可以把发送组件替换为平台支持的语音组件，实现直接发送群语音。

## 安装

将整个 `astrbot_plugin_wusound_tts` 文件夹放到 AstrBot 插件目录，例如：

```text
data/plugins/astrbot_plugin_wusound_tts
```

然后在 AstrBot WebUI 中重载插件或重启 AstrBot。

## 必填配置

```text
api_key: 悟声开发者中心创建的 API Key
voice_id: 悟声语音角色 ID
```

也可以不在配置里填写 `api_key`，改用环境变量：

```text
WUSOUND_API_KEY=你的悟声 API Key
```

## 常用配置

```text
enabled: true
tts_endpoint: https://v1.wusound.cn/api/tts/simple-generate
voice_id: 你的悟声角色 ID
prompt_id: 可选的风格 ID
audio_format: mp3
send_as: file
use_context_send_message: true
prefer_remote_url: true
max_output_tokens: 80
translate_to_japanese: true
```

`max_output_tokens` 是短回复阈值。超过这个估算 token 数就不会生成音频，避免长回复拖慢群聊。

`prefer_remote_url` 建议保持开启。悟声会返回公网 mp3 地址，直接让平台从 URL 发送文件通常比先下载到 AstrBot 本地再发送更稳定，尤其是 Docker、远程适配器或 OneBot 分离部署时。

`send_as` 默认是 `file`，会发送音频文件。可以改成 `record` 尝试直接发送语音，但不同平台对语音格式和组件支持差异很大，建议先用文件跑通。

## 悟声接口适配

插件默认按下面结构请求悟声：

```json
{
  "text": "翻译后的日语文本",
  "voiceId": "你的语音角色 ID",
  "promptId": "可选风格 ID",
  "format": "mp3"
}
```

如果你的悟声控制台示例字段不同，可以在 `payload_template` 中覆盖请求体，例如：

```json
{"content":"{{text}}","voice_id":"你的角色ID","format":"mp3"}
```

`{{text}}` 会被替换为日语文本。插件支持悟声直接返回音频内容，也支持从 JSON 结果里提取 `url`、`audioUrl`、`audio_url`、`fileUrl`、`audioBase64` 等字段。

## 工作流程

```text
AstrBot AI 回复
-> 插件读取回复纯文本
-> 估算 token 数并判断是否低于阈值
-> 使用当前会话 LLM 翻译成日语
-> 调用悟声实时 TTS
-> 优先读取悟声返回的远程 mp3 URL
-> 使用 AstrBot 主动消息接口发送到当前会话
```

## 已知限制

- 翻译依赖当前会话可用的 LLM；如果没有可用 LLM，插件会直接把原文送去 TTS。
- 不同平台对文件消息和语音消息的支持不同；如果文件能发、语音不能发，通常是平台适配器限制。
- 当前 token 统计是轻量估算，不是精确模型 token 计数。
- 悟声接口字段如果与默认值不一致，优先通过 `payload_template` 适配。
