import os
import sys

__package__ = "test"
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import torch
import torch.nn.functional as F
import model.model_minimind as model_module
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

def test_dense_attention():
    print("Starting Dense Attention Numerical Verification...")
    
    config = MiniMindConfig(
        hidden_size=256,
        num_hidden_layers=4,
        num_attention_heads=8,
        use_dense_attention=True,
        vocab_size=100
    )
    
    model = MiniMindForCausalLM(config).eval()
    
    original_sdpa = model_module.F.scaled_dot_product_attention
    layer_stats = []

    def patched_sdpa(query, key, value, attn_mask=None, dropout_p=0.0, is_causal=False):
        # Calculate stats
        attn_weights = (query @ key.transpose(-2, -1)) / (query.shape[-1]**0.5)
        if attn_mask is not None:
            if attn_mask.dtype == torch.bool:
                attn_weights.masked_fill_(~attn_mask, float('-inf'))
            else:
                attn_weights += attn_mask
        
        softmax_weights = F.softmax(attn_weights, dim=-1)
        
        stats = {
            'key_shape': key.shape,
            'max_weight': softmax_weights.max().item(),
            'mean_weight': softmax_weights.mean().item(),
            'std_weight': softmax_weights.std().item(),
            'entropy': -(softmax_weights * torch.log(softmax_weights + 1e-9)).sum(dim=-1).mean().item()
        }
        layer_stats.append(stats)
        
        return original_sdpa(query, key, value, attn_mask, dropout_p, is_causal)

    model_module.F.scaled_dot_product_attention = patched_sdpa
    
    try:
        batch_size = 1
        seq_len = 32
        input_ids = torch.randint(0, config.vocab_size, (batch_size, seq_len))
        
        with torch.no_grad():
            model(input_ids)
            
        print("\nNumerical Stats per Layer:")
        for i, stats in enumerate(layer_stats):
            print(f"Layer {i}:")
            print(f"  KV Shape: {stats['key_shape']}")
            print(f"  Max Attention Weight: {stats['max_weight']:.4f}")
            print(f"  Entropy: {stats['entropy']:.4f}")

        # --- 以下是實際的斷言 (原本這個檔案只印統計、不驗證任何東西) ---
        assert len(layer_stats) == config.num_hidden_layers, "每層都應該呼叫一次 attention"

        # 跨層 KV pool 應該逐層加深：第 L 層看得到 1..L 層，長度 = seq_len * L
        for depth, stats in enumerate(layer_stats, start=1):
            assert stats['key_shape'][2] == seq_len * depth, \
                f"第 {depth} 層的 KV 長度應為 {seq_len * depth}，實際 {stats['key_shape'][2]}"

        # 注意力權重必須是有效的機率分佈，且不能塌縮或發散
        for i, stats in enumerate(layer_stats):
            assert 0.0 < stats['max_weight'] <= 1.0 + 1e-5, f"第 {i} 層權重超出 [0,1]"
            assert stats['entropy'] > 0.0, f"第 {i} 層 entropy 非正 (塌縮到單一 token)"
            assert stats['entropy'] == stats['entropy'], f"第 {i} 層 entropy 為 NaN"

        print("\n✅ 跨層 KV pool 深度與注意力分佈檢查通過")
        return True

    finally:
        model_module.F.scaled_dot_product_attention = original_sdpa


def test_dense_causality():
    """核心不變量：第 L 層、時間 t 的 Query 不能看到任何 t' > t 的資訊。"""
    print("\nStarting Dense Attention Causality Check...")
    config = MiniMindConfig(hidden_size=128, num_hidden_layers=4, num_attention_heads=4,
                            use_dense_attention=True, use_engram=False, vocab_size=100)
    torch.manual_seed(0)
    model = MiniMindForCausalLM(config).eval()

    ids = torch.randint(1, config.vocab_size, (1, 12))
    with torch.no_grad():
        base = model(ids).logits
        mutated = ids.clone()
        mutated[0, 8:] = torch.randint(1, config.vocab_size, (4,))  # 只改未來的 token
        after = model(mutated).logits

    drift = (base[:, :8] - after[:, :8]).abs().max().item()
    assert drift < 1e-5, f"改動未來 token 影響了位置 0..7 的輸出 (drift={drift:.3e})，因果律被破壞"
    print(f"✅ Causality holds across all layers (drift={drift:.2e})")
    return True


def test_dense_padding_and_cache():
    """padding mask 與 KV cache 在 dense 模式下都必須正確 (含 seq_len=1 的解碼步驟)。"""
    print("\nStarting Dense Attention Mask / Cache Check...")
    config = MiniMindConfig(hidden_size=128, num_hidden_layers=3, num_attention_heads=4,
                            use_dense_attention=True, use_engram=False, vocab_size=100)
    torch.manual_seed(0)
    model = MiniMindForCausalLM(config).eval()

    # 1) prefill 與逐 token 解碼必須得到相同的最後一步 logits
    ids = torch.randint(0, config.vocab_size, (1, 10))
    with torch.no_grad():
        full = model(ids).logits[:, -1]
        pre = model(ids[:, :9], use_cache=True)
        inc = model(ids[:, 9:], past_key_values=pre.past_key_values, use_cache=True).logits[:, -1]
    diff = (full - inc).abs().max().item()
    assert diff < 1e-4, f"prefill 與增量解碼不一致 (diff={diff:.3e})"

    # 2) 被 padding 遮住的 token 不得影響解碼結果
    ids = torch.randint(1, config.vocab_size, (1, 6))
    am = torch.ones(1, 6); am[0, :2] = 0

    def decode(seq):
        with torch.no_grad():
            pre = model(seq[:, :5], attention_mask=am[:, :5], use_cache=True)
            return model(seq[:, 5:], attention_mask=am,
                         past_key_values=pre.past_key_values, use_cache=True).logits

    mutated = ids.clone(); mutated[0, :2] = torch.tensor([7, 9])
    drift = (decode(ids) - decode(mutated)).abs().max().item()
    assert drift < 1e-5, f"padding mask 在解碼時失效 (drift={drift:.3e})"

    print("✅ Mask and cache behaviour verified (prefill == incremental, padding honoured)")
    return True


if __name__ == "__main__":
    test_dense_attention()
    test_dense_causality()
    test_dense_padding_and_cache()
    print("\nAll dense attention checks passed.")
