"""纠正检测 — 测试。"""

from pipeline.correction_handler import detect_correction


class TestDetectCorrection:
    def test_detect_ta_behavior_correction(self):
        assert detect_correction("ta不会这样说")
        assert detect_correction("ta没这么温柔")
        assert detect_correction("ta不可能会这样")

    def test_detect_not_like_ta(self):
        assert detect_correction("这不像ta")
        assert detect_correction("那不像ta")
        assert detect_correction("这不是ta的性格")

    def test_detect_ta_actually_is(self):
        assert detect_correction("ta其实是比较直接的人")
        assert detect_correction("ta应该是用感叹号")

    def test_detect_ta_should_be(self):
        assert detect_correction("ta应该是直来直去的")
        assert detect_correction("ta其实是会撒娇的")

    def test_detect_wrong_style(self):
        assert detect_correction("不是ta的风格")
        assert detect_correction("不是ta的语气")

    def test_normal_conversation_no_false_positive(self):
        # 日常对话不应误触发
        assert not detect_correction("今天天气不对啊")
        assert not detect_correction("不是这样的，你听我说")
        assert not detect_correction("你太温柔了")
        assert not detect_correction("感觉不对，我们换个话题吧")
        assert not detect_correction("不太对，但说不上来")

    def test_no_ta_reference_no_trigger(self):
        # 不含"ta"指代的消息不应触发
        assert not detect_correction("不对不对，你说错了")
        assert not detect_correction("这不是我想要的答案")
        assert not detect_correction("太冷漠了吧你")

    def test_ta_with_negation_triggers(self):
        assert detect_correction("ta不会用这种语气说话")
        assert detect_correction("ta没这么说过")
        assert detect_correction("ta不是这样的人")
