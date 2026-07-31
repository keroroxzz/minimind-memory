import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import torch
import model.model_minimind as model_module
from model.model_minimind import MiniMindConfig, MiniMindModel, MiniMindForCausalLM

def test_looped():
    config = MiniMindConfig(
        hidden_size=256,
        num_hidden_layers=2,
        use_looped_transformer=True,
        num_loops=3,
        loop_lora_rank=16,
        vocab_size=100
    )
    model = MiniMindModel(config)
    input_ids = torch.randint(0, config.vocab_size, (1, 10))
    hidden_states, presents, aux_loss, next_mems = model(input_ids)

    assert len(presents) == 6, f"Expected 6 presents, got {len(presents)}"
    print("Test passed: Looped Transformer executed 3 loops on 2 layers correctly.")


def test_loop_lora_starts_as_identity():
    """LoopLoRA 的 B 初始化為 0，因此訓練起點時額外的 loop 不會注入任何擾動。"""
    config = MiniMindConfig(hidden_size=128, num_hidden_layers=2, use_looped_transformer=True,
                            num_loops=3, loop_lora_rank=16, use_engram=False, vocab_size=100)
    model = MiniMindModel(config)
    for layer in model.layers:
        for idx in ('1', '2'):
            assert torch.all(layer.loop_lora_attn[idx].B.weight == 0), "attn LoRA B 應初始化為 0"
            assert torch.all(layer.loop_lora_mlp[idx].B.weight == 0), "mlp LoRA B 應初始化為 0"
            assert torch.any(layer.loop_lora_attn[idx].A.weight != 0), "attn LoRA A 不該全為 0"
    print("Test passed: LoopLoRA initialised as a no-op (B=0, A!=0).")


def test_dense_pool_resets_each_loop():
    """dense + looped：KV pool 不可跨 loop 累積。

    否則深度會變成 layers*num_loops，記憶體平方成長，且違反
    readme_dense_attention.md §1.1「第 L 層看得到第 1..L 層」的不變量。
    """
    config = MiniMindConfig(hidden_size=128, num_hidden_layers=3, num_attention_heads=4,
                            use_dense_attention=True, use_looped_transformer=True,
                            num_loops=3, use_engram=False, vocab_size=100)
    torch.manual_seed(0)
    model = MiniMindForCausalLM(config).eval()

    seq_len = 8
    lengths = []
    original = model_module.F.scaled_dot_product_attention

    def spy(q, k, v, attn_mask=None, dropout_p=0.0, is_causal=False, **kw):
        lengths.append(k.shape[2])
        return original(q, k, v, attn_mask, dropout_p, is_causal)

    model_module.F.scaled_dot_product_attention = spy
    try:
        with torch.no_grad():
            model(torch.randint(0, config.vocab_size, (1, seq_len)))
    finally:
        model_module.F.scaled_dot_product_attention = original

    expected = [seq_len * d for d in (1, 2, 3)] * 3
    assert lengths == expected, f"KV pool 未在每個 loop 重置: {lengths} != {expected}"
    print("Test passed: dense KV pool resets on every loop.")


def test_looped_kv_cache_roundtrip():
    """多 loop 下 past_key_values 的索引對應必須正確 (每個 loop-layer 一個條目)。"""
    config = MiniMindConfig(hidden_size=128, num_hidden_layers=2, num_attention_heads=4,
                            use_looped_transformer=True, num_loops=3, use_engram=False,
                            vocab_size=100)
    torch.manual_seed(0)
    model = MiniMindForCausalLM(config).eval()

    ids = torch.randint(0, config.vocab_size, (1, 10))
    with torch.no_grad():
        full = model(ids).logits[:, -1]
        pre = model(ids[:, :9], use_cache=True)
        assert len(pre.past_key_values) == 6, "cache 條目數應為 layers * num_loops"
        inc = model(ids[:, 9:], past_key_values=pre.past_key_values, use_cache=True).logits[:, -1]

    diff = (full - inc).abs().max().item()
    assert diff < 1e-4, f"looped 模式下 prefill 與增量解碼不一致 (diff={diff:.3e})"
    print("Test passed: looped KV cache indexing is correct.")


if __name__ == "__main__":
    test_looped()
    test_loop_lora_starts_as_identity()
    test_dense_pool_resets_each_loop()
    test_looped_kv_cache_roundtrip()
    print("\nAll looped transformer checks passed.")
