"""
nlp/bert/inference.py — 학습된 BERT 모델 단독 추론 테스트
──────────────────────────────────────────────────────────
실행:
    python -m monitoring_llm.nlp.bert.inference
    python -m monitoring_llm.nlp.bert.inference --model ./nlp/bert/intent_model
"""

import argparse
import os
import sys

import torch
import torch.nn.functional as F
from transformers import AutoModelForSequenceClassification, AutoTokenizer

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "../../.."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

DEFAULT_MODEL_PATH = os.getenv(
    "BERT_MODEL_PATH", os.path.join(_HERE, "intent_model")
)


class BertIntentClassifier:
    """학습된 BERT 모델로 인텐트를 분류하는 독립 클래스."""

    def __init__(self, model_path: str = DEFAULT_MODEL_PATH, max_length: int = 64):
        self.max_length = max_length
        self.tokenizer  = AutoTokenizer.from_pretrained(model_path)
        self.model      = AutoModelForSequenceClassification.from_pretrained(model_path)
        self.model.eval()
        self.id2label = self.model.config.id2label
        print(f"모델 로드 완료: {model_path} ({len(self.id2label)}개 intent)")

    def predict(self, text: str, threshold: float = 0.70) -> dict:
        """
        텍스트 분류 후 결과 딕셔너리 반환.

        Returns:
            {
                "intent":     str,   # threshold 미만이면 "unknown"
                "confidence": float,
                "all_probs":  dict,  # intent → probability
            }
        """
        inputs = self.tokenizer(
            text,
            max_length=self.max_length,
            padding=True,
            truncation=True,
            return_tensors="pt",
        )
        with torch.no_grad():
            logits = self.model(**inputs).logits

        probs      = F.softmax(logits, dim=-1)[0]
        pred_id    = probs.argmax().item()
        confidence = probs[pred_id].item()
        intent     = self.id2label[pred_id]

        all_probs = {
            self.id2label[i]: round(p.item(), 3)
            for i, p in enumerate(probs)
        }

        return {
            "intent":     intent if confidence >= threshold else "unknown",
            "confidence": round(confidence, 3),
            "all_probs":  all_probs,
        }

    def batch_predict(self, texts: list[str], threshold: float = 0.70) -> list[dict]:
        return [self.predict(t, threshold) for t in texts]


# ── 테스트 ────────────────────────────────────────────────────────
def run_test(model_path: str):
    clf = BertIntentClassifier(model_path)

    test_cases = [
        # (질문, 예상 intent)
        ("어제 어떤 서버에 문제가 있었어?",           "incident_history"),
        ("web01 서버 정보 알려줘",                   "asset_info"),
        ("was01 CPU 사용률 보여줘",                  "metric_range"),
        ("에러 로그 보여줘",                          "multi_modal"),
        ("500 에러 왜 발생해?",                       "error_analysis"),
        ("OOM 해결 방법 알려줘",                      "action_recommend"),
        ("오늘 날씨 어때?",                           "unknown"),  # 도메인 외
    ]

    print("\n── 추론 테스트 ──")
    ok = 0
    for text, expected in test_cases:
        r = clf.predict(text)
        status = "OK" if r["intent"] == expected else "FAIL"
        if status == "OK":
            ok += 1
        print(f"{status} [{r['confidence']:.2f}] {text}")
        if status == "FAIL":
            print(f"     예상={expected}, 실제={r['intent']}")
        print(f"     확률분포: {r['all_probs']}")

    print(f"\n결과: {ok}/{len(test_cases)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL_PATH, help="모델 경로")
    args = parser.parse_args()
    run_test(args.model)
