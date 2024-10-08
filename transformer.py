"""
https://arxiv.org/pdf/1706.03762
"""
import math

import torch
import torch.nn as nn

neg_inf = float('-inf')

# TODO: I am pretty sure I've forgot to normalize or scale something somewhere


def create_causal_mask(seq_len):
    return torch.tril(torch.ones(seq_len, seq_len), diagonal=0).type(torch.uint8)  # (seq_len, seq_len)


def positional_encoding(seq_len, embed_size):
    pos_vec = torch.arange(seq_len).unsqueeze(1)  # (seq_len, 1)
    i_times_two_vec = torch.arange(0, embed_size, 2)  # (embed_size // 2)
    pos_encoding = torch.empty(seq_len, embed_size)
    div_term = torch.exp(-math.log(10000) * i_times_two_vec / embed_size)
    pos_encoding[:, ::2] = torch.sin(pos_vec * div_term)
    pos_encoding[:, 1::2] = torch.cos(pos_vec * div_term)
    return pos_encoding


class ScaledDotProductAttention(nn.Module):
    def __init__(self):
        super().__init__()
        self.dropout = nn.Dropout(p=0.1)

    def forward(self, q, k, v, mask=None):
        """
        q - (batch_size, n_heads, seq_len_q, embed_size)
        k - (batch_size, n_heads, seq_len_kv, embed_size)
        v - (batch_size, n_heads, seq_len_kv, embed_size)
        mask - (batch_size or 1, seq_len_q or 1 (for broadcasting), seq_len_kv)
        """
        d_k = k.size(-1)
        attn_weights = q @ k.transpose(-1, -2) / d_k ** 0.5  # (batch_size, n_heads, seq_len_q, seq_len_kv)
        if mask is not None:
            attn_weights = attn_weights.masked_fill(mask[:, torch.newaxis] == 0, neg_inf)
        attn_weights = torch.softmax(attn_weights, dim=-1)
        attn_weights = self.dropout(attn_weights)  # AIAYN didn't ask for this
        return attn_weights @ v  # (batch_size, n_heads, seq_len_kv, d_v)


class MultiHeadAttention(nn.Module):
    def __init__(self, n_heads, embed_size):
        super().__init__()
        assert embed_size % n_heads == 0

        self.n_heads = n_heads
        self.embed_size = embed_size
        self.head_dim = embed_size // n_heads

        self.proj = nn.Linear(embed_size, embed_size)
        self.q_weights = nn.Linear(embed_size, embed_size)
        self.k_weights = nn.Linear(embed_size, embed_size)
        self.v_weights = nn.Linear(embed_size, embed_size)

        self.sdpa = ScaledDotProductAttention()

    def forward(self, q, k, v, mask=None):
        """
        q - (batch_size, seq_len_q, embed_size)
        k - (batch_size, seq_len_kv, embed_size)
        v - (batch_size, seq_len_kv, embed_size)
        mask - (batch_size, seq_len_qkv)
        """
        batch_size = q.size(0)
        seq_len_q = q.size(1)

        q = self.q_weights(q)
        k = self.k_weights(k)
        v = self.v_weights(v)

        q = q.view(batch_size, self.n_heads, q.size(1), self.head_dim)
        k = k.view(batch_size, self.n_heads, k.size(1), self.head_dim)
        v = v.view(batch_size, self.n_heads, v.size(1), self.head_dim)

        attentions = self.sdpa(q, k, v, mask=mask)  # (batch_size, n_heads, seq_len_v, head_dim)
        # .contiguous() often used here
        return self.proj(attentions.view(batch_size, seq_len_q, self.embed_size))  # (batch_size, seq_len_q, embed_size)


class FeedForward(nn.Module):
    def __init__(self, d_out, d_ff):
        super().__init__()
        self.ff = nn.Sequential(
            nn.Linear(d_out, d_ff),
            nn.ReLU(),
            nn.Dropout(p=0.1),  # AIAYN didn't ask for this
            nn.Linear(d_ff, d_out)
        )

    def forward(self, x):
        return self.ff(x)


class EncoderBlock(nn.Module):
    def __init__(self, n_heads, embed_size, d_ff):
        super().__init__()
        self.ff = FeedForward(embed_size, d_ff)
        self.mha = MultiHeadAttention(n_heads, embed_size)
        self.ln1 = nn.LayerNorm(embed_size)
        self.ln2 = nn.LayerNorm(embed_size)
        self.dropout = nn.Dropout(p=0.1)

    def forward(self, x, mask=None):
        attention = self.ln1(self.dropout(self.mha(x, x, x, mask)) + x)  # (batch_size, seq_len, embed_size)
        return self.ln2(self.dropout(self.ff(attention)) + attention)  # (batch_size, seq_len, embed_size)


