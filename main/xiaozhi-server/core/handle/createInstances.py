from core.utils import llm, tts
from config.settings import yamlConfig, getDB
from sqlalchemy import text



"""
获取 llm 实例
"""
async def get_llm_instance(websocket):
    # 获取用户配置的 websocket 地址
    websocket_path = websocket.request.path
    # 获取 websocket 地址中携带的智能体 id
    agent_id = websocket_path[(websocket_path.rfind('/')) + 1:]

    # 将智能体 id 作为 api_key
    yamlConfig["LLM"][yamlConfig["selected_module"]["LLM"]]["api_key"] = agent_id

    # 创建 LLM 实例
    llm_instance = llm.create_instance(
        yamlConfig["LLM"][yamlConfig["selected_module"]["LLM"]]['type'],
        yamlConfig["LLM"][yamlConfig["selected_module"]["LLM"]],
    )

    return llm_instance, agent_id



"""
获取 tts 实例
"""
async def get_tts_instance(agent_id):

    # 查询该智能体选择的 tts
    with getDB() as db:
        # 编写原生 SQL 查询
        sql_query = text("""
            SELECT agents.*, tts_voice.voice_code
            FROM agents
            JOIN tts_voice ON agents.tts_voice_id = tts_voice.id
            WHERE agents.id = :agent_id
        """)

        # 执行查询并传递参数
        res = db.execute(sql_query, {"agent_id": agent_id}).first()

        if res is None:
            # 智能体没有绑定音色, 走免费的 EdgeTTS
            tts_instance = tts.create_instance(
                yamlConfig["TTS"]["EdgeTTS"]["type"],
                yamlConfig["TTS"]["EdgeTTS"],
                yamlConfig["delete_audio"]
            )

        else:
            # 修改音色编码
            yamlConfig["TTS"][yamlConfig["selected_module"]["TTS"]]["voice"] = res.voice_code

            tts_instance = tts.create_instance(
                yamlConfig["TTS"][yamlConfig["selected_module"]["TTS"]]["type"],
                yamlConfig["TTS"][yamlConfig["selected_module"]["TTS"]],
                yamlConfig["delete_audio"]
            )

        return tts_instance










