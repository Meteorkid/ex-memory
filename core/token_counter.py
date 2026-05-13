"""Token 使用统计（CLI 用户界面输出）。"""


class TokenCounter:
    def __init__(self):
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_reasoning_tokens = 0
        self.session_turns = 0

    def update(self, usage):
        if not usage:
            return
        self.total_prompt_tokens += getattr(usage, "prompt_tokens", 0)
        self.total_completion_tokens += getattr(usage, "completion_tokens", 0)
        details = getattr(usage, "completion_tokens_details", None)
        if details:
            self.total_reasoning_tokens += getattr(details, "reasoning_tokens", 0)
        self.session_turns += 1

    def display_summary(self):
        """向终端输出本次会话的 Token 统计。这是 CLI 交互界面的一部分，用 print 是合理的。"""
        total = self.total_prompt_tokens + self.total_completion_tokens
        print("\n" + "─" * 35)
        print(" Token used")
        print("-" * 35)
        print(f" Prompt_tokens:     {self.total_prompt_tokens:>8}")
        print(f" Completion_tokens: {self.total_completion_tokens:>8}")
        if self.total_reasoning_tokens > 0:
            print(f" Reasoning_tokens: {self.total_reasoning_tokens:>8}")
        print()
        print(f" Total:             {total:>8}")
        print(f" Rounds:            {self.session_turns:>8}")
        print("─" * 35 + "\n")
