"""Token 使用统计。"""


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
