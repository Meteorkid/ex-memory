"""证据缺失生成端行为评测的单元测试（不调用外部 API）。"""

import json

import pytest

from evals.abstention_eval import (
    AbstentionJudgeError,
    _parse_stance,
    STANCES,
)


class TestParseStance:
    def test_abstain_ok(self):
        out = _parse_stance(
            json.dumps({"stance": "abstain", "reason": "表示不记得", "fabricated_claims": []})
        )
        assert out["stance"] == "abstain"
        assert out["fabricated_claims"] == []

    def test_fabricate_with_claims(self):
        out = _parse_stance(
            json.dumps(
                {
                    "stance": "fabricate",
                    "reason": "给出具体事实",
                    "fabricated_claims": ["胃不好", "一吃辣就闹腾"],
                }
            )
        )
        assert out["stance"] == "fabricate"
        assert len(out["fabricated_claims"]) == 2

    def test_hedge_ok(self):
        out = _parse_stance(json.dumps({"stance": "hedge", "reason": "反问回避"}))
        assert out["stance"] == "hedge"
        assert out["fabricated_claims"] == []

    def test_missing_claims_defaults_empty(self):
        out = _parse_stance(json.dumps({"stance": "abstain"}))
        assert out["fabricated_claims"] == []

    def test_invalid_stance_rejected(self):
        with pytest.raises(ValueError, match="stance 取值非法"):
            _parse_stance(json.dumps({"stance": "unknown"}))

    def test_missing_stance_rejected(self):
        with pytest.raises(ValueError):
            _parse_stance(json.dumps({"reason": "no stance"}))

    def test_claims_not_list_rejected(self):
        with pytest.raises(ValueError, match="fabricated_claims 必须是数组"):
            _parse_stance(json.dumps({"stance": "fabricate", "fabricated_claims": "x"}))

    def test_bad_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            _parse_stance("{not json")


class TestStancesConstant:
    def test_three_stances(self):
        assert set(STANCES) == {"abstain", "hedge", "fabricate"}


class TestMajorityLogic:
    """多数姿态定论 + 平票取保守侧的规则。"""

    @staticmethod
    def _majority(runs: list[str]) -> tuple[str, bool]:
        tally = {s: sum(1 for r in runs if r == s) for s in STANCES}
        top = max(tally.values())
        tied = [s for s in ("fabricate", "hedge", "abstain") if tally[s] == top]
        return tied[0], top == len(runs)

    def test_unanimous_abstain(self):
        stance, consistent = self._majority(["abstain"] * 3)
        assert stance == "abstain"
        assert consistent is True

    def test_majority_fabricate(self):
        stance, consistent = self._majority(["fabricate", "abstain", "fabricate"])
        assert stance == "fabricate"
        assert consistent is False

    def test_tie_prefers_fabricate(self):
        # 1:1 平票时取 fabricate，避免低估编造风险
        stance, _ = self._majority(["fabricate", "abstain"])
        assert stance == "fabricate"

    def test_tie_prefers_hedge_over_abstain(self):
        stance, _ = self._majority(["hedge", "abstain"])
        assert stance == "hedge"

    def test_all_different_prefers_fabricate(self):
        stance, consistent = self._majority(["fabricate", "hedge", "abstain"])
        assert stance == "fabricate"
        assert consistent is False


class TestJudgeErrorIsException:
    def test_subclass_of_exception(self):
        assert issubclass(AbstentionJudgeError, Exception)
