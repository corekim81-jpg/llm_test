"""
nlp/bert/train.py — BERT 파인튜닝 학습 스크립트
─────────────────────────────────────────────────
실행 방법 (monitoring_llm/ 부모 디렉토리에서):
    python -m monitoring_llm.nlp.bert.train

또는 nlp/bert/ 디렉토리에서:
    python train.py

학습 완료 후 .env 설정:
    BERT_MODEL_PATH=./nlp/bert/intent_model
"""

import os
import sys

# nlp/bert/ → nlp/ → monitoring_llm/ → llm_test/  (3단계 위 = 패키지 루트)
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "../../.."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import numpy as np
import torch
from sklearn.metrics import classification_report
from sklearn.model_selection import train_test_split
from transformers import (
    AutoModelForSequenceClassification,
    AutoTokenizer,
    EarlyStoppingCallback,
    Trainer,
    TrainingArguments,
)

# ── 경로 설정 (패키지 외부에서도 실행 가능하도록) ─────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "../../.."))

from monitoring_llm.nlp.bert.dataset import INTENT_LABELS, TRAIN_DATA, id2label, label2id
from monitoring_llm.nlp.bert.dataset_class import IntentDataset

# ── 학습 설정 ─────────────────────────────────────────────────────
# 한국어 고성능 모델 우선순위:
#   1. snunlp/KR-ELECTRA-discriminator  (ELECTRA, 고성능)
#   2. klue/roberta-base                (RoBERTa, 범용)
#   3. monologg/koelectra-base-v3-discriminator (ELECTRA 경량)
MODEL_NAME = os.getenv("BERT_PRETRAIN_MODEL", "snunlp/KR-ELECTRA-discriminator")
SAVE_PATH  = os.getenv("BERT_MODEL_PATH",     os.path.join(_HERE, "intent_model"))
MAX_LENGTH = int(os.getenv("BERT_MAX_LENGTH", "64"))    # 모니터링 쿼리는 짧으므로 64 충분
BATCH_SIZE = int(os.getenv("BERT_BATCH_SIZE", "16"))
EPOCHS     = int(os.getenv("BERT_EPOCHS",     "20"))    # 데이터 적으므로 충분히 학습
TEST_RATIO = float(os.getenv("BERT_TEST_RATIO", "0.15"))  # 검증 비율 줄여 학습 데이터 확보


def main():
    print(f"사전학습 모델: {MODEL_NAME}")
    print(f"저장 경로    : {SAVE_PATH}")
    print(f"GPU 사용     : {torch.cuda.is_available()}")
    print(f"학습 데이터  : {len(TRAIN_DATA)}개")
    print()

    # ── 토크나이저 & 모델 ──────────────────────────────────────────
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    model     = AutoModelForSequenceClassification.from_pretrained(
        MODEL_NAME,
        num_labels=len(INTENT_LABELS),
        id2label=id2label,
        label2id=label2id,
    )

    # ── 데이터 분할 (클래스 균등 분할) ────────────────────────────
    intents = [d["intent"] for d in TRAIN_DATA]
    train_data, val_data = train_test_split(
        TRAIN_DATA,
        test_size=TEST_RATIO,
        random_state=42,
        stratify=intents,
    )

    train_ds = IntentDataset(train_data, tokenizer, label2id, MAX_LENGTH)
    val_ds   = IntentDataset(val_data,   tokenizer, label2id, MAX_LENGTH)
    print(f"학습: {len(train_ds)}개 | 검증: {len(val_ds)}개")

    # ── 평가 함수 ──────────────────────────────────────────────────
    def compute_metrics(eval_pred):
        logits, labels = eval_pred
        preds    = np.argmax(logits, axis=-1)
        accuracy = (preds == labels).mean()
        return {"accuracy": float(accuracy)}

    # ── 학습 설정 ──────────────────────────────────────────────────
    args = TrainingArguments(
        output_dir=SAVE_PATH,
        num_train_epochs=EPOCHS,
        per_device_train_batch_size=BATCH_SIZE,
        per_device_eval_batch_size=BATCH_SIZE,
        eval_strategy="epoch",
        save_strategy="epoch",
        load_best_model_at_end=True,
        metric_for_best_model="accuracy",
        greater_is_better=True,
        learning_rate=2e-5,             # 소규모 데이터에는 낮은 lr 안정적
        warmup_ratio=0.15,              # warmup 비율 올려 초기 발산 방지
        weight_decay=0.01,
        logging_steps=5,
        fp16=torch.cuda.is_available(),
        report_to="none",               # wandb/tensorboard 비활성화
        save_safetensors=False,         # ELECTRA non-contiguous tensor 저장 오류 방지
        save_total_limit=2,             # 체크포인트 최대 2개만 유지
    )

    trainer = Trainer(
        model=model,
        args=args,
        train_dataset=train_ds,
        eval_dataset=val_ds,
        compute_metrics=compute_metrics,
        callbacks=[EarlyStoppingCallback(early_stopping_patience=5)],  # patience 늘림
    )

    # ── 학습 실행 ──────────────────────────────────────────────────
    print("\n── 학습 시작 ──")
    trainer.train()

    # ── 모델 저장 ──────────────────────────────────────────────────
    trainer.save_model(SAVE_PATH)
    tokenizer.save_pretrained(SAVE_PATH)
    print(f"\n모델 저장 완료: {SAVE_PATH}")

    # ── 검증 세트 상세 리포트 ──────────────────────────────────────
    pred_out    = trainer.predict(val_ds)
    pred_labels = np.argmax(pred_out.predictions, axis=-1)
    true_labels = [label2id[d["intent"]] for d in val_data]

    print("\n── Classification Report ──")
    print(classification_report(true_labels, pred_labels, target_names=INTENT_LABELS))

    print("\n.env 설정 추가:")
    print(f"  BERT_MODEL_PATH={SAVE_PATH}")


if __name__ == "__main__":
    main()
