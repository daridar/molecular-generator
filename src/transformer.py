import math
from typing import Union

import torch
import torch.nn as nn


class ScaledDotProductAttention(nn.Module):
    """Taken from https://github.com/CyberZHG/torch-multi-head-attention/"""

    def forward(self, query, key, value, mask=None):
        dk = query.size(-1)
        scores = query.matmul(key.transpose(-2, -1)) / math.sqrt(dk)

        if mask is not None:
            # First, we add very large negative number so the corresponding
            # attention weights will be close to zero
            scores = scores.masked_fill(mask == 0, -1e9)

        attention = torch.softmax(scores, dim=-1)

        if mask is not None:
            # And second, we zero all post-softmax values. This might be needed
            # since if a particular row doesn't contain any non-masked value,
            # this would result in all attention weights having the same
            # nonzero values, while we want them to be exactly zero.
            attention = attention.masked_fill(mask == 0, 0)

        return attention.matmul(value), attention


class MultiHeadAttention(nn.Module):
    """Taken from https://github.com/CyberZHG/torch-multi-head-attention/"""

    def __init__(self, in_features, head_num, bias=True):
        """Multi-head attention.
        :param in_features: Size of each input sample.
        :param head_num: Number of heads.
        :param bias: Whether to use the bias term.
        :param activation: The activation after each linear transformation.
        """
        super(MultiHeadAttention, self).__init__()
        if in_features % head_num != 0:
            raise ValueError(
                "`in_features`({}) should be divisible by `head_num`({})".format(
                    in_features, head_num
                )
            )
        self.in_features = in_features
        self.head_num = head_num
        self.bias = bias
        self.linear_q = nn.Linear(in_features, in_features, bias)
        self.linear_k = nn.Linear(in_features, in_features, bias)
        self.linear_v = nn.Linear(in_features, in_features, bias)
        self.linear_o = nn.Linear(in_features, in_features, bias)

    def forward(self, q, k, v, mask=None):
        q, k, v = self.linear_q(q), self.linear_k(k), self.linear_v(v)

        q = self._reshape_to_batches(q)
        k = self._reshape_to_batches(k)
        v = self._reshape_to_batches(v)

        if mask is not None:
            mask = mask.repeat(self.head_num, 1, 1)

        y, alignments = ScaledDotProductAttention()(q, k, v, mask)
        y = self._reshape_from_batches(y)
        y = self.linear_o(y)

        return y, self._reshape_alignments(alignments)

    @staticmethod
    def create_causal_mask(x=None, batch_size=None, seq_len=None, device=None):
        if x is None and batch_size is None and seq_len is None:
            raise ValueError(
                "Please provide either tensor or shape parameters."
            )
        if x is not None:
            batch_size, seq_len, dmodel = x.size()

        if device is None and x is None:
            raise ValueError("Please provide either tensor or device.")

        device = device or x.device

        return (
            torch.tril(torch.ones(seq_len, seq_len, device=device))
            .view(1, seq_len, seq_len)
            .repeat(batch_size, 1, 1)
        )

    def _reshape_alignments(self, alignments):
        batch_head, len1, len2 = alignments.size()
        batch_size = batch_head // self.head_num
        alignments = alignments.permute(1, 2, 0)
        alignments = alignments.reshape(len1, len2, batch_size, self.head_num)
        alignments = alignments.permute(2, 3, 0, 1)
        return alignments

    def _reshape_to_batches(self, x):
        batch_size, seq_len, in_feature = x.size()
        sub_dim = in_feature // self.head_num
        return (
            x.reshape(batch_size, seq_len, self.head_num, sub_dim)
            .permute(0, 2, 1, 3)
            .reshape(batch_size * self.head_num, seq_len, sub_dim)
        )

    def _reshape_from_batches(self, x):
        batch_size, seq_len, in_feature = x.size()
        batch_size //= self.head_num
        out_dim = in_feature * self.head_num
        return (
            x.reshape(batch_size, self.head_num, seq_len, in_feature)
            .permute(0, 2, 1, 3)
            .reshape(batch_size, seq_len, out_dim)
        )

    def extra_repr(self):
        return "in_features={}, head_num={}, bias={}, activation={}".format(
            self.in_features, self.head_num, self.bias, self.activation,
        )