class Encoder(nn.Module):
    def __init__(self, n_blocks, n_heads, embed_size, d_ff):
        super().__init__()
        self.encoder_blocks = nn.ModuleList([
            EncoderBlock(n_heads, embed_size, d_ff) for _ in range(n_blocks)
        ])

    def forward(self, x, mask=None):
        # mask - (batch_size, 1, seq_len)
        for encoder_block in self.encoder_blocks:
            x = encoder_block(x, mask)
        return x


class DecoderBlock(nn.Module):
    def __init__(self, n_heads, embed_size, d_ff):
        super().__init__()
        self.ff = FeedForward(embed_size, d_ff)
        self.mha = MultiHeadAttention(n_heads, embed_size)
        self.mmha = MultiHeadAttention(n_heads, embed_size)
        self.ln1 = nn.LayerNorm(embed_size)
        self.ln2 = nn.LayerNorm(embed_size)
        self.ln3 = nn.LayerNorm(embed_size)
        self.dropout = nn.Dropout(p=0.1)

    def forward(self, x, memory, causal_mask, encoder_mask=None):
        # x - (batch_size, seq_len, embed_size)
        attention = self.ln1(self.dropout(self.mmha(x, x, x, causal_mask)) + x)
        attention = self.ln2(self.dropout(self.mha(attention, memory, memory, mask=encoder_mask)) + attention)
        return self.ln3(self.dropout(self.ff(attention)) + attention)


class Decoder(nn.Module):
    def __init__(self, n_blocks, n_heads, embed_size, d_ff):
        super().__init__()
        self.decoder_blocks = nn.ModuleList([
            DecoderBlock(n_heads, embed_size, d_ff) for _ in range(n_blocks)
        ])

    def forward(self, x, memory, encoder_mask=None):
        # encoder_mask - (batch_size, 1, seq_len)
        causal_mask = create_causal_mask(x.size(1))[torch.newaxis, torch.newaxis].to(x.device)  # (1, 1, seq_len_tgt, seq_len_tgt)
        for decoder_block in self.decoder_blocks:
            x = decoder_block(x, memory, causal_mask, encoder_mask=encoder_mask)
        return x


class Transformer(nn.Module):
    def __init__(
        self,
        vocab_size,
        n_encoder_blocks=6,
        n_decoder_block=6,
        n_encoder_heads=8,
        n_decoder_heads=8,
        embed_size=512,
        d_ff=2048,
        max_len=4096,
    ):
        super().__init__()
        self.pos_enc = positional_encoding(max_len, embed_size)
        self.encoder = Encoder(n_encoder_blocks, n_encoder_heads, embed_size, d_ff)
        self.decoder = Decoder(n_decoder_block, n_decoder_heads, embed_size, d_ff)
        self.embedding = nn.Embedding(vocab_size, embed_size)
        self.proj = nn.Linear(embed_size, vocab_size)

        self.dropout = nn.Dropout(p=0.1)

    def forward(self, src, tgt, src_mask=None):
        """
        src - (batch_size, seq_len_src)
        tgt - (batch_size, seq_len_tgt)
        src_mask - (batch_size, seq_len_src)
        """
        if src_mask is not None:
            src_mask = src_mask[:, torch.newaxis]  # (batch_size, 1, seq_len_src)
        src_embed = self.embedding(src)  # (batch_size, seq_len_src, embed_size)
        src_embed += self.pos_enc[:src.size(1)].to(src_embed.device)
        src_embed = self.dropout(src_embed)
        memory = self.encoder(src_embed, mask=src_mask)  # (batch_size, seq_len_src, embed_size)
        tgt_embed = self.embedding(tgt)  # (batch_size, seq_len_tgt, embed_size)
        tgt_embed += self.pos_enc[:tgt.size(1)].to(tgt_embed.device)
        tgt_embed = self.dropout(tgt_embed)
        attention = self.decoder(tgt_embed, memory, encoder_mask=src_mask)  # (batch_size, seq_len_tgt, embed_size)
        return self.proj(attention)  # (batch_size, seq_len_tgt, vocab_size)

    @torch.no_grad
    def generate(self, src, start_token, eos_token, max_tokens=20):
        if src.dim() == 1:
            src = src.unsqueeze(0)
        elif src.size(0) != 1:
            raise ValueError("batch_size > 1 is not supported...")
        memory = self.encoder(self.embedding(src))
        generated = [start_token]

        for _ in range(max_tokens):
            embeds = self.embedding(torch.tensor([generated]))
            attention = self.decoder(embeds, memory)
            logits = self.proj(attention)
            generated_token = logits[:, -1].argmax()

            if generated_token == eos_token:
                break

            generated.append(generated_token.item())
        return torch.tensor(generated)
