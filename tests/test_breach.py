"""越界检测与盲测（FR-061）。

「越界」指镜像做出了不属于「ta」的行为：自称 AI、百科式回答、公文语气。
这类失败很显眼，用户一眼出戏，但此前没有任何度量。
"""

import json

import pytest

from evals.breach import (
    breach_rate,
    build_blind_set,
    detect,
    export_blind_set,
    score_blind_set,
)


class TestDetect:
    def test_natural_chat_is_not_breach(self):
        for text in ["在吗", "今天好累", "哈哈哈", "嗯嗯知道了", "早点睡"]:
            assert detect(text).breached is False, text

    def test_self_identifying_as_ai(self):
        result = detect("作为一个AI助手，我理解您的感受")
        assert result.breached
        assert "self_identify_ai" in result.categories

    def test_denying_feelings_is_breach(self):
        assert detect("我没有真实的情感").breached

    def test_assistant_tone(self):
        assert "assistant_tone" in detect("希望我的回答对你有帮助").categories

    def test_encyclopedic_structure(self):
        assert "encyclopedic" in detect("首先要明确，其次需要考虑，最后再看").categories

    def test_formal_written_language(self):
        assert "formal_written" in detect("您好，建议您先休息").categories

    def test_meta_disclosure(self):
        assert "meta_disclosure" in detect("我是根据聊天记录生成的").categories

    def test_multiple_categories_reported(self):
        result = detect("您好，有什么可以帮到你")
        assert len(result.categories) >= 2

    def test_empty_text(self):
        assert detect("").breached is False
        assert detect("   ").breached is False

    def test_matched_fragment_is_returned_for_debugging(self):
        result = detect("作为一个AI助手")
        assert result.matches and "AI" in result.matches[0]


class TestBreachRate:
    def test_all_clean(self):
        stats = breach_rate(["在吗", "好呀", "嗯"])
        assert stats["rate"] == 0.0 and stats["breached"] == 0

    def test_mixed(self):
        stats = breach_rate(["在吗", "作为一个AI，我", "好呀", "您好"])
        assert stats["breached"] == 2
        assert stats["rate"] == 0.5

    def test_empty_input(self):
        assert breach_rate([])["total"] == 0
        assert breach_rate(["", "  "])["total"] == 0

    def test_categories_are_ranked(self):
        stats = breach_rate(["您好", "您好", "作为一个AI"])
        assert list(stats["by_category"])[0] in ("formal_written", "assistant_tone")


class TestBlindSet:
    REAL = [f"真话{i}" for i in range(10)]
    GENERATED = [f"生成{i}" for i in range(10)]

    def test_each_question_has_two_options(self):
        questions = build_blind_set(self.REAL, self.GENERATED, size=5)
        assert len(questions) == 5
        assert all(len(q["options"]) == 2 for q in questions)

    def test_answer_points_to_the_real_one(self):
        for q in build_blind_set(self.REAL, self.GENERATED, size=5):
            answer_text = next(
                o["text"] for o in q["options"] if o["label"] == q["answer"]
            )
            assert answer_text.startswith("真话")

    def test_order_is_shuffled_not_always_a(self):
        answers = {
            q["answer"] for q in build_blind_set(self.REAL, self.GENERATED, size=10)
        }
        assert len(answers) > 1, "真话永远在同一个位置，盲测就没意义了"

    def test_deterministic_with_same_seed(self):
        a = build_blind_set(self.REAL, self.GENERATED, size=5, seed=7)
        b = build_blind_set(self.REAL, self.GENERATED, size=5, seed=7)
        assert a == b

    def test_empty_side_raises(self):
        with pytest.raises(ValueError):
            build_blind_set([], self.GENERATED)

    def test_export_hides_answers(self, tmp_path):
        """题面交给标注者，不能带答案。"""
        questions = build_blind_set(self.REAL, self.GENERATED, size=3)
        path = export_blind_set(questions, tmp_path / "blind.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert all("answer" not in q for q in payload)


class TestScoring:
    def test_perfect_discrimination_is_bad(self):
        """全猜对说明一眼能看出哪句是生成的。"""
        questions = build_blind_set(
            [f"真{i}" for i in range(4)], [f"假{i}" for i in range(4)]
        )
        answers = {q["id"]: q["answer"] for q in questions}
        score = score_blind_set(questions, answers)
        assert score["discrimination"] == 1.0
        assert score["distance_from_ideal"] == 0.5

    def test_chance_level_is_ideal(self):
        questions = build_blind_set(
            [f"真{i}" for i in range(4)], [f"假{i}" for i in range(4)]
        )
        answers = {}
        for i, q in enumerate(questions):
            correct = q["answer"]
            answers[q["id"]] = (
                correct if i % 2 == 0 else ("A" if correct == "B" else "B")
            )
        score = score_blind_set(questions, answers)
        assert score["discrimination"] == 0.5
        assert score["distance_from_ideal"] == 0.0

    def test_partial_answers_only_grade_answered(self):
        questions = build_blind_set(
            [f"真{i}" for i in range(4)], [f"假{i}" for i in range(4)]
        )
        score = score_blind_set(questions, {questions[0]["id"]: questions[0]["answer"]})
        assert score["answered"] == 1

    def test_no_answers(self):
        questions = build_blind_set(["真"], ["假"])
        assert score_blind_set(questions, {})["discrimination"] is None


class TestMirrorIntegration:
    def test_blind_set_uses_real_corpus(self, tmp_path, monkeypatch):
        from core.corpus_store import append_messages
        from evals.breach import blind_set_from_mirror

        monkeypatch.setattr("config.EXES_DIR", tmp_path / "exes")
        (tmp_path / "exes" / "bl").mkdir(parents=True)
        append_messages(
            "bl",
            [{"content": f"ta 的原话{i}", "is_target": True} for i in range(5)],
            source="wechat",
        )
        questions = blind_set_from_mirror("bl", [f"生成{i}" for i in range(5)], size=5)
        assert len(questions) == 5
