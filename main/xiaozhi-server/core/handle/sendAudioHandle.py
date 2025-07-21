import json
import asyncio
import time
from core.providers.tts.dto.dto import SentenceType
from core.utils.util import get_string_no_punctuation_or_emoji, analyze_emotion
from loguru import logger
import re

TAG = __name__

emoji_map = {
    "neutral": "Neutral",
    "happy": "Happy",
    "laughing": "Happy",
    "funny": "Happy",
    "sad": "Sad",
    "angry": "Angry",
    "crying": "Cry",
    "loving": "Happy",
    "embarrassed": "Embarrassed",
    "surprised": "Surprised",
    "shocked": "Shock",
    "thinking": "Confused",
    "winking": "Wink",
    "cool": "Happy",
    "relaxed": "Happy",
    "delicious": "Happy",
    "kissy": "Happy",
    "confident": "Happy",
    "sleepy": "Sleepy",
    "silly": "Happy",
    "confused": "Confused"
}


async def sendAudioMessage(conn, sentenceType, audios, text):
    # 去除括号中的语气词
    text = await handle_text(text)
    # 发送句子开始消息
    if text is not None:
        emotion = analyze_emotion(text)
        emoji = emoji_map.get(emotion, "happy")  # 默认使用笑脸
        await conn.websocket.send(
            json.dumps(
                {
                    "type": "llm",
                    "text": emoji,
                    "emotion": emotion,
                    "session_id": conn.session_id,
                }
            )
        )
    pre_buffer = False
    if conn.tts.tts_audio_first_sentence and text is not None:
        conn.logger.bind(tag=TAG).info(f"发送第一段语音: {text}")
        conn.tts.tts_audio_first_sentence = False
        pre_buffer = True

    await send_tts_message(conn, "sentence_start", text)

    await sendAudio(conn, audios, pre_buffer)

    await send_tts_message(conn, "sentence_end", text)

    # 发送结束消息（如果是最后一个文本）
    if conn.llm_finish_task and sentenceType == SentenceType.LAST:
        await send_tts_message(conn, "stop", None)
        conn.client_is_speaking = False
        if conn.close_after_chat:
            await conn.close()


# 播放音频
async def sendAudio(conn, audios, pre_buffer=True):
    if audios is None or len(audios) == 0:
        return
    # 流控参数优化
    frame_duration = 60  # 帧时长（毫秒），匹配 Opus 编码
    start_time = time.perf_counter()
    play_position = 0
    last_reset_time = time.perf_counter()  # 记录最后的重置时间

    # 仅当第一句话时执行预缓冲
    if pre_buffer:
        pre_buffer_frames = min(3, len(audios))
        for i in range(pre_buffer_frames):
            await conn.websocket.send(audios[i])
        remaining_audios = audios[pre_buffer_frames:]
    else:
        remaining_audios = audios

    # 播放剩余音频帧
    for opus_packet in remaining_audios:
        if conn.client_abort:
            break

        # 每分钟重置一次计时器
        if time.perf_counter() - last_reset_time > 60:
            await conn.reset_timeout()
            last_reset_time = time.perf_counter()

        # 计算预期发送时间
        expected_time = start_time + (play_position / 1000)
        current_time = time.perf_counter()
        delay = expected_time - current_time
        if delay > 0:
            await asyncio.sleep(delay)

        await conn.websocket.send(opus_packet)

        play_position += frame_duration


async def send_tts_message(conn, state, text=None):
    """发送 TTS 状态消息"""
    message = {"type": "tts", "state": state, "session_id": conn.session_id}
    if text is not None:
        message["text"] = text

    # TTS播放结束
    if state == "stop":
        # 播放提示音
        tts_notify = conn.config.get("enable_stop_tts_notify", False)
        if tts_notify:
            stop_tts_notify_voice = conn.config.get(
                "stop_tts_notify_voice", "config/assets/tts_notify.mp3"
            )
            audios, _ = conn.tts.audio_to_opus_data(stop_tts_notify_voice)
            await sendAudio(conn, audios)
        # 清除服务端讲话状态
        conn.clearSpeakStatus()

    # 发送消息到客户端
    await conn.websocket.send(json.dumps(message))


async def send_stt_message(conn, text):
    end_prompt_str = conn.config.get("end_prompt", {}).get("prompt")
    if end_prompt_str and end_prompt_str == text:
        await send_tts_message(conn, "start")
        return

    """发送 STT 状态消息"""
    stt_text = get_string_no_punctuation_or_emoji(text)
    await conn.websocket.send(
        json.dumps({"type": "stt", "text": stt_text, "session_id": conn.session_id})
    )
    conn.client_is_speaking = True
    await send_tts_message(conn, "start")


"""
移除括号中的语气词
"""
async def handle_text(text):
    if not text or len(text) == 0:
        return text

    # 1. 删除括号及其内容：包括中文括号（）和英文括号()
    text = re.sub(r'（[^）]*）|$[^)]*$', '', text)

    # 2. 使用栈检测成对引号，标记孤立引号为待删除
    stack = []
    chars = list(text)
    quote_pairs = {'"': '"', '“': '”', '‘': '’'}
    quote_positions = {}  # 记录每一对引号的位置

    for i, ch in enumerate(chars):
        if ch in quote_pairs:
            stack.append((i, ch))  # 左引号入栈
        elif ch in quote_pairs.values():
            if stack and quote_pairs.get(stack[-1][1]) == ch:
                left_idx, left_quote = stack.pop()
                quote_positions[left_idx] = i  # 记录成对引号范围
                quote_positions[i] = left_idx
            else:
                # 孤立右引号，标记删除
                chars[i] = '\x00DELETE\x00'

    # 标记未闭合的左引号为待删除
    for pos, _ in stack:
        chars[pos] = '\x00DELETE\x00'

    # 构建新字符串，暂时不处理引号
    temp_text = ''.join([c for c in chars if c != '\x00DELETE\x00'])

    # 3. 清理残留符号，但保留成对引号和非首尾省略号
    cleaned_parts = []
    i = 0
    while i < len(temp_text):
        matched = False
        # 检查是否在成对引号内
        in_quotes = any(start <= i <= end for start, end in quote_positions.items())

        # 如果当前位置是符号，并且不在引号中，则考虑删除
        if not in_quotes:
            # 匹配单独的标点符号（不包括成对引号中的）
            symbol_match = re.match(r'[‘’"()（）\u2026.]', temp_text[i:])
            if symbol_match:
                char = symbol_match.group(0)
                # 判断是否是省略号的一部分
                ellipsis_match = re.match(r'(…|\.\.\.|\u2026)', temp_text[i:])
                if ellipsis_match:
                    ellipsis = ellipsis_match.group(0)
                    # 判断是否处于文本中间（不是开头或结尾）
                    start_pos = i
                    end_pos = i + len(ellipsis)
                    if 0 < start_pos or end_pos < len(temp_text):
                        # 保留省略号
                        cleaned_parts.append(ellipsis)
                        i += len(ellipsis)
                        matched = True
                    else:
                        # 首或尾的省略号，删除
                        i += len(ellipsis)
                        matched = True
                else:
                    # 其他符号，删除
                    i += 1
                    matched = True

        if not matched:
            cleaned_parts.append(temp_text[i])
            i += 1

    cleaned_text = ''.join(cleaned_parts)

    # 4. 去除首尾空白字符
    final_text = cleaned_text.strip()
    return final_text