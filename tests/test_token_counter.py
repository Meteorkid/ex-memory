"""Token 计数器 — 测试。"""

from core.token_counter import TokenCounter


class TestTokenCounter:
    def test_initial_state(self):
        counter = TokenCounter()
        assert counter.total_prompt_tokens == 0
        assert counter.total_completion_tokens == 0
        assert counter.total_reasoning_tokens == 0
        assert counter.session_turns == 0

    def test_update_with_usage_object(self):
        counter = TokenCounter()

        class Usage:
            prompt_tokens = 100
            completion_tokens = 50
            completion_tokens_details = None

        counter.update(Usage())
        assert counter.total_prompt_tokens == 100
        assert counter.total_completion_tokens == 50
        assert counter.session_turns == 1

    def test_update_with_reasoning_tokens(self):
        counter = TokenCounter()

        class ReasoningDetails:
            reasoning_tokens = 30

        class Usage:
            prompt_tokens = 200
            completion_tokens = 80
            completion_tokens_details = ReasoningDetails()

        counter.update(Usage())
        assert counter.total_prompt_tokens == 200
        assert counter.total_completion_tokens == 80
        assert counter.total_reasoning_tokens == 30
        assert counter.session_turns == 1

    def test_update_none_does_nothing(self):
        counter = TokenCounter()
        counter.update(None)
        assert counter.session_turns == 0

    def test_multiple_updates_accumulate(self):
        counter = TokenCounter()

        class Usage:
            prompt_tokens = 10
            completion_tokens = 5
            completion_tokens_details = None

        for _ in range(5):
            counter.update(Usage())
        assert counter.total_prompt_tokens == 50
        assert counter.total_completion_tokens == 25
        assert counter.session_turns == 5

    def test_display_summary_no_error(self):
        counter = TokenCounter()

        class Usage:
            prompt_tokens = 10
            completion_tokens = 5
            completion_tokens_details = None

        counter.update(Usage())
        counter.display_summary()  # 不应抛异常
