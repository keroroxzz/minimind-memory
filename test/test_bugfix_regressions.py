"""FIX_TODO.md 回歸測試。

每個 test 對應 FIX_TODO.md 中的一個編號，並且在修好之前必須是紅燈。
直接執行：  python test/test_bugfix_regressions.py
"""
import os
import sys
import unittest

__package__ = "test"
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import torch

from model.model_minimind import MiniMindConfig, MiniMindForCausalLM


def build(**kw):
    cfg = dict(hidden_size=128, num_hidden_layers=3, num_attention_heads=4,
               use_engram=False, vocab_size=100)
    cfg.update(kw)
    torch.manual_seed(0)
    return MiniMindForCausalLM(MiniMindConfig(**cfg)).eval()


class TestDenseAttention(unittest.TestCase):
    def test_c2_padding_mask_honoured_at_decode(self):
        """C2: dense 解碼時 padding mask 必須真的遮住被 pad 的 token。"""
        m = build(use_dense_attention=True)
        ids = torch.randint(1, 100, (1, 6))
        am = torch.ones(1, 6)
        am[0, :2] = 0  # 前兩個 token 是 padding

        def run(seq):
            with torch.no_grad():
                pre = m(seq[:, :5], attention_mask=am[:, :5], use_cache=True)
                return m(seq[:, 5:], attention_mask=am,
                         past_key_values=pre.past_key_values, use_cache=True).logits

        base = run(ids)
        mutated = ids.clone()
        mutated[0, :2] = torch.tensor([7, 9])  # 改動被遮罩的位置
        self.assertLess((base - run(mutated)).abs().max().item(), 1e-5,
                        "被 padding 遮罩的 token 仍然影響了輸出")

    def test_c2_padding_mask_honoured_at_prefill(self):
        """C2: seq_len>1 的路徑本來就正確，確保修正沒有弄壞它。"""
        m = build(use_dense_attention=True)
        ids = torch.randint(1, 100, (1, 8))
        am = torch.ones(1, 8)
        am[0, :3] = 0
        with torch.no_grad():
            base = m(ids, attention_mask=am).logits[:, -1]
            mutated = ids.clone()
            mutated[0, :3] = torch.tensor([5, 6, 7])
            alt = m(mutated, attention_mask=am).logits[:, -1]
        self.assertLess((base - alt).abs().max().item(), 1e-5)

    def test_dense_prefill_matches_incremental_decode(self):
        """dense: 一次算完 vs 用 KV cache 逐 token，最後一步 logits 必須一致。"""
        m = build(use_dense_attention=True)
        ids = torch.randint(0, 100, (1, 10))
        with torch.no_grad():
            full = m(ids).logits[:, -1]
            pre = m(ids[:, :9], use_cache=True)
            inc = m(ids[:, 9:], past_key_values=pre.past_key_values, use_cache=True).logits[:, -1]
        self.assertLess((full - inc).abs().max().item(), 1e-4)

    def test_dense_mask_is_bool_not_additive(self):
        """C2 根因：送進 SDPA 的 mask 必須是 bool，float 會被當成 additive bias。"""
        import model.model_minimind as mm
        m = build(use_dense_attention=True)
        ids = torch.randint(1, 100, (1, 6))
        am = torch.ones(1, 6)
        am[0, :2] = 0
        seen = []
        orig = mm.F.scaled_dot_product_attention

        def spy(q, k, v, attn_mask=None, dropout_p=0.0, is_causal=False, **kw):
            if attn_mask is not None:
                seen.append(attn_mask.dtype)
            return orig(q, k, v, attn_mask, dropout_p, is_causal)

        mm.F.scaled_dot_product_attention = spy
        try:
            with torch.no_grad():
                pre = m(ids[:, :5], attention_mask=am[:, :5], use_cache=True)
                seen.clear()
                m(ids[:, 5:], attention_mask=am,
                  past_key_values=pre.past_key_values, use_cache=True)
        finally:
            mm.F.scaled_dot_product_attention = orig
        self.assertTrue(seen, "解碼步驟沒有送出任何 attn_mask")
        for dt in seen:
            self.assertEqual(dt, torch.bool, f"SDPA 收到 {dt} mask，會被當成 additive bias")


class TestEngram(unittest.TestCase):
    def _engram_model(self, **kw):
        cfg = dict(hidden_size=64, num_hidden_layers=2, num_attention_heads=4,
                   engram_layers=[0], engram_vocab_size=4000, n_head_per_ngram=2,
                   n_embed_per_ngram=8, engram_offload_cpu=False, vocab_size=100)
        cfg.update(kw)
        torch.manual_seed(0)
        return MiniMindForCausalLM(MiniMindConfig(**cfg)).eval()

    def test_c3_stage2_batch_matches_incremental(self):
        """C3: stage2 (含 ShortConv) 的增量結果必須等於全序列前向的同一位置。"""
        m = self._engram_model()
        eng = m.model.engram_system
        ids = torch.tensor([[11, 22, 33, 44, 55, 66, 77, 88, 99, 12, 34, 56]])
        hidden = torch.randn(1, ids.shape[1], 64)
        t = ids.shape[1] - 1

        with torch.no_grad():
            feats_full = eng.stage1_gather(ids, ids.shape[1], n_context=eng.conv_context)
            out_full = eng.stage2_fusion(0, hidden, feats_full)

            feats_inc = eng.stage1_gather(ids, 1, n_context=eng.conv_context)
            out_inc = eng.stage2_fusion(0, hidden[:, t:t + 1], feats_inc)

        self.assertLess((out_full[:, t:t + 1] - out_inc).abs().max().item(), 1e-5,
                        "ShortColl 增量與全序列不一致 (卷積看到 zero-padding)")

    def test_c3_conv_context_is_full_receptive_field(self):
        """C3: 撈取的歷史長度必須覆蓋 ShortConv 的完整感受野。"""
        m = self._engram_model(engram_kernel_size=4, max_ngram_size=3)
        eng = m.model.engram_system
        self.assertEqual(eng.conv_context, (4 - 1) * 3)

    def test_c3_end_to_end_generation_matches_teacher_forcing(self):
        """C3: 整個模型層級 — 有 engram 時，cache 解碼要對得上一次算完的結果。"""
        m = self._engram_model()
        ids = torch.randint(0, 100, (1, 12))
        with torch.no_grad():
            full = m(ids).logits[:, -1]
            pre = m(ids[:, :11], use_cache=True)
            inc = m(ids[:, 11:], past_key_values=pre.past_key_values,
                    use_cache=True, full_input_ids=ids).logits[:, -1]
        self.assertLess((full - inc).abs().max().item(), 1e-4)


if __name__ == '__main__':
    unittest.main(verbosity=2)