class TransformerDecoderLayer(nn.Module):
    def __init__(
        self,
        dmodel,
        nhead,
        dim_feedforward=1024,
        dropout=0.1,
        n_conditional_channels=0,
    ):
        super().__init__()

        self.decoder_attention = MultiHeadAttention(dmodel, nhead)

        self.feedforward = nn.Sequential(
            nn.Linear(dmodel + n_conditional_channels, dim_feedforward),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(dim_feedforward, dmodel),
        )

        self.norm1 = nn.LayerNorm(dmodel)
        self.norm2 = nn.LayerNorm(dmodel)
        self.dropout1 = nn.Dropout(dropout)
        self.dropout2 = nn.Dropout(dropout)

    def forward(
        self,
        x,
        target_attention_mask,
        log_p,
    ):

        sequence_length = x.size(1)

        # tgt attention
        shortcut, tgt_alignments = self.decoder_attention(
            x, x, x, mask=target_attention_mask
        )
        x = x + self.dropout1(shortcut)
        x = self.norm1(x)

        ff_inps = [x]
        if log_p is not None:
            log_p = log_p.unsqueeze(1).repeat(1, sequence_length).unsqueeze(-1)
            ff_inps.append(log_p)

        ff_inps = torch.cat(ff_inps, dim=-1)

        shortcut = self.feedforward(ff_inps)

        # post
        x = x + self.dropout2(shortcut)
        x = self.norm2(x)

        return x, tgt_alignments


class SinusoidalPositionalEmbedding(nn.Module):
    def __init__(self, dmodel, num_positions):

        super().__init__()
        self.dmodel = dmodel
        self.num_positions = num_positions

    def forward(self, x):

        sequence_length = x.size(1)

        if sequence_length > self.num_positions:
            raise ValueError(
                "Provided tensor has length that is incompatible with "
                "the PositionalEmbedding layer. `sequence_length` is "
                "{sequence_length}, while the maximum position index is "
                "{maximum_position_index}.".format(
                    sequence_length=sequence_length,
                    maximum_position_index=self.num_positions,
                )
            )

        positions = torch.arange(sequence_length, device=x.device).float()
        positions.unsqueeze_(dim=1)
        freqs = torch.arange(self.dmodel, device=x.device).float()
        freqs.unsqueeze_(dim=0)

        sin = torch.sin(positions / 10000 ** (freqs[:, ::2] / self.dmodel))
        cos = torch.sin(positions / 10000 ** (freqs[:, 1::2] / self.dmodel))

        encodings = torch.cat([sin, cos], dim=1)
        encodings.unsqueeze_(dim=0)

        return x + encodings


@torch.no_grad()
def create_self_attention_mask(sequence_length, causal=False):

    device = sequence_length.device
    batch_size = len(sequence_length)
    max_sequence_length = sequence_length.max()

    r = (
        torch.arange(0, max_sequence_length, device=device)
        .unsqueeze(0)
        .repeat(batch_size, 1)
    )
    attention_mask = (r < sequence_length.unsqueeze(1)).float()
    attention_mask.unsqueeze_(1)
    attention_mask = attention_mask * attention_mask.transpose(1, 2)

    if causal:
        causal_mask = MultiHeadAttention.create_causal_mask(
            batch_size=batch_size, seq_len=max_sequence_length, device=device
        )
        attention_mask *= causal_mask

    return attention_mask


