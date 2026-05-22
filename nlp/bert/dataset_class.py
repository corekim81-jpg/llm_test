"""
nlp/bert/dataset_class.py — PyTorch Dataset 래퍼
"""

import torch
from torch.utils.data import Dataset
from transformers import PreTrainedTokenizerBase


class IntentDataset(Dataset):
    def __init__(
        self,
        data: list[dict],
        tokenizer: PreTrainedTokenizerBase,
        label2id: dict[str, int],
        max_length: int = 64,
    ):
        self.data       = data
        self.tokenizer  = tokenizer
        self.label2id   = label2id
        self.max_length = max_length

    def __len__(self) -> int:
        return len(self.data)

    def __getitem__(self, idx: int) -> dict:
        item = self.data[idx]
        enc  = self.tokenizer(
            item["text"],
            max_length=self.max_length,
            padding="max_length",
            truncation=True,
            return_tensors="pt",
        )
        return {
            "input_ids":      enc["input_ids"].squeeze(),
            "attention_mask": enc["attention_mask"].squeeze(),
            "labels":         torch.tensor(self.label2id[item["intent"]], dtype=torch.long),
        }
