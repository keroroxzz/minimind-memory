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

    def test_h4_max_ngram_size_above_three(self):
        """H4: max_ngram_size 是可調參數，>3 不該 IndexError。"""
        for n in (2, 4, 5, 8):
            with self.subTest(max_ngram_size=n):
                m = self._engram_model(max_ngram_size=n, engram_layers=[0])
                with torch.no_grad():
                    m(torch.randint(0, 100, (1, 8)))

    def test_h4_multipliers_backward_compatible(self):
        """H4: max_ngram_size=3 的乘數必須跟修正前完全一致。"""
        eng = self._engram_model(max_ngram_size=3).model.engram_system
        self.assertEqual(eng.multipliers.tolist(), [31, 10007, 424243])

    def test_h4_hash_stays_in_int64_range(self):
        """H4: 擴充的乘數不能讓 hash 溢位 int64。"""
        eng = self._engram_model(max_ngram_size=8).model.engram_system
        ids = torch.full((1, 16), 6399, dtype=torch.long)  # 最大 token id
        h = eng.get_hashes(ids, 16)
        self.assertTrue((h >= 0).all(), "hash 出現負值，代表 int64 溢位")

    def test_h4_rejects_degenerate_ngram_size(self):
        """H4: max_ngram_size<2 會讓 total_heads=0，應該直接報錯而非除以零。"""
        with self.assertRaises(ValueError):
            self._engram_model(max_ngram_size=1)


class TestRecurrence(unittest.TestCase):
    def test_h7_non_flash_mems_with_attention_mask(self):
        """H7: attention_mask 長度是 seq_len，但有 mems 時 scores 是 mem_len+seq_len。"""
        m = build(use_recurrence=True, mem_len=8, flash_attn=False)
        ids = torch.randint(0, 100, (1, 6))
        am = torch.ones(1, 6)
        with torch.no_grad():
            first = m(ids, attention_mask=am)
            m(ids, attention_mask=am, mems=first.next_mems)  # 之前 RuntimeError

    def test_h7_padding_still_masked_with_mems(self):
        """H7: 補齊 mask 之後，當前 segment 的 padding 仍然要被遮住。"""
        m = build(use_recurrence=True, mem_len=8, flash_attn=False)
        ids = torch.randint(1, 100, (1, 6))
        am = torch.ones(1, 6)
        am[0, :2] = 0
        with torch.no_grad():
            mems = m(ids, attention_mask=am).next_mems
            base = m(ids, attention_mask=am, mems=mems).logits[:, -1]
            mutated = ids.clone()
            mutated[0, :2] = torch.tensor([13, 17])
            alt = m(mutated, attention_mask=am, mems=mems).logits[:, -1]
        self.assertLess((base - alt).abs().max().item(), 1e-5)


class TestLoopedTransformer(unittest.TestCase):
    @staticmethod
    def _key_lengths(model, seq_len=8):
        import model.model_minimind as mm
        lengths = []
        orig = mm.F.scaled_dot_product_attention

        def spy(q, k, v, attn_mask=None, dropout_p=0.0, is_causal=False, **kw):
            lengths.append(k.shape[2])
            return orig(q, k, v, attn_mask, dropout_p, is_causal)

        mm.F.scaled_dot_product_attention = spy
        try:
            with torch.no_grad():
                model(torch.randint(0, 100, (1, seq_len)))
        finally:
            mm.F.scaled_dot_product_attention = orig
        return lengths

    def test_h3_kv_pool_resets_each_loop(self):
        """H3: dense pool 不可跨 loop 累積，否則深度變成 layers*num_loops。"""
        m = build(use_dense_attention=True, use_looped_transformer=True,
                  num_loops=3, num_hidden_layers=3)
        self.assertEqual(self._key_lengths(m, 8), [8, 16, 24] * 3)

    def test_h3_no_loop_unchanged(self):
        """H3: 沒開 looped 時行為必須完全不變。"""
        m = build(use_dense_attention=True, num_hidden_layers=3)
        self.assertEqual(self._key_lengths(m, 8), [8, 16, 24])

    def test_looped_presents_count(self):
        """looped: KV cache 條目數 = layers * num_loops。"""
        m = build(use_looped_transformer=True, num_loops=3, num_hidden_layers=2)
        with torch.no_grad():
            out = m(torch.randint(0, 100, (1, 8)), use_cache=True)
        self.assertEqual(len(out.past_key_values), 6)

    def test_looped_prefill_matches_incremental_decode(self):
        """looped: 多 loop 下 KV cache 的索引對應要正確。"""
        m = build(use_looped_transformer=True, num_loops=3, num_hidden_layers=2)
        ids = torch.randint(0, 100, (1, 10))
        with torch.no_grad():
            full = m(ids).logits[:, -1]
            pre = m(ids[:, :9], use_cache=True)
            inc = m(ids[:, 9:], past_key_values=pre.past_key_values, use_cache=True).logits[:, -1]
        self.assertLess((full - inc).abs().max().item(), 1e-4)


class TestLatentAttention(unittest.TestCase):
    def test_c1_forward_does_not_crash(self):
        """C1: use_latent_attention=1 之前每次 forward 都 RuntimeError。"""
        m = build(use_latent_attention=True, kv_lora_rank=128, qk_rope_dim=64)
        with torch.no_grad():
            out = m(torch.randint(0, 100, (2, 8)))
        self.assertEqual(out.logits.shape, (2, 8, 100))

    def test_c1_prefill_matches_incremental_decode(self):
        """C1: latent 路徑的 KV cache 必須正確。"""
        m = build(use_latent_attention=True, kv_lora_rank=128, qk_rope_dim=64)
        ids = torch.randint(0, 100, (1, 10))
        with torch.no_grad():
            full = m(ids).logits[:, -1]
            pre = m(ids[:, :9], use_cache=True)
            inc = m(ids[:, 9:], past_key_values=pre.past_key_values, use_cache=True).logits[:, -1]
        self.assertLess((full - inc).abs().max().item(), 1e-4)

    def test_c1_qk_norm_is_applied(self):
        """C1: q_norm/k_norm 是用 latent_head_dim 建的，必須真的被用到。"""
        m = build(use_latent_attention=True, kv_lora_rank=128, qk_rope_dim=64)
        attn = m.model.layers[0].self_attn
        with torch.no_grad():
            attn.q_norm.weight.fill_(1.0)
            base = m(torch.randint(0, 100, (1, 8))).logits.clone()
            attn.q_norm.weight.fill_(4.0)
            scaled = m(torch.randint(0, 100, (1, 8))).logits
        self.assertGreater((base - scaled).abs().max().item(), 1e-4,
                           "改動 q_norm 權重沒有影響輸出，代表它沒有被套用")

    def test_c1_composes_with_dense_attention(self):
        """C1: latent 的存在理由就是抵銷 dense 的 KV 膨脹，兩者必須能組合。"""
        m = build(use_latent_attention=True, use_dense_attention=True,
                  kv_lora_rank=128, qk_rope_dim=64)
        with torch.no_grad():
            out = m(torch.randint(0, 100, (1, 8)))
        self.assertEqual(out.logits.shape, (1, 8, 100))

    def test_c1_rejects_indivisible_rank(self):
        """C1: kv_lora_rank 不能整除 head 數時應該直接報錯，而非默默截斷。"""
        with self.assertRaises(ValueError):
            build(use_latent_attention=True, kv_lora_rank=100, qk_rope_dim=64,
                  num_attention_heads=8)


if __name__ == '__main__':
    unittest.main(verbosity=2)
