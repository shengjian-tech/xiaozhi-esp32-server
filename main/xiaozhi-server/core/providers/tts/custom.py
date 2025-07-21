import os
import json
import uuid
import requests
from config.logger import setup_logging
from datetime import datetime
from core.providers.tts.base import TTSProviderBase
from config.config_loader import read_config, get_project_dir, load_config

TAG = __name__
logger = setup_logging()

class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.url = config.get("url")
        self.method = config.get("method", "GET")
        self.headers = config.get("headers", {})
        self.format = config.get("format", "wav")
        self.audio_file_type = config.get("format", "wav")
        self.output_file = config.get("output_dir", "tmp/")
        self.params = config.get("params")

        if isinstance(self.params, str):
            try:
                self.params = json.loads(self.params)
            except json.JSONDecodeError:
                raise ValueError("Custom TTS配置参数出错,无法将字符串解析为对象")
        elif not isinstance(self.params, dict):
            raise TypeError("Custom TTS配置参数出错, 请参考配置说明")

    def generate_filename(self):
        return os.path.join(self.output_file, f"tts-{datetime.now().date()}@{uuid.uuid4().hex}.{self.format}")

    async def text_to_speak(self, text, output_file):
        # request_params = {}
        # for k, v in self.params.items():
        #     if isinstance(v, str) and "{prompt_text}" in v:
        #         v = v.replace("{prompt_text}", text)
        #     request_params[k] = v
        #
        # if self.method.upper() == "POST":
        #     resp = requests.post(self.url, json=request_params, headers=self.headers)
        # else:
        #     resp = requests.get(self.url, params=request_params, headers=self.headers)
        # if resp.status_code == 200:
        #     with open(output_file, "wb") as file:
        #         file.write(resp.content)
        # else:
        #     error_msg = f"Custom TTS请求失败: {resp.status_code} - {resp.text}"
        #     logger.bind(tag=TAG).error(error_msg)
        #     raise Exception(error_msg)  # 抛出异常，让调用方捕获

        # 处理文本
        text = await self.remove_unmatched_quotes(text)
        # POST 方法请求(/speech)
        request_body = {}
        for k, v in self.params.items():
            if isinstance(v, str) and "{prompt_text}" in v:
                v = v.replace("{prompt_text}", text)
            request_body[k] = v

        # 如果 voiceType 不存在，默认是 "fixed"
        # ragkb作为后台,没有voiceType属性,toptok作为后台有voiceType属性
        voice_type = request_body.get("voiceType", "fixed")

        if voice_type  == "fixed":
            resp = requests.post(self.url, headers=self.headers, data=json.dumps(request_body, ensure_ascii=False).encode('utf-8'))
        elif voice_type  == "clone":
            resp = requests.post(self.url, json=request_body)

        if resp.status_code == 200:
            if output_file:
                with open(output_file, "wb") as file:
                    file.write(resp.content)
            else:
                return resp.content

        else:
            error_msg = f"Custom TTS请求失败: {resp.status_code} - {resp.text}"
            logger.bind(tag=TAG).error(error_msg)
            raise Exception(error_msg)  # 抛出异常，让调用方捕获



    # 去除字符串中不成对的引号
    async def remove_unmatched_quotes(self, text):
        if not text:
            return text

        result = []
        stack = []
        quote_positions = []  # 存储所有引号的位置和类型：例如 {'index': 5, 'char': '"', 'paired': False}

        for i, c in enumerate(text):
            if c in ('"', "'"):
                if stack and stack[-1]['char'] == c:
                    # 找到闭合引号，弹出栈顶
                    start = stack.pop()
                    quote_positions.append((start['index'], i, c))
                else:
                    # 开启一个新的引号
                    stack.append({'index': i, 'char': c})

        # 构建结果字符串，仅保留成对引号之间的内容
        paired_ranges = set()
        for start, end, _ in quote_positions:
            paired_ranges.update(range(start, end + 1))

        result = []
        for i, c in enumerate(text):
            if c in ('"', "'"):
                if i in paired_ranges:
                    result.append(c)
            else:
                result.append(c)

        return ''.join(result)