@torch.no_grad()
def create_attention_mask(src_sequence_length, tgt_sequence_length):

    device = src_sequence_length.device
    batch_size = len(src_sequence_length)

    src_max_sequence_length = src_sequence_length.max()
    r = (
        torch.arange(0, src_max_sequence_length, device=device)
        .unsqueeze(0)
        .repeat(batch_size, 1)
    )
    src_attention_mask = (r < src_sequence_length.unsqueeze(1)).float()
    src_attention_mask.unsqueeze_(1)

    tgt_max_sequence_length = tgt_sequence_length.max()
    r = (
        torch.arange(0, tgt_max_sequence_length, device=device)
        .unsqueeze(0)
        .repeat(batch_size, 1)
    )
    tgt_attention_mask = (r < tgt_sequence_length.unsqueeze(1)).float()
    tgt_attention_mask.unsqueeze_(1)

    attention_mask = src_attention_mask * tgt_attention_mask.transpose(1, 2)

    return attention_mask


class Transformer(nn.Module):
    def __init__(
        self,
        vocab_size,
        dmodel,
        nhead,
        decoder_layers,
        dim_feedforward=1024,
        dropout=0.1,
        num_positions=1024,
        n_conditional_channels=0,
    ):
        super().__init__()

        self.dmodel = dmodel

        self.embedding = nn.Embedding(
            num_embeddings=vocab_size, embedding_dim=dmodel
        )

        self.positional_encoding = SinusoidalPositionalEmbedding(
            dmodel, num_positions
        )

        self.decoder_layers = nn.ModuleList(
            [
                TransformerDecoderLayer(
                    dmodel,
                    nhead,
                    dim_feedforward=dim_feedforward,
                    dropout=dropout,
                    n_conditional_channels=n_conditional_channels,
                )
                for _ in range(decoder_layers)
            ]
        )

        self.classifier = nn.Linear(dmodel, vocab_size)

    def _decode(
        self,
        x,
        target_attention_mask,
        log_p=None,
    ):

        decoder_alignments = []
        decoder_encoder_alignments = []
        for decoder_layer in self.decoder_layers:
            x, decoder_alignment = decoder_layer(
                x,
                target_attention_mask,
                log_p,
            )
            decoder_alignments.append(decoder_alignment)

        return x, decoder_alignments

    def _encode_output_ids(self, output_ids):
        target_embeddings = self.embedding(output_ids)
        target_embeddings = self.positional_encoding(target_embeddings)

        return target_embeddings

    def forward(
        self,
        output_ids,
        target_sequence_length,
        log_p=None,
    ):

        batch_size = output_ids.size(0)

        target_embeddings = self._encode_output_ids(output_ids)

        decoder_mask = create_self_attention_mask(
            target_sequence_length, causal=True
        )

        (decoded, decoder_alignments,) = self._decode(
            target_embeddings,
            target_attention_mask=decoder_mask,
            log_p=log_p,
        )

        logits = self.classifier(decoded)

        return dict(
            logits=logits,
            decoder_alignments=decoder_alignments,
            target_embeddings=target_embeddings,
        )

    @torch.no_grad()
    def generate(
        self,
        batch_size,
        max_target_sequence_length,
        start_id,
        device,
        temperature=1.0,
        mask_ids=(),
        log_p=None,
    ):

        output_ids = torch.zeros(batch_size, device=device).long()
        output_ids.fill_(start_id).unsqueeze_(1)

        for step in range(1, max_target_sequence_length):

            target_sequence_length = torch.zeros(
                batch_size, device=device
            ).long()
            target_sequence_length.fill_(step)

            decoder_mask = create_self_attention_mask(
                target_sequence_length, causal=True
            )

            target_embeddings = self._encode_output_ids(output_ids)

            (decoded, decoder_alignments,) = self._decode(
                target_embeddings,
                target_attention_mask=decoder_mask,
                log_p=log_p,
            )

            last_decoded = decoded[:, -1, :]
            last_logits = self.classifier(
                last_decoded
            )  # [b_s, (seq_len==1), vocab_size]

            for mask_id in mask_ids:
                last_logits[:, mask_id] = -1e9

            probs = torch.softmax(last_logits / temperature, dim=1)
            choices = torch.multinomial(probs, num_samples=1)

            output_ids = torch.cat([output_ids, choices], dim=1)

        return dict(
            output_ids=output_ids,
        )
