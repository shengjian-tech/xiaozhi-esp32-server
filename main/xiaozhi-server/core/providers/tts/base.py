import os
import queue
import uuid
import asyncio
import threading
from core.utils import p3
from datetime import datetime
from core.utils import textUtils
from abc import ABC, abstractmethod
from config.logger import setup_logging
from core.utils.util import audio_to_data
from core.utils.tts import MarkdownCleaner
from core.utils.output_counter import add_device_output
from core.handle.reportHandle import enqueue_tts_report
from core.handle.sendAudioHandle import sendAudioMessage
from core.providers.tts.dto.dto import (
    TTSMessageDTO,
    SentenceType,
    ContentType,
    InterfaceType,
)
import re


import traceback

TAG = __name__
logger = setup_logging()


class TTSProviderBase(ABC):
    def __init__(self, config, delete_audio_file):
        self.interface_type = InterfaceType.NON_STREAM
        self.conn = None
        self.tts_timeout = 10
        self.delete_audio_file = delete_audio_file
        self.output_file = config.get("output_dir", "tmp/")
        self.tts_text_queue = queue.Queue()
        self.tts_audio_queue = queue.Queue()
        self.tts_audio_first_sentence = True

        self.tts_text_buff = []
        self.punctuations = (
            "。",
            ".",
            "？",
            "?",
            "！",
            "!",
            "；",
            ";",
            "：",
        )
        self.first_sentence_punctuations = (
            "，",
            "～",
            "~",
            "、",
            ",",
            "。",
            ".",
            "？",
            "?",
            "！",
            "!",
            "；",
            ";",
            "：",
        )
        self.tts_stop_request = False
        self.processed_chars = 0
        self.is_first_sentence = True
        self.brackets_arr = []  # 存放找到的括号及内容
        self.text_before_brackets = ""  # 括号前被忽略的文本
        self.before_text_arr = []   # 括号前被忽略的文本数组

    def generate_filename(self, extension=".wav"):
        return os.path.join(
            self.output_file,
            f"tts-{datetime.now().date()}@{uuid.uuid4().hex}{extension}",
        )

    def to_tts(self, text):
        tmp_file = self.generate_filename()
        try:
            max_repeat_time = 5
            text = MarkdownCleaner.clean_markdown(text)
            while not os.path.exists(tmp_file) and max_repeat_time > 0:
                try:
                    asyncio.run(self.text_to_speak(text, tmp_file))
                except Exception as e:
                    logger.bind(tag=TAG).warning(
                        f"语音生成失败{5 - max_repeat_time + 1}次: {text}，错误: {e}"
                    )
                    # 未执行成功，删除文件
                    if os.path.exists(tmp_file):
                        os.remove(tmp_file)
                    max_repeat_time -= 1

            if max_repeat_time > 0:
                logger.bind(tag=TAG).info(
                    f"语音生成成功: {text}:{tmp_file}，重试{5 - max_repeat_time}次"
                )
            else:
                logger.bind(tag=TAG).error(
                    f"语音生成失败: {text}，请检查网络或服务是否正常"
                )

            return tmp_file
        except Exception as e:
            logger.bind(tag=TAG).error(f"Failed to generate TTS file: {e}")
            return None

    @abstractmethod
    async def text_to_speak(self, text, output_file):
        pass

    def audio_to_pcm_data(self, audio_file_path):
        """音频文件转换为PCM编码"""
        return audio_to_data(audio_file_path, is_opus=False)

    def audio_to_opus_data(self, audio_file_path):
        """音频文件转换为Opus编码"""
        return audio_to_data(audio_file_path, is_opus=True)

    def tts_one_sentence(
        self,
        conn,
        content_type,
        content_detail=None,
        content_file=None,
        sentence_id=None,
    ):
        """发送一句话"""
        if not sentence_id:
            if conn.sentence_id:
                sentence_id = conn.sentence_id
            else:
                sentence_id = str(uuid.uuid4()).replace("-", "")
                conn.sentence_id = sentence_id
        self.tts_text_queue.put(
            TTSMessageDTO(
                sentence_id=sentence_id,
                sentence_type=SentenceType.FIRST,
                content_type=ContentType.ACTION,
            )
        )
        self.tts_text_queue.put(
            TTSMessageDTO(
                sentence_id=sentence_id,
                sentence_type=SentenceType.MIDDLE,
                content_type=content_type,
                content_detail=content_detail,
                content_file=content_file,
            )
        )
        self.tts_text_queue.put(
            TTSMessageDTO(
                sentence_id=sentence_id,
                sentence_type=SentenceType.LAST,
                content_type=ContentType.ACTION,
            )
        )

    async def open_audio_channels(self, conn):
        self.conn = conn
        self.tts_timeout = conn.config.get("tts_timeout", 10)
        # tts 消化线程
        self.tts_priority_thread = threading.Thread(
            target=self.tts_text_priority_thread, daemon=True
        )
        self.tts_priority_thread.start()

        # 音频播放 消化线程
        self.audio_play_priority_thread = threading.Thread(
            target=self._audio_play_priority_thread, daemon=True
        )
        self.audio_play_priority_thread.start()

    # 这里默认是非流式的处理方式
    # 流式处理方式请在子类中重写
    def tts_text_priority_thread(self):
        while not self.conn.stop_event.is_set():
            try:
                message = self.tts_text_queue.get(timeout=1)
                if self.conn.client_abort:
                    logger.bind(tag=TAG).info("收到打断信息，终止TTS文本处理线程")
                    continue
                if message.sentence_type == SentenceType.FIRST:
                    # 初始化参数
                    self.tts_stop_request = False
                    self.processed_chars = 0
                    self.tts_text_buff = []
                    self.is_first_sentence = True
                    self.tts_audio_first_sentence = True
                    self.brackets_arr = []  # 重置
                    self.text_before_brackets = ""  # 重置
                    self.before_text_arr = []  # 重置
                elif ContentType.TEXT == message.content_type:
                    self.tts_text_buff.append(message.content_detail)
                    segment_text = self._get_segment_text()
                    if segment_text:
                        tts_file = self.to_tts(segment_text)
                        if tts_file:
                            audio_datas = self._process_audio_file(tts_file)
                            self.tts_audio_queue.put(
                                (message.sentence_type, audio_datas, segment_text)
                            )
                elif ContentType.FILE == message.content_type:
                    self._process_remaining_text()
                    tts_file = message.content_file
                    if tts_file and os.path.exists(tts_file):
                        audio_datas = self._process_audio_file(tts_file)
                        self.tts_audio_queue.put(
                            (message.sentence_type, audio_datas, message.content_detail)
                        )

                if message.sentence_type == SentenceType.LAST:
                    self._process_remaining_text()
                    self.tts_audio_queue.put(
                        (message.sentence_type, [], message.content_detail)
                    )

            except queue.Empty:
                continue
            except Exception as e:
                logger.bind(tag=TAG).error(
                    f"处理TTS文本失败: {str(e)}, 类型: {type(e).__name__}, 堆栈: {traceback.format_exc()}"
                )
                continue

    def _audio_play_priority_thread(self):
        while not self.conn.stop_event.is_set():
            text = None
            try:
                try:
                    sentence_type, audio_datas, text = self.tts_audio_queue.get(
                        timeout=1
                    )
                except queue.Empty:
                    if self.conn.stop_event.is_set():
                        break
                    continue
                future = asyncio.run_coroutine_threadsafe(
                    sendAudioMessage(self.conn, sentence_type, audio_datas, text),
                    self.conn.loop,
                )
                future.result()
                if self.conn.max_output_size > 0 and text:
                    add_device_output(self.conn.headers.get("device-id"), len(text))
                enqueue_tts_report(self.conn, text, audio_datas)
            except Exception as e:
                logger.bind(tag=TAG).error(
                    f"audio_play_priority priority_thread: {text} {e}"
                )

    async def start_session(self, session_id):
        pass

    async def finish_session(self, session_id):
        pass

    async def close(self):
        """资源清理方法"""
        if hasattr(self, "ws") and self.ws:
            await self.ws.close()

    # def _get_segment_text(self):
    #     # 合并当前全部文本并处理未分割部分
    #     full_text = "".join(self.tts_text_buff)
    #     current_text = full_text[self.processed_chars :]  # 从未处理的位置开始
    #     last_punct_pos = -1
    #
    #     # 根据是否是第一句话选择不同的标点符号集合
    #     punctuations_to_use = (
    #         self.first_sentence_punctuations
    #         if self.is_first_sentence
    #         else self.punctuations
    #     )
    #
    #     for punct in punctuations_to_use:
    #         pos = current_text.rfind(punct)
    #         if (pos != -1 and last_punct_pos == -1) or (
    #             pos != -1 and pos < last_punct_pos
    #         ):
    #             last_punct_pos = pos
    #
    #     if last_punct_pos != -1:
    #         segment_text_raw = current_text[: last_punct_pos + 1]
    #         segment_text = textUtils.get_string_no_punctuation_or_emoji(
    #             segment_text_raw
    #         )
    #         self.processed_chars += len(segment_text_raw)  # 更新已处理字符位置
    #
    #         # 如果是第一句话，在找到第一个逗号后，将标志设置为False
    #         if self.is_first_sentence:
    #             self.is_first_sentence = False
    #
    #         return segment_text
    #     elif self.tts_stop_request and current_text:
    #         segment_text = current_text
    #         self.is_first_sentence = True  # 重置标志
    #         return segment_text
    #     else:
    #         return None

    def _get_segment_text(self):
        # 合并当前全部文本并处理未分割部分
        full_text = "".join(self.tts_text_buff)
        # 判断是否有不成对的括号
        single_bracket = self.has_unpaired_brackets(full_text)
        if single_bracket:
            return None

        skip_text_len = 0

        # 判断是否有双括号
        found, brackets = self.has_paired_brackets(full_text)
        if found and len(self.brackets_arr) < len(brackets) and len(brackets) > 0:
            # 有括号, 且是新的括号
            self.brackets_arr = brackets

            skip_text_len = len(full_text) - self.processed_chars - len(brackets[-1])
            skip_text = full_text[self.processed_chars : (self.processed_chars+skip_text_len)]
            self.before_text_arr.append(skip_text)

            # 将新的括号及内容所占字符的个数加到开始索引上
            self.processed_chars = self.processed_chars + len(brackets[-1]) + skip_text_len


        current_text = "".join(self.before_text_arr) + full_text[self.processed_chars:]



        # 去除'”'后,如果为空字符串返回None
        if self.is_text_empty_after_removing_quotes(current_text):
            return None

        last_punct_pos = -1


        # 根据是否是第一句话选择不同的标点符号集合
        punctuations_to_use = (
            self.first_sentence_punctuations
            if self.is_first_sentence
            else self.punctuations
        )

        for punct in punctuations_to_use:
            pos = current_text.rfind(punct)
            if (pos != -1 and last_punct_pos == -1) or (
                pos != -1 and pos < last_punct_pos
            ):
                last_punct_pos = pos

        if last_punct_pos != -1:
            segment_text_raw = current_text[: last_punct_pos + 1]
            segment_text = textUtils.get_string_no_punctuation_or_emoji(
                segment_text_raw
            )
            # processed_chars的长度中已经包含了text_before_brackets的长度
            """
            self.processed_chars: 嘿，分析员，（双手叉腰，昂起头）   16
            segment_text_raw: 分析员，有我这样的优秀战友在，你居然还想着火锅？  24
            full_text: 嘿，分析员，（双手叉腰，昂起头）有我这样的优秀战友在，你居然还想着火锅？   36
            
            "分析员，"  的长度用了2次, 所以下一次索引不能从40开始,要从 16 + 24 - len(分析员，)  = 36 开始
            """

            # self.processed_chars = self.processed_chars + len(segment_text_raw) - len(self.text_before_brackets)  # 更新已处理字符位置 ----------------
            self.processed_chars = self.processed_chars + len(segment_text_raw) - sum(len(item) for item in self.before_text_arr)  # 更新已处理字符位置

            # 如果是第一句话，在找到第一个逗号后，将标志设置为False
            if self.is_first_sentence:
                self.is_first_sentence = False

            self.text_before_brackets = ""  # 重置
            self.before_text_arr = []  # 重置
            return segment_text
        elif self.tts_stop_request and current_text:
            segment_text = self.remove_parentheses(current_text)
            self.is_first_sentence = True  # 重置标志
            self.brackets_arr = []  # 重置
            self.text_before_brackets = ""  # 重置
            self.before_text_arr = []  # 重置
            return segment_text
        else:
            return None

    def _process_audio_file(self, tts_file):
        """处理音频文件并转换为指定格式

        Args:
            tts_file: 音频文件路径
            content_detail: 内容详情

        Returns:
            tuple: (sentence_type, audio_datas, content_detail)
        """
        audio_datas = []
        if tts_file.endswith(".p3"):
            audio_datas, _ = p3.decode_opus_from_file(tts_file)
        elif self.conn.audio_format == "pcm":
            audio_datas, _ = self.audio_to_pcm_data(tts_file)
        else:
            audio_datas, _ = self.audio_to_opus_data(tts_file)

        if (
            self.delete_audio_file
            and tts_file is not None
            and os.path.exists(tts_file)
            and tts_file.startswith(self.output_file)
        ):
            os.remove(tts_file)
        return audio_datas

    def _process_remaining_text(self):
        """处理剩余的文本并生成语音

        Returns:
            bool: 是否成功处理了文本
        """
        full_text = "".join(self.tts_text_buff)
        remaining_text = "".join(self.before_text_arr) + full_text[self.processed_chars :]
        # 去除单个单引号,若去除单引号后长度为0,返回false,否则将单引号传递给TTS合成会报错
        if self.is_text_empty_after_removing_quotes(remaining_text):
            return False
        if remaining_text:
            segment_text = textUtils.get_string_no_punctuation_or_emoji(remaining_text)
            if segment_text:
                tts_file = self.to_tts(segment_text)
                audio_datas = self._process_audio_file(tts_file)
                self.tts_audio_queue.put(
                    (SentenceType.MIDDLE, audio_datas, segment_text)
                )
                self.processed_chars += len(full_text)
                return True
        return False


    """
    判断文本中是否有单括号(中文括号、英文括号)
    """
    def has_unpaired_brackets(self, text):
        stack = []

        for char in text:
            if char == '(' or char == '（':
                stack.append(char)
            elif char == ')' or char == '）':
                if not stack:
                    # 没有对应的左括号
                    return True
                left = stack.pop()
                # 判断是否匹配（严格匹配中英文）
                if (char == ')' and left != '(') or (char == '）' and left != '（'):
                    return True

        # 最后栈里还有未匹配的左括号
        return len(stack) > 0

    """
    判断文本中是否有成对的括号(中文括号、英文括号)
    """

    def has_paired_brackets(self, text):
        matched_brackets = []  # 存储所有找到的完整括号内容

        # 记录每个左括号的位置和类型
        bracket_positions = []

        for i, char in enumerate(text):
            if char == '(' or char == '（':
                bracket_positions.append((i, char))  # 保存位置和类型
            elif char == ')' and bracket_positions:
                start_idx, open_char = bracket_positions.pop()
                if open_char == '(':
                    end_idx = i + 1
                    matched_brackets.append(text[start_idx:end_idx])
            elif char == '）' and bracket_positions:
                start_idx, open_char = bracket_positions.pop()
                if open_char == '（':
                    end_idx = i + 1
                    matched_brackets.append(text[start_idx:end_idx])

        found = len(matched_brackets) > 0
        return found, matched_brackets

    def remove_parentheses(self, text):
        if text is None or len(text) == 0:
            return None  # 输入为空或 None 时直接返回 None

        # 步骤 1: 删除括号及括号中的内容（支持中英文）
        pattern_parentheses = r'（[^）]*）|$[^)]*$'
        text = re.sub(pattern_parentheses, '', text)

        # 步骤 2: 删除所有单引号 “ 和 ”
        text = text.replace('“', '').replace('”', '')

        # 步骤 3: 去除首尾空白字符
        cleaned_text = text.strip()

        # 步骤 4: 如果最终文本为空，返回 None
        if not cleaned_text:
            return None

        return cleaned_text

    def is_text_empty_after_removing_quotes(self, text):
        if not text:  # 如果是 None 或空字符串
            return True

        # 删除所有类型的单引号（中文、英文、左右引号）
        text_cleaned = text.replace('“', '') \
            .replace('”', '') \
            .replace("'", '') \
            .replace('‘', '') \
            .replace('’', '') \
            .strip()

        # 判断清理后是否为空
        return len(text_cleaned) == 0