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
        self.engram_n: int = kwargs.get("engram_n", 2)
        self.engram_table_size: int = kwargs.get("engram_table_size", 131072)
        self.engram_layers: List[int] = kwargs.get("engram_layers", [2,])

        # --- MiniMind DDE-v1 新增參數 ---
        self.use_dde: bool = kwargs.get("use_dde", False)
        self.dde_layer: int = kwargs.get("dde_layer", 4)
        self.dde_num_slots: int = kwargs.get("dde_num_slots", 1024)
        self.dde_num_coarse: int = kwargs.get("dde_num_coarse", 16)
        self.dde_num_fine: int = kwargs.get("dde_num_fine", 64)
        self.dde_memory_dim: int = kwargs.get("dde_memory_dim", 256)
        self.dde_key_dim: int = kwargs.get("dde_key_dim", 128)
        self.dde_diversity_weight: float = kwargs.get("dde_diversity_weight", 0.02)
        self.dde_sparsity_weight: float = kwargs.get("dde_sparsity_weight", 0.01)
        self.dde_ema_decay: float = kwargs.get("dde_ema_decay", 0.5)


# 🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏
#                   DDE Module (Dynamic Discrete Engram v1)
# 🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏
class DDEModule(nn.Module):
    def __init__(self, config: MiniMindConfig):
        super().__init__()
        self.num_slots = config.dde_num_slots
        self.num_coarse = config.dde_num_coarse
        self.num_fine = config.dde_num_fine
        self.mem_dim = config.dde_memory_dim
        self.dim = config.hidden_size
        self.ema_decay = config.dde_ema_decay
        
        # [穩定初始化] 採用 0.02 縮放，既保證信號強度又防止 FP16 溢出
        self.memory_init = nn.Parameter(torch.randn(self.num_slots, self.mem_dim) * 0.02)
        
        # [信號縮放器] 初始設為 0.1，讓 DDE 內容緩慢融入主幹殘差流
        self.output_scale = nn.Parameter(torch.ones(1) * 0.1)
        
        # [小模型 A] Packer (寫入壓縮) 與 Unpacker (讀取解壓縮)
        self.packer = nn.Sequential(
            nn.Linear(self.dim, self.dim // 2),
            nn.SiLU(),
            nn.Linear(self.dim // 2, self.mem_dim),
            nn.LayerNorm(self.mem_dim)
        )
        self.unpacker = nn.Linear(self.mem_dim, self.dim)
        
        # [小模型 B] Indexer (層次化定址)
        self.indexer = nn.Sequential(
            nn.Linear(self.dim, self.dim),
            nn.SiLU(),
            nn.Linear(self.dim, self.num_coarse + self.num_fine + 2)
        )
        
        # [門控初始優化] 初始設為 0.0 (Sigmoid 後 0.5)
        nn.init.constant_(self.indexer[-1].bias[-2:], 0.0)

    def forward(self, h: torch.Tensor, split_idx: int = None, temp: float = 1.0):
        b, seq_len, _ = h.shape
        device = h.device
        
        # 初始記憶狀態
        current_memory = self.memory_init.unsqueeze(0).expand(b, -1, -1).clone()

        if split_idx is None or split_idx <= 0 or split_idx >= seq_len:
            # 推論模式
            indexer_out = self.indexer(h)
            coarse = F.softmax(indexer_out[..., :self.num_coarse] / max(temp, 0.05), dim=-1)
            fine = F.softmax(indexer_out[..., self.num_coarse : self.num_coarse + self.num_fine] / max(temp, 0.05), dim=-1)
            read_gate = torch.sigmoid(indexer_out[..., -1 : ])
            address = (coarse.unsqueeze(-1) * fine.unsqueeze(-2)).view(b, seq_len, self.num_slots)
            read_vec = torch.matmul(address, current_memory)
            # 加上 output_scale
            return h + self.unpacker(read_vec) * read_gate * self.output_scale, torch.tensor(0.0, device=device), torch.tensor(0.0, device=device)

        # 1. 物理切開
        h_context = h[:, :split_idx, :]
        h_query = h[:, split_idx:, :]

        # 階段 1: Context (純寫入)
        ctx_indexer_out = self.indexer(h_context)
        # 限制 temp 下限防止溢出
        safe_temp = max(temp, 0.05)
        ctx_coarse = F.softmax(ctx_indexer_out[..., :self.num_coarse] / safe_temp, dim=-1)
        ctx_fine = F.softmax(ctx_indexer_out[..., self.num_coarse : self.num_coarse + self.num_fine] / safe_temp, dim=-1)
        ctx_write_gate = torch.sigmoid(ctx_indexer_out[..., -2 : -1])
        ctx_address = (ctx_coarse.unsqueeze(-1) * ctx_fine.unsqueeze(-2)).view(b, split_idx, self.num_slots)
        ctx_packed = self.packer(h_context)

        update_strength = ctx_write_gate * ctx_address
        chunk_updates = torch.bmm(update_strength.transpose(1, 2), ctx_packed)
        chunk_weights = update_strength.sum(dim=1).unsqueeze(-1) + 1e-10
        normalized_updates = chunk_updates / chunk_weights
        
        update_mask = (chunk_weights > 0.001).to(h.dtype)
        new_memory = current_memory * (1 - update_mask * self.ema_decay) + \
                     normalized_updates * update_mask * self.ema_decay

        # 階段 2: Query (純讀取)
        q_indexer_out = self.indexer(h_query)
        q_coarse = F.softmax(q_indexer_out[..., :self.num_coarse] / safe_temp, dim=-1)
        q_fine = F.softmax(q_indexer_out[..., self.num_coarse : self.num_coarse + self.num_fine] / safe_temp, dim=-1)
        q_read_gate = torch.sigmoid(q_indexer_out[..., -1 : ])
        q_address = (q_coarse.unsqueeze(-1) * q_fine.unsqueeze(-2)).view(b, seq_len - split_idx, self.num_slots)
        
        read_vec = torch.bmm(q_address, new_memory)
        # 加上 output_scale 確保信號穩定
        h_query_mem = self.unpacker(read_vec) * q_read_gate * self.output_scale
        
        h_out = torch.cat([h_context, h_query + h_query_mem], dim=1)
        
        # [解除熵之陷阱] 修正 Diversity Loss
        avg_prob = ctx_address.mean(dim=(0, 1))
        uniform_log_p = math.log(1.0 / self.num_slots)
        div_loss = torch.sum(avg_prob * (torch.log(avg_prob + 1e-10) - uniform_log_p))
        sparsity_loss = ctx_write_gate.mean()
        
        return h_out, div_loss, sparsity_loss
        sparsity_loss = ctx_write_gate.mean()
        
        return h_out, div_loss, sparsity_loss


# 🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏
#                   Engram Module
# 🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏🌎🌍🌏
class EngramModule(nn.Module):
    def __init__(self, config: MiniMindConfig):
        super().__init__()
        self.n = config.engram_n
        self.table_size = config.engram_table_size
        self.dim = config.hidden_size
        self.engram_emb = nn.Embedding(self.table_size, self.dim)
        self.gate = nn.Linear(self.dim, self.dim, bias=False)
        self.prime = 31 

    def forward(self, input_ids: torch.Tensor, hidden_states: torch.Tensor) -> torch.Tensor:
        b, seq_len = input_ids.shape
        device = input_ids.device
        if self.n == 2:
            shifted_ids = torch.cat([torch.zeros((b, 1), dtype=input_ids.dtype, device=device), input_ids[:, :-1]], dim=1)
            hash_ids = (shifted_ids * self.prime + input_ids) % self.table_size
        else:
            hash_ids = input_ids.clone()
            for i in range(1, self.n):
                shifted = torch.cat([torch.zeros((b, i), dtype=input_ids.dtype, device=device), input_ids[:, :-i]], dim=1)
                hash_ids = (hash_ids * self.prime + shifted) % self.table_size
        engram_features = self.engram_emb(hash_ids)
        gate_weights = torch.sigmoid(self.gate(hidden_states))
        return engram_features * gate_weights

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
    if rope_scaling is not None:
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
        self.num_key_value_heads = config.num_attention_heads if config.num_key_value_heads is None else config.num_key_value_heads
        self.n_local_heads = config.num_attention_heads
        self.n_local_kv_heads = self.num_key_value_heads
        self.n_rep = self.n_local_heads // self.n_local_kv_heads
        self.head_dim = config.head_dim
        self.q_proj = nn.Linear(config.hidden_size, config.num_attention_heads * self.head_dim, bias=False)
        self.k_proj = nn.Linear(config.hidden_size, self.num_key_value_heads * self.head_dim, bias=False)
        self.v_proj = nn.Linear(config.hidden_size, self.num_key_value_heads * self.head_dim, bias=False)
        self.o_proj = nn.Linear(config.num_attention_heads * self.head_dim, config.hidden_size, bias=False)
        self.q_norm = RMSNorm(self.head_dim, eps=config.rms_norm_eps)
        self.k_norm = RMSNorm(self.head_dim, eps=config.rms_norm_eps)
        self.attn_dropout = nn.Dropout(config.dropout)
        self.resid_dropout = nn.Dropout(config.dropout)
        self.dropout = config.dropout
        self.flash = hasattr(torch.nn.functional, 'scaled_dot_product_attention') and config.flash_attn
    def forward(self, x, position_embeddings, past_key_value=None, use_cache=False, attention_mask=None):
        bsz, seq_len, _ = x.shape
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
        xq, xk, xv = (xq.transpose(1, 2), repeat_kv(xk, self.n_rep).transpose(1, 2), repeat_kv(xv, self.n_rep).transpose(1, 2))
        
        if attention_mask is not None and attention_mask.dim() == 4:
            scores = (xq @ xk.transpose(-2, -1)) / math.sqrt(self.head_dim)
            scores = scores + attention_mask
            output = self.attn_dropout(F.softmax(scores.float(), dim=-1).type_as(xq)) @ xv
        elif self.flash and (seq_len > 1) and (past_key_value is None) and (attention_mask is None or torch.all(attention_mask == 1)):
            output = F.scaled_dot_product_attention(xq, xk, xv, dropout_p=self.dropout if self.training else 0.0, is_causal=True)
        else:
            scores = (xq @ xk.transpose(-2, -1)) / math.sqrt(self.head_dim)
            scores[:, :, :, -seq_len:] += torch.full((seq_len, seq_len), float("-inf"), device=scores.device).triu(1)
            if attention_mask is not None: scores += (1.0 - attention_mask.unsqueeze(1).unsqueeze(2)) * -1e9
            output = self.attn_dropout(F.softmax(scores.float(), dim=-1).type_as(xq)) @ xv
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
        if self.training and self.config.router_aux_loss_coef > 0:
            load = F.one_hot(topk_idx, self.config.num_experts).float().mean(0)
            self.aux_loss = (load * scores.mean(0)).sum() * self.config.num_experts * self.config.router_aux_loss_coef
        else: self.aux_loss = scores.new_zeros(1).squeeze()
        return y.view(batch_size, seq_len, hidden_dim)

class MiniMindBlock(nn.Module):
    def __init__(self, layer_id: int, config: MiniMindConfig):
        super().__init__()
        self.self_attn = Attention(config)
        self.input_layernorm = RMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.post_attention_layernorm = RMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.mlp = FeedForward(config) if not config.use_moe else MOEFeedForward(config)
    def forward(self, hidden_states, position_embeddings, past_key_value=None, use_cache=False, attention_mask=None):
        residual = hidden_states
        hidden_states, present_key_value = self.self_attn(self.input_layernorm(hidden_states), position_embeddings, past_key_value, use_cache, attention_mask)
        hidden_states += residual
        hidden_states = hidden_states + self.mlp(self.post_attention_layernorm(hidden_states))
        return hidden_states, present_key_value

class MiniMindModel(nn.Module):
    def __init__(self, config: MiniMindConfig):
        super().__init__()
        self.config = config
        self.embed_tokens = nn.Embedding(config.vocab_size, config.hidden_size)
        self.dropout = nn.Dropout(config.dropout)
        self.layers = nn.ModuleList([MiniMindBlock(l, config) for l in range(config.num_hidden_layers)])
        self.norm = RMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        freqs_cos, freqs_sin = precompute_freqs_cis(dim=config.head_dim, end=config.max_position_embeddings, rope_base=config.rope_theta, rope_scaling=config.rope_scaling)
        self.register_buffer("freqs_cos", freqs_cos, persistent=False)
        self.register_buffer("freqs_sin", freqs_sin, persistent=False)
        self.use_engram = getattr(config, 'use_engram', False)
        if self.use_engram:
            self.engram_layers = set(getattr(config, 'engram_layers', [0, 1]))
            self.engram_module = EngramModule(config)
        self.use_dde = getattr(config, 'use_dde', False)
        if self.use_dde:
            self.dde_layer = config.dde_layer
            self.dde_module = DDEModule(config)

    def create_chunked_mask(self, seq_len, split_idx, device):
        mask = torch.tril(torch.ones((seq_len, seq_len), device=device))
        if split_idx is not None and 0 < split_idx < seq_len:
            mask[split_idx:, :split_idx] = 0.0
        attention_mask = torch.zeros((seq_len, seq_len), device=device)
        attention_mask = attention_mask.masked_fill(mask == 0, float('-inf'))
        return attention_mask.unsqueeze(0).unsqueeze(0)

    def forward(self, input_ids, attention_mask=None, past_key_values=None, use_cache=False, **kwargs):
        batch_size, seq_length = input_ids.shape
        past_key_values = past_key_values or [None] * len(self.layers)
        start_pos = past_key_values[0][0].shape[1] if past_key_values[0] is not None else 0
        hidden_states = self.dropout(self.embed_tokens(input_ids))
        position_embeddings = (self.freqs_cos[start_pos:start_pos + seq_length], self.freqs_sin[start_pos:start_pos + seq_length])
        
        split_idx = kwargs.get("split_idx", None)
        if split_idx is not None and isinstance(split_idx, torch.Tensor):
            split_idx = split_idx[0].item() # Assume batch consistent split for simplicity
            
        if self.use_dde and split_idx is not None:
            attention_mask = self.create_chunked_mask(seq_length, split_idx, input_ids.device)

        presents, dde_div_loss, dde_sparsity_loss = [], 0.0, 0.0
        for i, (layer, past_key_value) in enumerate(zip(self.layers, past_key_values)):
            if self.use_engram and i in self.engram_layers:
                hidden_states = hidden_states + self.engram_module(input_ids, hidden_states)
            
            if self.use_dde and i == self.dde_layer:
                temp = kwargs.get("dde_temp", 1.0)
                hidden_states, dde_div_loss, dde_sparsity_loss = self.dde_module(hidden_states, split_idx=split_idx, temp=temp)
            hidden_states, present = layer(hidden_states, position_embeddings, past_key_value, use_cache, attention_mask)
            presents.append(present)

        hidden_states = self.norm(hidden_states)
        aux_loss = sum([l.mlp.aux_loss for l in self.layers if isinstance(l.mlp, MOEFeedForward)], hidden_states.new_zeros(1).squeeze())

        if self.use_dde:
            aux_loss += (self.config.dde_sparsity_weight * dde_sparsity_loss) +\
                        (self.config.dde_diversity_weight * dde_div_loss)

        return hidden_states, presents, aux_loss

class MiniMindForCausalLM(PreTrainedModel, GenerationMixin):
    config_class = MiniMindConfig

    def __init__(self, config: MiniMindConfig = None):
        self.config = config or MiniMindConfig()
        super().__init__(self.config)
        self.model = MiniMindModel(self.config)
        self.lm_head = nn.Linear(self.config.hidden_size, self.config.vocab_size, bias=False)
        self.model.embed_tokens.weight = self.lm_head.weight

    def freeze_backbone(self):
        for name, param in self.named_parameters():
            param.requires_grad = ('dde_module' in name)

    def forward(self, input_ids, attention_mask=None, past_key_values=None, use_cache=False, logits_to_keep=0, labels=None, **kwargs):
        hidden_states, past_key_values, aux_loss = self.model(input_ids, attention_mask, past_key_values, use_cache, **kwargs)
        logits = self.lm_head(hidden_states[:, -logits_to_keep:, :]) if logits_to_keep > 0 else self.lm_head(hidden_states)
        loss = None

        if labels is not None:
            x, y = logits[..., :-1, :].contiguous(), labels[..., 1:].contiguous()
            loss = F.cross_entropy(x.view(-1, x.size(-1)), y.view(-1), ignore_index=-100)

        return MoeCausalLMOutputWithPast(loss=loss, aux_loss=aux_loss, logits=logits, past_key_values=past_key_values, hidden_states=hidden_states)

    @torch.inference_mode()
    def generate(self, inputs=None, max_new_tokens=8192, temperature=0.85, top_p=0.85, top_k=50, eos_token_id=2, streamer=None, use_cache=True, do_sample=True, repetition_penalty=1.0, **kwargs):
        input_ids = kwargs.pop("input_ids", inputs)
        finished = torch.zeros(input_ids.shape[0], dtype=torch.bool, device=input_ids.device)
        if streamer:
            streamer.put(input_ids.cpu())
        
        for _ in range(max_new_tokens):
            outputs = self.forward(input_ids, use_cache=use_cache, **kwargs)
            logits = outputs.logits[:, -1, :] / temperature
            if repetition_penalty != 1.0:
                for i in range(input_ids.shape[0]): logits[i, torch.unique(input_ids[i])] /= repetition_penalty

            if top_k > 0:
                logits[logits < torch.topk(logits, top_k)[0][..., -1, None]] = -float('inf')

            if top_p < 1.0:
                sorted_logits, sorted_indices = torch.sort(logits, descending=True)
                mask = (torch.cumsum(torch.softmax(sorted_logits, dim=-1), dim=-1) > top_p); mask[..., 1:], mask[..., 0] = mask[..., :-1].clone(), 0
                logits[mask.scatter(1, sorted_indices, mask)] = -float('inf')

            next_token = torch.multinomial(torch.softmax(logits, dim=-1), 1) if do_sample else torch.argmax(logits, dim=-1, keepdim=True)
            if eos_token_id is not None:
                next_token = torch.where(finished.unsqueeze(-1), next_token.new_full((next_token.shape[0], 1), eos_token_id), next_token)
            input_ids = torch.cat([input_ids, next_token], dim=-1)
            
            if streamer:
                streamer.put(next_token.cpu())
            if eos_token_id is not None:
                finished |= next_token.squeeze(-1).eq(eos_token_id)
                if finished.all():
                    break
        if streamer:
            streamer.end()
        
        return input_ids
