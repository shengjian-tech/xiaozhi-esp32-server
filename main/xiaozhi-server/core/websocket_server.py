import asyncio
import websockets
from config.logger import setup_logging
from core.connection import ConnectionHandler
from core.utils.util import get_local_ip
from core.utils import asr, vad, llm, tts, memory, intent
from core.handle.createInstances import get_llm_instance, get_tts_instance

TAG = __name__


class WebSocketServer:
    def __init__(self, config: dict):
        self.config = config
        self.logger = setup_logging()
        self._vad, self._asr, self._memory, self.intent = self._create_processing_instances()
        self.active_connections = set()  # 添加全局连接记录

    def _create_processing_instances(self):
        memory_cls_name = self.config["selected_module"].get("Memory", "nomem") # 默认使用nomem
        has_memory_cfg = self.config.get("Memory") and memory_cls_name in self.config["Memory"]
        memory_cfg = self.config["Memory"][memory_cls_name] if has_memory_cfg else {}

        """创建处理模块实例"""
        return (
            vad.create_instance(
                self.config["selected_module"]["VAD"],
                self.config["VAD"][self.config["selected_module"]["VAD"]]
            ),
            asr.create_instance(
                self.config["selected_module"]["ASR"]
                if not 'type' in self.config["ASR"][self.config["selected_module"]["ASR"]]
                else
                self.config["ASR"][self.config["selected_module"]["ASR"]]["type"],
                self.config["ASR"][self.config["selected_module"]["ASR"]],
                self.config["delete_audio"]
            ),
            memory.create_instance(memory_cls_name, memory_cfg),
            intent.create_instance(
                self.config["selected_module"]["Intent"]
                if not 'type' in self.config["Intent"][self.config["selected_module"]["Intent"]]
                else
                self.config["Intent"][self.config["selected_module"]["Intent"]]["type"],
                self.config["Intent"][self.config["selected_module"]["Intent"]]
            ),
        )

    async def start(self):
        server_config = self.config["server"]
        host = server_config["ip"]
        port = server_config["port"]

        self.logger.bind(tag=TAG).info("Server is running at ws://{}:{}", get_local_ip(), port)
        self.logger.bind(tag=TAG).info("=======上面的地址是websocket协议地址，请勿用浏览器访问=======")
        async with websockets.serve(
                self._handle_connection,
                host,
                port
        ):
            await asyncio.Future()

    async def _handle_connection(self, websocket):
        """根据用户的连接信息创建 llm 实例"""
        new_llm_instance, agent_id = await get_llm_instance(websocket)
        """根据用户的连接信息创建 tts 实例"""
        new_tts_instance = await get_tts_instance(agent_id)

        """处理新连接，每次创建独立的ConnectionHandler"""
        # 创建ConnectionHandler时传入当前server实例
        handler = ConnectionHandler(self.config, self._vad, self._asr, new_llm_instance, new_tts_instance, self._memory, self.intent)
        self.active_connections.add(handler)
        try:
            await handler.handle_connection(websocket)
        finally:
            self.active_connections.discard(handler)
