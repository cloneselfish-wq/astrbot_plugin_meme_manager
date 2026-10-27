"""点名合成路径：用户明确要求生成表情包时绕过会话节奏掷骰。"""

from types import SimpleNamespace

from astrbot_plugin_meme_manager import main as plugin_main
from astrbot_plugin_meme_manager.main import (
    COMPOSE_EXPLICIT_PATTERN,
    MemeSender,
)


class FakeEvent:
    def __init__(self, text: str, self_id: str = "3177624799"):
        self._text = text
        self._extras: dict = {}
        self.unified_msg_origin = "aiocqhttp:GroupMessage:123"
        self.session_id = "123"
        self.self_id = self_id

    def get_extra(self, key):
        return self._extras.get(key)

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_message_str(self) -> str:
        return self._text

    def get_self_id(self) -> str:
        return self.self_id


def make_sender(service=None, allowed: bool = True) -> MemeSender:
    sender = object.__new__(MemeSender)
    sender.config = {}
    sender.compose_service = service
    sender._compose_bot_allowed = lambda event: allowed
    return sender


def test_explicit_pattern_hits_request_phrasings():
    hits = [
        "saaya帮我生成一个又要掏钱了的表情包",
        "帮我生成一个表情包",
        "做个掏钱表情包",
        "来一个哭哭的表情",
        "来点表情包",
        "整一张贴纸",
        "给我生成meme",
        "帮表情包生成一下",
    ]
    for text in hits:
        assert COMPOSE_EXPLICIT_PATTERN.search(text), text


def test_explicit_pattern_ignores_non_requests():
    misses = [
        "今天心情不错",
        "这个表情包太好笑了",
        "刚才那个梗图谁发的",
        "帮我发一下表情包",
        "等下回来发表情",
        "抽卡又要掏钱了",
    ]
    for text in misses:
        assert not COMPOSE_EXPLICIT_PATTERN.search(text), text


def test_explicit_requested_caches_per_event():
    sender = make_sender()
    event = FakeEvent("帮我生成一个表情包")
    assert sender._compose_explicit_requested(event) is True
    # 即使文本被替换也读缓存，保证同一轮决策稳定
    event._text = "今天天气不错"
    assert sender._compose_explicit_requested(event) is True
    assert event.get_extra("meme_manager_compose_explicit") is True


def test_explicit_request_forces_activation_without_dice():
    dice_calls = []

    class Service:
        def should_compose(self, key):
            dice_calls.append(key)
            return False

    sender = make_sender(Service())
    event = FakeEvent("saaya帮我生成一个又要掏钱了的表情包")
    assert sender._compose_should_activate(event) is True
    assert dice_calls == []
    assert event.get_extra("meme_manager_compose_active") is True
    assert event.get_extra("meme_manager_compose_variation")


def test_non_explicit_round_still_uses_dice():
    class Service:
        def should_compose(self, key):
            return False

    sender = make_sender(Service())
    event = FakeEvent("今天心情不错")
    assert sender._compose_should_activate(event) is False
    assert event.get_extra("meme_manager_compose_active") is False


def test_explicit_request_respects_bot_whitelist():
    sender = make_sender(SimpleNamespace(should_compose=lambda key: False), allowed=False)
    event = FakeEvent("帮我生成一个表情包", self_id="2587837206")
    assert sender._compose_should_activate(event) is False


def test_activation_decision_is_cached():
    sender = make_sender(None)
    event = FakeEvent("帮我生成一个表情包")
    event.set_extra("meme_manager_compose_active", True)
    # 即使服务不可用，也沿用本轮已缓存的决策
    assert sender._compose_should_activate(event) is True


def test_explicit_prompt_tells_model_to_compose():
    prompt = MemeSender._compose_explicit_system_prompt()
    assert "明确要求" in prompt
    assert "必须调用一次 compose_sticker" in prompt
    assert COMPOSE_PROMPT_MARKERS_OK(prompt)


def COMPOSE_PROMPT_MARKERS_OK(prompt: str) -> bool:
    return prompt.lstrip("\n").startswith(plugin_main.COMPOSE_PROMPT_MARKER_START) and (
        prompt.rstrip("\n").endswith(plugin_main.COMPOSE_PROMPT_MARKER_END)
    )
