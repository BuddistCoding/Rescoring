import torch
import torch.nn as nn
from torch.optim import AdamW
import logging
from transformers import BertTokenizer, BartForConditionalGeneration


class RoBart(nn.Module):
    def __init__(self, device, lr=1e-5):
        super().__init__(self)
        self.device = device

        self.tokenizer = BertTokenizer.from_pretrained("fnlp/bart-base-chinese")
        self.model = BartForConditionalGeneration.from_pretrained(
            "fnlp/bart-base-chinese"
        )

    def forward(self, input_id, seg, attention_masks, labels):

        loss = self.model(input_id, seg, attention_masks, labels=labels).loss

        return loss

    def recognize(self, input_id, seg, attention_masks, max_lens=50):
        output = self.model.generate(
            input_id,
            seg,
            attention_masks,
            max_length=max_lens,
        )
