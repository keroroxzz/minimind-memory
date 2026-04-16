import math, torch, torch.nn.functional as F
from torch import nn
from transformers.activations import ACT2FN
from transformers import PreTrainedModel, GenerationMixin, PretrainedConfig
from transformers.modeling_outputs import MoeCausalLMOutputWithPast
from dataclasses import dataclass, field
from typing import List, Optional

# 🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏
#                                     MiniMind Config
# 🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏
class MiniMindConfig(PretrainedConfig):
    model_type = "minimind"
    def __init__(self, hidden_size=768, num_hidden_layers=8, use_moe=False, **kwargs):
        super().__init__(**kwargs)
        self.hidden_size = hidden_size
        self.num_hidden_layers = num_hidden_layers
        self.use_moe = use_moe
        self.dropout = kwargs.get("dropout", 0.0)
        self.vocab_size = kwargs.get("vocab_size", 6400)
        self.bos_token_id = kwargs.get("bos_token_id", 1)
        self.eos_token_id = kwargs.get("eos_token_id", 2)
        self.flash_attn = kwargs.get("flash_attn", True)
        self.num_attention_heads = kwargs.get("num_attention_heads", 8)
        self.num_key_value_heads = kwargs.get("num_key_value_heads", 4)
        self.head_dim = kwargs.get("head_dim", self.hidden_size // self.num_attention_heads)
        self.hidden_act = kwargs.get("hidden_act", 'silu')
        self.intermediate_size = kwargs.get("intermediate_size", math.ceil(hidden_size * math.pi / 64) * 64)
        self.max_position_embeddings = kwargs.get("max_position_embeddings", 32768)
        self.rms_norm_eps = kwargs.get("rms_norm_eps", 1e-6)
        self.rope_theta = kwargs.get("rope_theta", 1e6)
        self.inference_rope_scaling = kwargs.get("inference_rope_scaling", False)
        self.rope_scaling = {
            "beta_fast": 32,
            "beta_slow": 1,
            "factor": 16,
            "original_max_position_embeddings": 2048,
            "attention_factor": 1.0,
            "type": "yarn"
        } if self.inference_rope_scaling else None
        ### MoE specific configs (ignored if use_moe = False)
        self.num_experts = kwargs.get("num_experts", 4)
        self.num_experts_per_tok = kwargs.get("num_experts_per_tok", 1)
        self.moe_intermediate_size = kwargs.get("moe_intermediate_size", self.intermediate_size)
        self.norm_topk_prob = kwargs.get("norm_topk_prob", True)
        self.router_aux_loss_coef = kwargs.get("router_aux_loss_coef", 5e-4)

        # --- DeepSeek Engram 新增參數 ---
        self.use_engram: bool = kwargs.get("use_engram", True)
        self.engram_offload_cpu: bool = kwargs.get("engram_offload_cpu", True) # 是否將 Embedding 表放在 CPU
        self.max_ngram_size: int = kwargs.get("max_ngram_size", 3)
        self.engram_vocab_size: int = kwargs.get("engram_vocab_size", 1024 * 1024) # Unified table size (e.g. 1M)
        self.n_embed_per_ngram: int = kwargs.get("n_embed_per_ngram", 768)
        self.n_head_per_ngram: int = kwargs.get("n_head_per_ngram", 8)
        self.engram_layers: List[int] = kwargs.get("engram_layers", [2, 4, 6]) # 作用層數
        self.engram_kernel_size: int = kwargs.get("engram_kernel_size", 4)

        # --- Dense & Latent Attention 新增參數 ---
        self.use_dense_attention: bool = kwargs.get("use_dense_attention", False)
        self.use_latent_attention: bool = kwargs.get("use_latent_attention", False)
        self.kv_lora_rank: int = kwargs.get("kv_lora_rank", 128)
        self.qk_rope_dim: int = kwargs.get("qk_rope_dim", 64)


# 🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏
#                   Engram Module (from https://github.com/deepseek-ai/Engram/tree/main)
# 🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏
class MultiHeadEmbedding(nn.Module):
    def __init__(self, list_of_N: List[int], D: int):
        super().__init__()
        self.num_heads = len(list_of_N)
        self.embedding_dim = D
        offsets = [0]
        for n in list_of_N[:-1]:
            offsets.append(offsets[-1] + n)
        self.register_buffer("offsets", torch.tensor(offsets, dtype=torch.long), persistent=False)
        total_N = sum(list_of_N)
        self.embedding = nn.Embedding(num_embeddings=total_N, embedding_dim=D)

    def forward(self, input_ids: torch.Tensor) -> torch.Tensor:
        # input_ids: [..., num_heads]
        # Ensure offsets are on same device as input_ids
        shifted_input_ids = input_ids + self.offsets.to(input_ids.device)
        output = self.embedding(shifted_input_ids)
        return output

class ShortConv(nn.Module):
    def __init__(self, hidden_size: int, kernel_size: int = 4, dilation: int = 1):
        super().__init__()
        self.conv = nn.Conv1d(
            in_channels=hidden_size,
            out_channels=hidden_size,
            kernel_size=kernel_size,
            groups=hidden_size,
            bias=False,
            padding=(kernel_size - 1) * dilation,
            dilation=dilation,
        )
        self.act_fn = nn.SiLU()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: [B, L, D]
        T = x.shape[1]
        x_bct = x.transpose(1, 2)
        y_bct = self.conv(x_bct)
        y_bct = y_bct[..., :T]
        y = self.act_fn(y_bct).transpose(1, 2)
        return y

def is_prime(n):
    if n < 2: return False
    for i in range(2, int(math.sqrt(n)) + 1):
        if n % i == 0: return False
    return True

def find_next_prime(start, seen_primes):
    candidate = start + 1
    while True:
        if is_prime(candidate) and candidate not in seen_primes:
            return candidate
        candidate += 1

class EngramManager(nn.Module):
    def __init__(self, config: MiniMindConfig):
        super().__init__()
        self.max_ngram_size = config.max_ngram_size
        self.n_head_per_ngram = config.n_head_per_ngram
        self.hidden_size = config.hidden_size
        self.n_embed_per_ngram = config.n_embed_per_ngram
        self.layer_ids = config.engram_layers
        self.offload_cpu = config.engram_offload_cpu
        self.total_heads = (self.max_ngram_size - 1) * self.n_head_per_ngram

        # 1. 為每個 head 生成質數模數 (Unified Table)
        self.head_vocab_sizes = []
        seen_primes = set()
        curr_start = config.engram_vocab_size // self.total_heads
        for _ in range(self.total_heads):
            p = find_next_prime(curr_start, seen_primes)
            seen_primes.add(p)
            self.head_vocab_sizes.append(p)
            curr_start = p

        # 2. Embedding 表 (可以在 CPU 或 GPU)
        self.embedding_table = MultiHeadEmbedding(self.head_vocab_sizes, D=self.n_embed_per_ngram // self.n_head_per_ngram)
        if self.offload_cpu:
            self.embedding_table = self.embedding_table.cpu()

        # 3. GPU 上的計算組件 (Gating, Conv, Norm)
        self.register_buffer("multipliers", torch.tensor([31, 10007, 424243], dtype=torch.long))
        
        # 為了支援多層，每層可以有自己的 gating 參數，或者共用。這裡每層獨立，效能更好。
        self.fusions = nn.ModuleDict({
            str(layer_id): nn.ModuleDict({
                "value_proj": nn.Linear((self.max_ngram_size - 1) * self.n_embed_per_ngram, self.hidden_size, bias=False),
                "key_proj": nn.Linear((self.max_ngram_size - 1) * self.n_embed_per_ngram, self.hidden_size, bias=False),
                "short_conv": ShortConv(self.hidden_size, kernel_size=config.engram_kernel_size, dilation=self.max_ngram_size),
                "norm1": RMSNorm(self.hidden_size),
                "norm2": RMSNorm(self.hidden_size)
            }) for layer_id in self.layer_ids
        })

    def _apply(self, fn):
        super()._apply(fn)
        if self.offload_cpu:
            self.embedding_table.cpu()
        return self

    def get_hashes(self, full_input_ids: torch.Tensor, L_curr: int):
        B, L_full = full_input_ids.shape
        device = full_input_ids.device
        
        all_hashes = []
        shifts = []
        for k in range(self.max_ngram_size):
            if k == 0:
                shifts.append(full_input_ids)
            else:
                s = torch.cat([torch.zeros((B, k), dtype=torch.long, device=device), full_input_ids[:, :-k]], dim=1)
                shifts.append(s)

        h_idx = 0
        for n in range(2, self.max_ngram_size + 1):
            mix = (shifts[0] * self.multipliers[0])
            for k in range(1, n):
                mix = mix ^ (shifts[k] * self.multipliers[k])
            
            for _ in range(self.n_head_per_ngram):
                all_hashes.append(mix % self.head_vocab_sizes[h_idx])
                h_idx += 1
        
        # [B, L_full, num_total_heads]
        hash_ids = torch.stack(all_hashes, dim=-1)
        # 只取目前需要的 token 部分
        return hash_ids[:, -L_curr:, :]

    def stage1_gather(self, full_input_ids: torch.Tensor, L_curr: int):
        # 1. 計算哈希 (GPU)
        hash_ids = self.get_hashes(full_input_ids, L_curr)
        
        # 2. 查表 (如果 offload，則需要將 hash_ids 轉到 CPU)
        if self.offload_cpu:
            cpu_hash_ids = hash_ids.cpu()
            # 獲取嵌入 (CPU)
            embeddings = self.embedding_table(cpu_hash_ids)
            # 傳回 GPU
            embeddings = embeddings.to(full_input_ids.device)
        else:
            embeddings = self.embedding_table(hash_ids)
            
        # 展平嵌入 [B, L, total_heads * head_dim]
        embeddings = embeddings.flatten(start_dim=-2)
        return embeddings

    def stage2_fusion(self, layer_id: int, hidden_states: torch.Tensor, engram_features: torch.Tensor):
        f = self.fusions[str(layer_id)]
        
        # Cross-Attention Gating
        key = f["norm1"](f["key_proj"](engram_features))
        query = f["norm2"](hidden_states)
        
        gate = (key * query).sum(dim=-1, keepdim=True) / math.sqrt(self.hidden_size)
        gate = gate.abs().clamp_min(1e-6).sqrt() * gate.sign()
        gate = torch.sigmoid(gate)

        value = gate * f["value_proj"](engram_features)
        output = value + f["short_conv"](value)
        return output

# 🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏
#                                     MiniMind Model
# 🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏

# 🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏
#                                     MiniMind Model
# 🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏
class RMSNorm(torch.nn.Module):
    def __init__(self, dim: int, eps: float = 1e-5):
        super().__init__()
        self.eps = eps
        self.weight = nn.Parameter(torch.ones(dim))

    def norm(self, x):
        return x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + self.eps)

    def forward(self, x):
        return (self.weight * self.norm(x.float())).type_as(x)

def precompute_freqs_cis(dim: int, end: int = int(32 * 1024), rope_base: float = 1e6, rope_scaling: dict = None):
    freqs, attn_factor = 1.0 / (rope_base ** (torch.arange(0, dim, 2)[: (dim // 2)].float() / dim)), 1.0
    if rope_scaling is not None: # YaRN: f'(i) = f(i)((1-γ) + γ/s), where γ∈[0,1] is linear ramp
        orig_max, factor, beta_fast, beta_slow, attn_factor = (
            rope_scaling.get("original_max_position_embeddings", 2048), rope_scaling.get("factor", 16),
            rope_scaling.get("beta_fast", 32.0), rope_scaling.get("beta_slow", 1.0), rope_scaling.get("attention_factor", 1.0)
        )
        if end / orig_max > 1.0:
            inv_dim = lambda b: (dim * math.log(orig_max / (b * 2 * math.pi))) / (2 * math.log(rope_base))
            low, high = max(math.floor(inv_dim(beta_fast)), 0), min(math.ceil(inv_dim(beta_slow)), dim // 2 - 1)
            ramp = torch.clamp((torch.arange(dim // 2, device=freqs.device).float() - low) / max(high - low, 0.001), 0, 1)
            freqs = freqs * (1 - ramp + ramp / factor)
    t = torch.arange(end, device=freqs.device)
    freqs = torch.outer(t, freqs).float()
    freqs_cos = torch.cat([torch.cos(freqs), torch.cos(freqs)], dim=-1) * attn_factor
    freqs_sin = torch.cat([torch.sin(freqs), torch.sin(freqs)], dim=-1) * attn_factor
    return freqs_cos, freqs_sin

def apply_rotary_pos_emb(q, k, cos, sin, unsqueeze_dim=1):
    def rotate_half(x): return torch.cat((-x[..., x.shape[-1] // 2:], x[..., : x.shape[-1] // 2]), dim=-1)
    q_embed = ((q * cos.unsqueeze(unsqueeze_dim)) + (rotate_half(q) * sin.unsqueeze(unsqueeze_dim))).to(q.dtype)
    k_embed = ((k * cos.unsqueeze(unsqueeze_dim)) + (rotate_half(k) * sin.unsqueeze(unsqueeze_dim))).to(k.dtype)
    return q_embed, k_embed

def repeat_kv(x: torch.Tensor, n_rep: int) -> torch.Tensor:
    bs, slen, num_key_value_heads, head_dim = x.shape
    if n_rep == 1: return x
    return (x[:, :, :, None, :].expand(bs, slen, num_key_value_heads, n_rep, head_dim).reshape(bs, slen, num_key_value_heads * n_rep, head_dim))

class Attention(nn.Module):
    def __init__(self, config: MiniMindConfig):
        super().__init__()
        self.config = config
        self.num_key_value_heads = config.num_attention_heads if config.num_key_value_heads is None else config.num_key_value_heads
        self.n_local_heads = config.num_attention_heads
        self.n_local_kv_heads = self.num_key_value_heads
        self.n_rep = self.n_local_heads // self.n_local_kv_heads
        self.head_dim = config.head_dim
        
        self.use_dense_attention = config.use_dense_attention
        self.use_latent_attention = config.use_latent_attention

        if self.use_latent_attention:
            self.kv_lora_rank = config.kv_lora_rank
            self.qk_rope_dim = config.qk_rope_dim
            self.latent_head_dim = self.kv_lora_rank // self.n_local_heads
            
            # Latent 模式：Q 投影至 latent 維度 + rope 維度
            self.wq = nn.Linear(config.hidden_size, self.kv_lora_rank + self.n_local_heads * self.qk_rope_dim, bias=False)
            # Latent 模式：KV 直接壓縮至 latent 空間 + 單一 rope 維度
            self.kv_a_proj = nn.Linear(config.hidden_size, self.kv_lora_rank + self.qk_rope_dim, bias=False)
            self.o_proj = nn.Linear(self.kv_lora_rank, config.hidden_size, bias=False)
        else:
            self.q_proj = nn.Linear(config.hidden_size, config.num_attention_heads * self.head_dim, bias=False)
            self.k_proj = nn.Linear(config.hidden_size, self.num_key_value_heads * self.head_dim, bias=False)
            self.v_proj = nn.Linear(config.hidden_size, self.num_key_value_heads * self.head_dim, bias=False)
            self.o_proj = nn.Linear(config.num_attention_heads * self.head_dim, config.hidden_size, bias=False)
            
        self.q_norm = RMSNorm(self.head_dim if not self.use_latent_attention else self.latent_head_dim, eps=config.rms_norm_eps)
        self.k_norm = RMSNorm(self.head_dim if not self.use_latent_attention else self.latent_head_dim, eps=config.rms_norm_eps)
        self.attn_dropout = nn.Dropout(config.dropout)
        self.resid_dropout = nn.Dropout(config.dropout)
        self.dropout = config.dropout
        self.flash = hasattr(torch.nn.functional, 'scaled_dot_product_attention') and config.flash_attn

    def forward(self, x, position_embeddings, past_key_value=None, use_cache=False, attention_mask=None, global_kv_pool=None):
        bsz, seq_len, _ = x.shape

        if self.use_latent_attention:
            # === Latent Attention 邏輯 ===
            q_out = self.wq(x)
            q_content, q_rope = torch.split(q_out, [self.kv_lora_rank, self.n_local_heads * self.qk_rope_dim], dim=-1)
            q_content = q_content.view(bsz, seq_len, self.n_local_heads, self.latent_head_dim)
            q_rope = q_rope.view(bsz, seq_len, self.n_local_heads, self.qk_rope_dim)

            kv_out = self.kv_a_proj(x)
            k_content, k_rope = torch.split(kv_out, [self.kv_lora_rank, self.qk_rope_dim], dim=-1)
            # K 的 content 與 V 共享
            k_content = k_content.view(bsz, seq_len, 1, self.kv_lora_rank).expand(-1, -1, self.n_local_heads, -1)
            k_content = k_content.reshape(bsz, seq_len, self.n_local_heads, self.latent_head_dim)
            v_content = k_content.clone() 
            k_rope = k_rope.view(bsz, seq_len, 1, self.qk_rope_dim).expand(-1, -1, self.n_local_heads, -1)

            cos, sin = position_embeddings
            q_rope, k_rope = apply_rotary_pos_emb(q_rope, k_rope, cos, sin)
            
            # Temporal KV拼接 (B, S, H, D)
            xk_cur = torch.cat([k_content, k_rope], dim=-1)
            xv_cur = v_content
            
            if past_key_value is not None:
                xk = torch.cat([past_key_value[0], xk_cur], dim=1)
                xv = torch.cat([past_key_value[1], xv_cur], dim=1)
            else:
                xk, xv = xk_cur, xv_cur
            
            past_kv = (xk, xv) if use_cache else None
            
            # 準備用於 Attention 的 xq, xk, xv (B, H, S, D)
            xq = torch.cat([q_content, q_rope], dim=-1).transpose(1, 2)
            xk_for_attn = xk.transpose(1, 2)
            xv_for_attn = xv.transpose(1, 2)
        else:
            # === 標準 MHA/GQA 邏輯 ===
            xq, xk, xv = self.q_proj(x), self.k_proj(x), self.v_proj(x)
            xq = xq.view(bsz, seq_len, self.n_local_heads, self.head_dim)
            xk = xk.view(bsz, seq_len, self.n_local_kv_heads, self.head_dim)
            xv = xv.view(bsz, seq_len, self.n_local_kv_heads, self.head_dim)
            xq, xk = self.q_norm(xq), self.k_norm(xk)
            cos, sin = position_embeddings
            xq, xk = apply_rotary_pos_emb(xq, xk, cos, sin)
            
            if past_key_value is not None:
                xk = torch.cat([past_key_value[0], xk], dim=1)
                xv = torch.cat([past_key_value[1], xv], dim=1)
            past_kv = (xk, xv) if use_cache else None
            
            xq = xq.transpose(1, 2)
            xk_for_attn = repeat_kv(xk, self.n_rep).transpose(1, 2)
            xv_for_attn = repeat_kv(xv, self.n_rep).transpose(1, 2)

        # === Dense Attention (T x D) 核心邏輯 ===
        if self.use_dense_attention and global_kv_pool is not None:
            # 將當前層「已經拼接好過去時間」的 KV 放入 Pool (形狀均為 B, H, S_total, D)
            global_kv_pool['k'].append(xk_for_attn)
            global_kv_pool['v'].append(xv_for_attn)

            keys_total = torch.cat(global_kv_pool['k'], dim=2)
            values_total = torch.cat(global_kv_pool['v'], dim=2)
            
            current_layer_depth = len(global_kv_pool['k'])
            total_seq_len = xk_for_attn.shape[2]
            
            if attention_mask is not None:
                if attention_mask.dim() == 2:
                    am = attention_mask.unsqueeze(1).unsqueeze(2)
                else:
                    am = attention_mask
                extended_mask = am.repeat(1, 1, 1, current_layer_depth)
            else:
                extended_mask = torch.ones((bsz, 1, seq_len, total_seq_len * current_layer_depth), device=xq.device, dtype=torch.bool)

            if total_seq_len > 1:
                # 每個 Query token t 只能看到時間點 t' <= t 的所有層
                # 單層的 Causal Mask: (seq_len, total_seq_len)
                # 如果正在生成 (seq_len=1)，則 query 0 可以看到 key 0...total_seq_len-1，這是對的
                if seq_len > 1:
                    causal_mask = torch.tril(torch.ones(seq_len, total_seq_len, device=xq.device, dtype=torch.bool))
                    causal_mask = causal_mask.repeat(1, current_layer_depth)
                    extended_mask = extended_mask.to(torch.bool) & causal_mask.unsqueeze(0).unsqueeze(0)

            output = F.scaled_dot_product_attention(
                xq, keys_total, values_total, 
                attn_mask=extended_mask.to(xq.dtype) if extended_mask.dtype != torch.bool else extended_mask, 
                is_causal=False 
            )
        else:
            # 標準單層 Attention (或 Dense 被禁用)
            if self.flash and (seq_len > 1) and (past_key_value is None) and (attention_mask is None or torch.all(attention_mask == 1)):
                output = F.scaled_dot_product_attention(xq, xk_for_attn, xv_for_attn, dropout_p=self.dropout if self.training else 0.0, is_causal=True)
            else:
                curr_head_dim = xq.shape[-1]
                scores = (xq @ xk_for_attn.transpose(-2, -1)) / math.sqrt(curr_head_dim)
                if xk_for_attn.shape[2] > 1 and seq_len > 1:
                    scores[:, :, :, -seq_len:] += torch.full((seq_len, seq_len), float("-inf"), device=scores.device).triu(1)
                if attention_mask is not None: 
                    scores += (1.0 - attention_mask.unsqueeze(1).unsqueeze(2)) * -1e9
                output = self.attn_dropout(F.softmax(scores.float(), dim=-1).type_as(xq)) @ xv_for_attn

        output = output.transpose(1, 2).reshape(bsz, seq_len, -1)
        output = self.resid_dropout(self.o_proj(output))
        return output, past_kv

class FeedForward(nn.Module):
    def __init__(self, config: MiniMindConfig, intermediate_size: int = None):
        super().__init__()
        intermediate_size = intermediate_size or config.intermediate_size
        self.gate_proj = nn.Linear(config.hidden_size, intermediate_size, bias=False)
        self.down_proj = nn.Linear(intermediate_size, config.hidden_size, bias=False)
        self.up_proj = nn.Linear(config.hidden_size, intermediate_size, bias=False)
        self.act_fn = ACT2FN[config.hidden_act]

    def forward(self, x):
        return self.down_proj(self.act_fn(self.gate_proj(x)) * self.up_proj(x))

class MOEFeedForward(nn.Module):
    def __init__(self, config: MiniMindConfig):
        super().__init__()
        self.config = config
        self.gate = nn.Linear(config.hidden_size, config.num_experts, bias=False)
        self.experts = nn.ModuleList([FeedForward(config, intermediate_size=config.moe_intermediate_size) for _ in range(config.num_experts)])
        self.act_fn = ACT2FN[config.hidden_act]

    def forward(self, x):
        batch_size, seq_len, hidden_dim = x.shape
        x_flat = x.view(-1, hidden_dim)
        scores = F.softmax(self.gate(x_flat), dim=-1)
        topk_weight, topk_idx = torch.topk(scores, k=self.config.num_experts_per_tok, dim=-1, sorted=False)
        if self.config.norm_topk_prob: topk_weight = topk_weight / (topk_weight.sum(dim=-1, keepdim=True) + 1e-20)
        y = torch.zeros_like(x_flat)
        for i, expert in enumerate(self.experts):
            mask = (topk_idx == i)
            if mask.any():
                token_idx = mask.any(dim=-1).nonzero().flatten()
                weight = topk_weight[mask].view(-1, 1)
                y.index_add_(0, token_idx, (expert(x_flat[token_idx]) * weight).to(y.dtype))
            elif self.training:
                y[0, 0] += 0 * sum(p.sum() for p in expert.parameters())
        if self.training and self.config.router_aux_loss_coef > 0:
            load = F.one_hot(topk_idx, self.config.num_experts).float().mean(0)
            self.aux_loss = (load * scores.mean(0)).sum() * self.config.num_experts * self.config.router_aux_loss_coef
        else:
            self.aux_loss = scores.new_zeros(1).squeeze()
        return y.view(batch_size, seq_len, hidden_dim)

class MiniMindBlock(nn.Module):
    def __init__(self, layer_id: int, config: MiniMindConfig):
        super().__init__()
        self.self_attn = Attention(config)
        self.input_layernorm = RMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.post_attention_layernorm = RMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.mlp = FeedForward(config) if not config.use_moe else MOEFeedForward(config)

    def forward(self, hidden_states, position_embeddings, past_key_value=None, use_cache=False, attention_mask=None, global_kv_pool=None):
        residual = hidden_states
        hidden_states, updated_kv = self.self_attn(
            self.input_layernorm(hidden_states), position_embeddings,
            past_key_value, use_cache, attention_mask, global_kv_pool
        )
        hidden_states += residual
        hidden_states = hidden_states + self.mlp(self.post_attention_layernorm(hidden_states))
        return hidden_states, updated_kv

class MiniMindModel(nn.Module):
    def __init__(self, config: MiniMindConfig):
        super().__init__()
        self.config = config
        self.vocab_size, self.num_hidden_layers = config.vocab_size, config.num_hidden_layers
        self.embed_tokens = nn.Embedding(config.vocab_size, config.hidden_size)
        self.dropout = nn.Dropout(config.dropout)
        self.layers = nn.ModuleList([MiniMindBlock(l, config) for l in range(self.num_hidden_layers)])
        self.norm = RMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        freqs_cos, freqs_sin = precompute_freqs_cis(dim=config.head_dim if not config.use_latent_attention else config.qk_rope_dim, 
                                                    end=config.max_position_embeddings, rope_base=config.rope_theta, rope_scaling=config.rope_scaling)
        self.register_buffer("freqs_cos", freqs_cos, persistent=False)
        self.register_buffer("freqs_sin", freqs_sin, persistent=False)

        # --- 初始化 Engram 模組 ---
        self.use_engram = getattr(config, 'use_engram', False)
        if self.use_engram:
            self.engram_layers = set(getattr(config, 'engram_layers', [0, 1]))
            self.engram_system = EngramManager(config)

    def forward(self, input_ids, attention_mask=None, past_key_values=None, use_cache=False, **kwargs):
        batch_size, seq_length = input_ids.shape
        if hasattr(past_key_values, 'layers'): past_key_values = None
        past_key_values = past_key_values or [None] * len(self.layers)
        start_pos = past_key_values[0][0].shape[1] if past_key_values[0] is not None else 0
        hidden_states = self.dropout(self.embed_tokens(input_ids))
        position_embeddings = (self.freqs_cos[start_pos:start_pos + seq_length], self.freqs_sin[start_pos:start_pos + seq_length])

        # Stage 1: Gather Engram Knowledge (Deterministic Query)
        engram_vram_features = None
        if self.use_engram:
            full_input_ids = kwargs.get('full_input_ids', input_ids)
            engram_vram_features = self.engram_system.stage1_gather(full_input_ids, seq_length)

        global_kv_pool = {'k': [], 'v': []} if self.config.use_dense_attention else None
        presents = []
        for i, (layer, past_key_value) in enumerate(zip(self.layers, past_key_values)):
            # Stage 2: Fusion Engram Knowledge into corresponding blocks
            if self.use_engram and i in self.engram_layers:
                hidden_states = hidden_states + self.engram_system.stage2_fusion(i, hidden_states, engram_vram_features)

            hidden_states, present = layer(
                hidden_states,
                position_embeddings,
                past_key_value=past_key_value,
                use_cache=use_cache,
                attention_mask=attention_mask,
                global_kv_pool=global_kv_pool
            )
            presents.append(present)

                
        hidden_states = self.norm(hidden_states)
        aux_loss = sum([l.mlp.aux_loss for l in self.layers if isinstance(l.mlp, MOEFeedForward)], hidden_states.new_zeros(1).squeeze())
        return hidden_states, presents, aux_loss

class MiniMindForCausalLM(PreTrainedModel, GenerationMixin):
    config_class = MiniMindConfig
    def __init__(self, config: MiniMindConfig = None):
        self.config = config or MiniMindConfig()
        super().__init__(self.config)
        self.model = MiniMindModel(self.config)
        self.lm_head = nn.Linear(self.config.hidden_size, self.config.vocab_size, bias=False)
        self.model.embed_tokens.weight = self.lm_head.weight
    
    def forward(self, input_ids, attention_mask=None, past_key_values=None, use_cache=False, logits_to_keep=0, labels=None, **kwargs):
        if 'full_input_ids' not in kwargs: kwargs['full_input_ids'] = input_ids
        hidden_states, past_key_values, aux_loss = self.model(input_ids, attention_mask, past_key_values, use_cache, **kwargs)
        slice_indices = slice(-logits_to_keep, None) if isinstance(logits_to_keep, int) else logits_to_keep
        logits = self.lm_head(hidden_states[:, slice_indices, :])
        loss = None
        if labels is not None:
            x, y = logits[..., :-1, :].contiguous(), labels[..., 1:].contiguous()
            loss = F.cross_entropy(x.view(-1, x.size(-1)), y.view(-1), ignore_index=-100)
        return MoeCausalLMOutputWithPast(loss=loss, aux_loss=aux_loss, logits=logits, past_key_values=past_key_values, hidden_states=hidden_states)
    
    # https://github.com/jingyaogong/minimind/discussions/611
    @torch.inference_mode()
    def generate(self, inputs=None, attention_mask=None, max_new_tokens=8192, temperature=0.85, top_p=0.85, top_k=50, eos_token_id=2, streamer=None, use_cache=True, num_return_sequences=1, do_sample=True, repetition_penalty=1.0, **kwargs):
        input_ids = kwargs.pop("input_ids", inputs).repeat(num_return_sequences, 1)
        attention_mask = attention_mask.repeat(num_return_sequences, 1) if attention_mask is not None else None
        past_key_values = kwargs.pop("past_key_values", None)
        finished = torch.zeros(input_ids.shape[0], dtype=torch.bool, device=input_ids.device)
        if streamer: streamer.put(input_ids.cpu())
        for _ in range(max_new_tokens):
            past_len = past_key_values[0][0].shape[1] if past_key_values else 0
            outputs = self.forward(input_ids[:, past_len:], attention_mask, past_key_values, use_cache=use_cache, full_input_ids=input_ids, **kwargs)
            attention_mask = torch.cat([attention_mask, attention_mask.new_ones(attention_mask.shape[0], 1)], -1) if attention_mask is not None else None
            logits = outputs.logits[:, -1, :] / temperature
            if repetition_penalty != 1.0:
                for i in range(input_ids.shape[0]): logits[i, torch.unique(input_ids[i])] /= repetition_penalty
            if top_k > 0: 
                logits[logits < torch.topk(logits, top_k)[0][..., -1, None]] = -float('inf')
            if top_p < 1.0:
                sorted_logits, sorted_indices = torch.sort(logits, descending=True)
                mask = torch.cumsum(torch.softmax(sorted_logits, dim=-1), dim=-1) > top_p
                mask[..., 1:], mask[..., 0] = mask[..., :-1].clone(), 0
                logits[mask.scatter(1, sorted_indices, mask)] = -float('inf')
            next_token = torch.multinomial(torch.softmax(logits, dim=-1), num_samples=1) if do_sample else torch.argmax(logits, dim=-1, keepdim=True)
            if eos_token_id is not None: next_token = torch.where(finished.unsqueeze(-1), next_token.new_full((next_token.shape[0], 1), eos_token_id), next_token)
            input_ids = torch.cat([input_ids, next_token], dim=-1)
            past_key_values = outputs.past_key_values if use_cache else None
            if streamer: streamer.put(next_token.cpu())
            if eos_token_id is not None:
                finished |= next_token.squeeze(-1).eq(eos_token_id)
                if finished.all(): break
        if streamer: streamer.end()
        if kwargs.get("return_kv"): return {'generated_ids': input_ids, 'past_kv': past_key_values}
        return input_ids