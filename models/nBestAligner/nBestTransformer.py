from numpy import place
import torch
import torch.nn as nn
from torch.optim import AdamW
import logging
from transformers import (
    BertModel,
    BertTokenizer,
    BertGenerationEncoder,
    BertGenerationDecoder,
    BertGenerationTokenizer,
    EncoderDecoderModel,
    BertConfig,
    EncoderDecoderConfig,
    BartTokenizer,
    BartForConditionalGeneration,
)
from torch.nn.utils.rnn import pad_sequence
from nBestAligner.nBestAlign import align, alignNbest


class nBestTransformer(nn.Module):
    def __init__(
        self,
        nBest,
        train_batch,
        test_batch,
        device,
        lr=1e-5,
        mode="align",
        model_name="bart",
        align_embedding=512,
    ):
        nn.Module.__init__(self)
        self.device = device
        self.embedding_dim = align_embedding

        if model_name == "bert":
            self.tokenizer = BertTokenizer.from_pretrained("bert-base-chinese")

            encoder = BertGenerationEncoder.from_pretrained(
                "bert-base-chinese", bos_token_id=101, eos_token_id=102
            )

            decoder = BertGenerationDecoder.from_pretrained(
                "bert-base-chinese",
                add_cross_attention=True,
                is_decoder=True,
                bos_token_id=101,
                eos_token_id=102,
            )

            self.model = EncoderDecoderModel(encoder=encoder, decoder=decoder).to(
                self.device
            )
        else:
            self.tokenizer = BertTokenizer.from_pretrained("fnlp/bart-base-chinese")

            self.model = BartForConditionalGeneration.from_pretrained(
                "fnlp/bart-base-chinese"
            ).to(self.device)

        self.nBest = nBest
        self.train_batch = train_batch
        self.test_batch = test_batch
        self.mode = mode

        self.vocab_size = self.tokenizer.vocab_size
        self.pad = self.tokenizer.convert_tokens_to_ids("[PAD]")

        self.model.config.decoder_start_token_id = self.tokenizer.convert_tokens_to_ids(
            "[CLS]"
        )
        self.model.config.pad_token_id = self.tokenizer.convert_tokens_to_ids("[PAD]")

        logging.warning(self.model.config)

        self.embedding = nn.Embedding(
            self.tokenizer.vocab_size, align_embedding, padding_idx=self.pad
        ).to(self.device)
        self.embeddingLinear = (
            nn.Linear(align_embedding * self.nBest, 768).to(self.device)
            if self.mode == "align"
            else None
        )

        parameters = list(self.embedding.parameters()) + list(self.model.parameters())
        if self.mode == "align":
            parameters += list(self.embeddingLinear.parameters())
        self.optimizer = AdamW(parameters, lr=lr)

    def forward(
        self,
        input_id,
        attention_mask,
        labels,
    ):
        if self.mode == "align":

            aligned_embedding = self.embedding(input_id)  # (L, N, 768)
            aligned_embedding = aligned_embedding.flatten(start_dim=2)  # (L, 768 * N)
            proj_embedding = self.embeddingLinear(
                aligned_embedding
            )  # (L, 768 * N) -> (L, 768)

            labels[labels == 0] = -100

            loss = self.model(
                inputs_embeds=proj_embedding,
                attention_mask=attention_mask,
                labels=labels,
            ).loss

            return loss

        elif self.mode == "plain":
            assert (
                attention_mask is not None
            ), "Attention Mask will not be produced during plain mode forward"

            loss = self.model(
                input_ids=input_id, attention_mask=attention_mask, labels=labels
            )

            return loss

    def recognize(
        self,
        input_id,
        attention_mask,
        decoder_ids,
        max_lens,
    ):
        # input_id : (B, L, N)
        if self.mode == "align":
            batch = input_id.shape[0]

            input_id = input_id.view(-1, self.nBest)
            aligned_embedding = self.embedding(input_id)  # (L, N, 768)
            aligned_embedding = aligned_embedding.view(
                batch, -1, self.nBest, self.embedding_dim
            )
            aligned_embedding = aligned_embedding.flatten(start_dim=2)  # (L, 768 * N)
            proj_embedding = self.embeddingLinear(
                aligned_embedding
            )  # (L, 768 * N) -> (L, 768)

            output = self.model.generate(
                inputs_embeds=proj_embedding,
                attention_mask=attention_mask,
                decoder_input_ids=decoder_ids,
                max_length=max_lens,
            )

            return output
