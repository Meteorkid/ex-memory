"""危机意念识别与求助资源门禁。

设计取向是「宁可误报不可漏报」：漏掉一条真实求救的代价，远大于
误打断一次正常对话。测试据此设定阈值——召回率有硬下限，误报率
只有软上限。
"""

import json
from pathlib import Path

import pytest

from core.safety.crisis import (
    SEVERITY_HIGH,
    CrisisSignal,
    detect_crisis,
    set_semantic_detector,
)

LABELED = Path(__file__).parent / "data" / "crisis_labeled.jsonl"


def _load_labeled():
    return [
        json.loads(line)
        for line in LABELED.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.fixture(autouse=True)
def no_semantic_channel():
    """默认只测规则通道，避免语义通道被注入后影响基线数字。"""
    set_semantic_detector(None)
    yield
    set_semantic_detector(None)


class TestLabeledSetMetrics:
    def test_recall_meets_threshold(self):
        """召回率 ≥ 95%（FR-010 验收标准）。

        注意这个数字的含金量有限：规则与标注集出自同一人之手，它证明的是
        「覆盖了作者能想到的说法」，不是真实世界召回。改写与隐喻要靠语义
        通道，接入前不要对外宣称达到了这个水平。
        """
        rows = _load_labeled()
        positives = [r for r in rows if r["label"] == 1]
        hits = sum(1 for r in positives if detect_crisis(r["text"]).hit)
        recall = hits / len(positives)
        assert recall >= 0.95, f"召回率 {recall:.1%} 低于 95%，漏报不可接受"

    def test_false_positive_rate_is_bounded(self):
        """误报可以接受，但不能高到把正常对话变成不可用。"""
        rows = _load_labeled()
        negatives = [r for r in rows if r["label"] == 0]
        fps = [r for r in negatives if detect_crisis(r["text"]).hit]
        rate = len(fps) / len(negatives)
        assert rate <= 0.15, f"误报率 {rate:.1%} 过高: {[r['text'] for r in fps]}"

    def test_labeled_set_covers_required_categories(self):
        """标注集必须覆盖各类表达，否则召回率没有意义。"""
        cats = {r["category"] for r in _load_labeled() if r["label"] == 1}
        assert {
            "direct",
            "passive",
            "plan",
            "self_harm",
            "loss_specific",
            "farewell",
        } <= cats


class TestHardNegatives:
    """这些是最容易误伤的表达，逐条钉死。"""

    @pytest.mark.parametrize(
        "text",
        [
            "我想死你了",
            "想死我了，快回来",
            "笑死我了哈哈哈",
            "今天累死了",
            "困死了，我先睡了",
            "这题难死了",
            "我的电脑死机了",
            "游戏里我又死了一次",
            "考完试解脱了",
            "这周结束了，终于解脱了",
            "下班了去陪我妈吃饭",
            "我明天要去找他谈点事",
            "最后一次提醒你交房租",
            "我想跟你一起去旅行",
        ],
    )
    def test_not_flagged(self, text):
        assert not detect_crisis(text).hit, f"误报: {text}"


class TestHardPositives:
    """这些必须命中，漏一条就是一次可能致命的失效。"""

    @pytest.mark.parametrize(
        "text",
        [
            "我不想活了",
            "我想死",
            "我想死了",
            "活不下去了",
            "撑不下去了",
            "没有我大家会更好",
            "我想去陪你",
            "等我，我很快就来找你了",
            "我已经写好遗书了",
            "我攒了很多安眠药",
        ],
    )
    def test_flagged(self, text):
        assert detect_crisis(text).hit, f"漏报: {text}"


class TestSignalShape:
    def test_signal_carries_no_user_text(self):
        """信号不得携带用户原话，避免明文流进日志与事件表。"""
        sig = detect_crisis("我不想活了")
        assert sig.hit
        for value in (sig.rule, sig.detector, sig.severity):
            assert "不想活" not in value

    def test_severity_and_rule_are_populated(self):
        sig = detect_crisis("我准备自杀")
        assert sig.severity == SEVERITY_HIGH
        assert sig.rule and sig.confidence > 0

    def test_miss_returns_non_hit_signal(self):
        assert detect_crisis("今天天气真好").hit is False
        assert detect_crisis("").hit is False


class TestResilience:
    def test_detector_failure_does_not_break_chain(self):
        """检测器故障绝不能让对话链路挂掉——但也不能因此静默放过。"""

        class Broken:
            def detect(self, text):
                raise RuntimeError("模型服务不可用")

        set_semantic_detector(Broken())
        # 规则通道仍应命中
        assert detect_crisis("我不想活了").hit
        # 正常文本不会因为异常而误判
        assert not detect_crisis("今天天气真好").hit

    def test_semantic_channel_can_catch_what_rules_miss(self):
        """语义通道接入后应能补规则通道的漏——这里用桩验证接线正确。"""
        text = "这条改写过的表达规则匹配不到"
        assert not detect_crisis(text).hit

        class Semantic:
            def detect(self, _text):
                return CrisisSignal(
                    hit=True,
                    severity=SEVERITY_HIGH,
                    confidence=0.9,
                    detector="semantic",
                    rule="semantic.paraphrase",
                )

        set_semantic_detector(Semantic())
        sig = detect_crisis(text)
        assert sig.hit and sig.detector == "semantic"


class TestResourceReviewGate:
    def test_unreviewed_content_exposes_no_hotline_numbers(self):
        """未经专业审阅时绝不展示具体号码——错号码比没号码更危险。"""
        from core.safety import resources

        resources.reset_cache()
        assert resources.is_reviewed() is False
        resp = resources.get_crisis_response()
        assert resp.hotlines == []
        assert resp.message.strip()

    def test_falls_back_when_content_file_missing(self, monkeypatch, tmp_path):
        """内容文件损坏时仍要有话可说，不能让危机流程静默失效。"""
        from core.safety import resources

        monkeypatch.setattr(resources, "CONTENT_PATH", tmp_path / "missing.json")
        resources.reset_cache()
        resp = resources.get_crisis_response()
        assert resp.message.strip()
        assert resp.hotlines == []
        resources.reset_cache()

    @staticmethod
    def _write(tmp_path, monkeypatch, payload):
        from core.safety import resources

        path = tmp_path / "c.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(resources, "CONTENT_PATH", path)
        resources.reset_cache()
        return resources

    _SIGNED_OFF = {
        "reviewed": True,
        "reviewed_by": "张三（注册心理师 XXXX）",
        "reviewed_at": "2026-09-02",
        "hotlines_verified_at": "2026-09-02",
        "reviewed_message": "已审阅文案",
    }

    def test_reviewed_content_exposes_only_verified_numbers(
        self, monkeypatch, tmp_path
    ):
        resources = self._write(
            tmp_path,
            monkeypatch,
            {
                **self._SIGNED_OFF,
                "hotlines": [
                    {"name": "已核实", "number": "12345", "verified_at": "2026-09-02"},
                    {"name": "未核实", "number": "67890", "verified_at": ""},
                    {"name": "占位未填", "number": ""},
                ],
            },
        )
        resp = resources.get_crisis_response()
        assert resp.reviewed is True
        assert [h["name"] for h in resp.hotlines] == ["已核实"]
        resources.reset_cache()

    def test_reviewed_true_without_signoff_is_treated_as_unreviewed(
        self, monkeypatch, tmp_path
    ):
        """🔴 光把 reviewed 写成 true 不算数，必须留下审阅痕迹。

        否则一次顺手的改动就能让未经核对的号码上线。
        """
        resources = self._write(
            tmp_path,
            monkeypatch,
            {
                "reviewed": True,
                "reviewed_message": "声称已审阅",
                "fallback_message": "兜底文案",
                "hotlines": [{"name": "热线", "number": "12345"}],
            },
        )
        assert resources.is_reviewed() is False
        resp = resources.get_crisis_response()
        assert resp.hotlines == []
        assert resp.message == "兜底文案"
        resources.reset_cache()

    def test_unverified_hotline_does_not_disable_the_others(
        self, monkeypatch, tmp_path
    ):
        """一条未核实的备选不应让所有热线一起消失——那比它防的问题更糟。"""
        resources = self._write(
            tmp_path,
            monkeypatch,
            {
                **self._SIGNED_OFF,
                "hotlines": [
                    {"name": "已核实", "number": "12345", "verified_at": "2026-09-02"},
                    {"name": "待研究", "number": "67890", "verified_at": ""},
                ],
            },
        )
        resp = resources.get_crisis_response()
        assert [h["name"] for h in resp.hotlines] == ["已核实"]
        resources.reset_cache()

    def test_no_verified_hotline_falls_back_to_non_promising_copy(
        self, monkeypatch, tmp_path
    ):
        """已审阅但一条都没核实通过时，不能展示承诺了热线的文案。"""
        resources = self._write(
            tmp_path,
            monkeypatch,
            {
                **self._SIGNED_OFF,
                "fallback_message": "兜底文案",
                "hotlines": [{"name": "热线", "number": "12345", "verified_at": ""}],
            },
        )
        resp = resources.get_crisis_response()
        assert resp.hotlines == []
        assert resp.message == "兜底文案"
        resources.reset_cache()

    def test_shipped_content_file_is_not_marked_reviewed(self):
        """仓库里带的文件必须始终是未审阅态——防止误提交一个已签字的版本。"""
        from core.safety import resources

        resources.reset_cache()
        assert resources.is_reviewed() is False
        resources.reset_cache()
