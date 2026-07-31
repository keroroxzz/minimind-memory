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

from model.model_minimind import MiniMindConfig, MiniMindForCausalLM  # noqa: E402


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


class TestGQAPool(unittest.TestCase):
    def test_m2_repeat_kv_heads_matches_repeat_kv_ordering(self):
        """M2: 新的 [B,H,S,D] 展開必須與既有 repeat_kv 的 head 排列完全一致。"""
        from model.model_minimind import repeat_kv, repeat_kv_heads
        x = torch.randn(2, 5, 3, 8)  # [B, S, H_kv, D]
        for n_rep in (1, 2, 4):
            with self.subTest(n_rep=n_rep):
                expected = repeat_kv(x, n_rep).transpose(1, 2)
                got = repeat_kv_heads(x.transpose(1, 2), n_rep)
                torch.testing.assert_close(got, expected)

    def test_m2_pool_holds_kv_heads_not_query_heads(self):
        """M2: pool 應該存 n_kv_heads 份，而非展開後的 n_attention_heads。"""
        import model.model_minimind as mm
        m = build(use_dense_attention=True, num_attention_heads=8,
                  num_key_value_heads=2, num_hidden_layers=2)
        pooled, orig = [], mm.torch.cat

        attn = m.model.layers[0].self_attn
        real_forward = attn.forward

        def wrapper(x, position_embeddings, past_key_value=None, use_cache=False,
                    attention_mask=None, global_kv_pool=None, mems=None):
            out = real_forward(x, position_embeddings, past_key_value, use_cache,
                               attention_mask, global_kv_pool, mems)
            if global_kv_pool:
                pooled.append(global_kv_pool['k'][0].shape[1])
            return out

        attn.forward = wrapper
        try:
            with torch.no_grad():
                m(torch.randint(0, 100, (1, 8)))
        finally:
            attn.forward = real_forward
        self.assertEqual(pooled, [2], "pool 存了展開後的 head 數，GQA 的節省被抵銷")

    def test_m2_gqa_dense_prefill_matches_decode(self):
        """M2: GQA + dense 的 cache 行為必須維持正確。"""
        m = build(use_dense_attention=True, num_attention_heads=8, num_key_value_heads=2)
        ids = torch.randint(0, 100, (1, 10))
        with torch.no_grad():
            full = m(ids).logits[:, -1]
            pre = m(ids[:, :9], use_cache=True)
            inc = m(ids[:, 9:], past_key_values=pre.past_key_values, use_cache=True).logits[:, -1]
        self.assertLess((full - inc).abs().max().item(), 1e-4)


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

    def test_m4_windowed_hash_matches_full_prefix_hash(self):
        """M4: 只 hash 窗口內的 token，結果必須與 hash 整個 prefix 完全相同。"""
        eng = self._engram_model().model.engram_system
        ids = torch.randint(1, 100, (2, 64))
        for L_curr in (1, 3, 16):
            with self.subTest(L_curr=L_curr):
                windowed = eng.get_hashes(ids, L_curr)
                reference = eng.get_hashes(ids, ids.shape[1])[:, -L_curr:, :]
                self.assertTrue(torch.equal(windowed, reference))

    def test_m4_decode_step_is_constant_work(self):
        """M4: 解碼時每步搬運的列數應該是常數，而非隨 prefix 線性成長。"""
        m = self._engram_model()
        eng = m.model.engram_system
        widths = []
        real = eng.embedding_table.forward
        eng.embedding_table.forward = lambda ids: widths.append(ids.shape[1]) or real(ids)
        try:
            with torch.no_grad():
                m.generate(torch.randint(0, 100, (1, 16)), max_new_tokens=6,
                           do_sample=False, eos_token_id=None)
        finally:
            eng.embedding_table.forward = real
        decode_widths = widths[1:]  # 第一次是 prefill
        self.assertTrue(all(w == decode_widths[0] for w in decode_widths),
                        f"每步搬運列數隨 prefix 成長: {decode_widths}")

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


class TestMoE(unittest.TestCase):
    @staticmethod
    def _moe(**kw):
        cfg = dict(hidden_size=32, num_hidden_layers=1, use_moe=True, num_experts=4,
                   use_engram=False, vocab_size=100)
        cfg.update(kw)
        torch.manual_seed(0)
        return MiniMindForCausalLM(MiniMindConfig(**cfg)).train()

    def _gate_grad(self, **kw):
        m = self._moe(router_aux_loss_coef=0.0, **kw)
        out = m(torch.randint(0, 100, (2, 8)), labels=torch.randint(0, 100, (2, 8)))
        out.loss.backward()
        return m.model.layers[0].mlp.gate.weight.grad.abs().sum().item()

    def test_h6_router_receives_gradient_with_k1(self):
        """H6: 預設 k=1 + norm_topk_prob=True 讓權重恆為 1.0，router 收不到梯度。"""
        self.assertGreater(self._gate_grad(num_experts_per_tok=1, norm_topk_prob=True), 1e-3)

    def test_h6_router_gradient_for_all_configs(self):
        """H6: 各種 k / norm 組合下 router 都必須可學。"""
        for k, norm in [(1, True), (1, False), (2, True), (2, False)]:
            with self.subTest(k=k, norm=norm):
                self.assertGreater(
                    self._gate_grad(num_experts_per_tok=k, norm_topk_prob=norm), 1e-3)

    def test_h6_topk_normalisation_still_applies_for_k_above_1(self):
        """H6: k>1 時 norm_topk_prob 的行為不可改變 (權重和為 1)。"""
        m = self._moe(num_experts_per_tok=2, norm_topk_prob=True)
        mlp = m.model.layers[0].mlp
        scores = torch.softmax(mlp.gate(torch.randn(5, 32)), dim=-1)
        w, _ = torch.topk(scores, k=2, dim=-1, sorted=False)
        w = w / (w.sum(dim=-1, keepdim=True) + 1e-20)
        torch.testing.assert_close(w.sum(-1), torch.ones(5))

    def test_m5_aux_loss_accumulates_over_loops(self):
        """M5: 迴圈結束後才讀 layer.mlp.aux_loss，只會剩下最後一個 loop 的值。"""
        collected = []
        m = self._moe(num_hidden_layers=2, use_looped_transformer=True, num_loops=3,
                      router_aux_loss_coef=1.0)
        for layer in m.model.layers:
            real = layer.mlp.forward
            layer.mlp.forward = (lambda r, mod: (lambda x: (r(x), collected.append(
                mod.aux_loss.item()))[0]))(real, layer.mlp)

        out = m(torch.randint(0, 100, (2, 8)))
        self.assertEqual(len(collected), 6, "2 層 x 3 loop 應該有 6 次 MoE 前向")
        expected = sum(collected) / 3  # 除以 num_loops 保持量級
        self.assertAlmostEqual(out.aux_loss.item(), expected, places=5)

    def test_m5_aux_loss_unchanged_without_loops(self):
        """M5: 沒開 looped 時 aux_loss 必須與原本一致 (各層相加)。"""
        collected = []
        m = self._moe(num_hidden_layers=2, router_aux_loss_coef=1.0)
        for layer in m.model.layers:
            real = layer.mlp.forward
            layer.mlp.forward = (lambda r, mod: (lambda x: (r(x), collected.append(
                mod.aux_loss.item()))[0]))(real, layer.mlp)
        out = m(torch.randint(0, 100, (2, 8)))
        self.assertAlmostEqual(out.aux_loss.item(), sum(collected), places=5)

    def test_m5_aux_loss_load_is_per_expert(self):
        """M5: load 必須是 [E]，原本 mean(0) 得到 [k,E]，k>1 會跨 slot 重複計算。"""
        m = self._moe(num_experts_per_tok=2, router_aux_loss_coef=1.0)
        mlp = m.model.layers[0].mlp
        m(torch.randint(0, 100, (2, 8)))
        self.assertEqual(mlp.aux_loss.shape, ())

        # 手算對照：load_i = 每個 token 是否路由到 expert i 的平均
        x = torch.randn(16, 32)
        scores = torch.softmax(mlp.gate(x), dim=-1)
        _, idx = torch.topk(scores, k=2, dim=-1, sorted=False)
        load = torch.nn.functional.one_hot(idx, 4).float().sum(dim=1).mean(dim=0)
        self.assertEqual(load.shape, (4,))
        self.assertAlmostEqual(load.sum().item(), 2.0, places=4)  # k=2


class TestEngramOffload(unittest.TestCase):
    CFG = dict(hidden_size=64, num_hidden_layers=2, engram_layers=[0],
               engram_vocab_size=200000, n_head_per_ngram=8, n_embed_per_ngram=64,
               engram_offload_cpu=True, vocab_size=100)

    def test_h5_ddp_ignore_list_includes_offloaded_table(self):
        """H5: DDP 不接受混合裝置的 module，offload 的表必須在忽略名單裡。"""
        m = MiniMindForCausalLM(MiniMindConfig(**self.CFG))
        self.assertIn("model.engram_system.embedding_table.embedding.weight",
                      m._ddp_params_and_buffers_to_ignore)

    def test_h5_offloaded_parameters_exposed(self):
        """H5: 被 DDP 忽略的參數要能被列出，梯度才有辦法手動同步。"""
        m = MiniMindForCausalLM(MiniMindConfig(**self.CFG))
        params = m.offloaded_parameters()
        self.assertEqual(len(params), 1)
        self.assertIs(params[0], m.model.engram_system.embedding_table.embedding.weight)

        off = MiniMindForCausalLM(MiniMindConfig(**{**self.CFG, 'engram_offload_cpu': False}))
        self.assertEqual(off.offloaded_parameters(), [])

    @unittest.skipUnless(torch.cuda.is_available(), "需要 CUDA")
    def test_h5_table_never_touches_gpu(self):
        """H5: super()._apply 會先把整張表搬上 GPU 再搬回來，造成暫態 VRAM 尖峰。"""
        m = MiniMindForCausalLM(MiniMindConfig(**self.CFG))
        table_bytes = m.model.engram_system.embedding_table.embedding.weight.numel() * 4

        # reset_peak_memory_stats 是把 peak 拉回「目前已配置量」而非 0，所以要量差值
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats()
        before = torch.cuda.memory_allocated()
        m = m.cuda()
        spike = torch.cuda.max_memory_allocated() - before

        self.assertEqual(m.model.engram_system.embedding_table.embedding.weight.device.type, 'cpu')
        self.assertLess(spike, table_bytes,
                        f"搬移過程的暫態 VRAM 尖峰 {spike} 已達整張 engram 表 {table_bytes} 的規模")

    @unittest.skipUnless(torch.cuda.is_available(), "需要 CUDA")
    def test_h5_half_keeps_table_on_cpu_with_matching_dtype(self):
        """H5: .half() 之後表要留在 CPU，但 dtype 必須跟其他元件一致。"""
        m = MiniMindForCausalLM(MiniMindConfig(**self.CFG)).cuda().half()
        w = m.model.engram_system.embedding_table.embedding.weight
        self.assertEqual(w.device.type, 'cpu')
        self.assertEqual(w.dtype, m.model.engram_system.fusions['0']['value_proj'].weight.dtype)
        with torch.no_grad():
            m(torch.randint(0, 100, (1, 8), device='cuda'))


class TestRecurrence(unittest.TestCase):
    def test_c4_cache_does_not_double_count_mems(self):
        """C4: 有 KV cache 時不可重新編碼 mem 位置，否則 cache 每步暴漲。"""
        m = build(use_recurrence=True, mem_len=8)
        ids = torch.randint(0, 100, (1, 6))
        with torch.no_grad():
            first = m(ids, use_cache=True)
            self.assertEqual(first.past_key_values[0][0].shape[1], 6)
            second = m(ids[:, -1:], past_key_values=first.past_key_values,
                       use_cache=True, mems=first.next_mems)
        self.assertEqual(second.past_key_values[0][0].shape[1], 7,
                         "cache 長度不是 +1，代表 mems 被重複編碼進 cache")

    def test_c4_generate_works_with_recurrence(self):
        """C4: use_recurrence=1 之前會讓 generate 的 start_pos 漂移而失效。"""
        m = build(use_recurrence=True, mem_len=8)
        out = m.generate(torch.randint(0, 100, (1, 6)), max_new_tokens=5,
                         do_sample=False, eos_token_id=None)
        self.assertEqual(out.shape, (1, 11))

    def test_c5_mem_tokens_get_distinct_rope_positions(self):
        """C5: mem token 不可全部塌到 position 0。"""
        import model.model_minimind as mm
        m = build(use_recurrence=True, mem_len=8)
        seen = []
        orig = mm.apply_rotary_pos_emb

        def spy(q, k, q_cos, q_sin, k_cos, k_sin, unsqueeze_dim=1):
            seen.append((q_cos.shape[0], k_cos.shape[0],
                         torch.unique(k_cos, dim=0).shape[0]))
            return orig(q, k, q_cos, q_sin, k_cos, k_sin, unsqueeze_dim)

        mm.apply_rotary_pos_emb = spy
        try:
            ids = torch.randint(0, 100, (1, 6))
            with torch.no_grad():
                mems = m(ids).next_mems
                seen.clear()
                m(ids, mems=mems)
        finally:
            mm.apply_rotary_pos_emb = orig

        for q_len, k_len, distinct in seen:
            self.assertEqual((q_len, k_len), (6, 12))
            self.assertEqual(distinct, 12, "K 的位置有重複，mem 區塊被 clamp 到同一個位置")

    def test_c5_query_is_offset_past_the_mems(self):
        """C5: Q 的位置必須接在 mems 之後 (mem_len ..)，K 則從 0 起算。"""
        import model.model_minimind as mm
        m = build(use_recurrence=True, mem_len=8)
        freqs = m.model.freqs_cos
        captured = {}
        orig = mm.apply_rotary_pos_emb

        def spy(q, k, q_cos, q_sin, k_cos, k_sin, unsqueeze_dim=1):
            captured.setdefault('q_cos', q_cos)
            captured.setdefault('k_cos', k_cos)
            return orig(q, k, q_cos, q_sin, k_cos, k_sin, unsqueeze_dim)

        mm.apply_rotary_pos_emb = spy
        try:
            ids = torch.randint(0, 100, (1, 6))
            with torch.no_grad():
                mems = m(ids).next_mems
                captured.clear()
                m(ids, mems=mems)
        finally:
            mm.apply_rotary_pos_emb = orig

        mem_len = mems[0].shape[1]
        torch.testing.assert_close(captured['k_cos'], freqs[:mem_len + 6])
        torch.testing.assert_close(captured['q_cos'], freqs[mem_len:mem_len + 6])

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


    def test_h1_mems_are_the_attention_input(self):
        """H1: mems 必須是 input_layernorm 的輸出，而非 post_attention_layernorm。"""
        m = build(use_recurrence=True, mem_len=16, num_hidden_layers=2)
        layer = m.model.layers[0]
        captured = {}

        def pre_hook(mod, args, kwargs):
            captured['h'] = args[0] if args else kwargs['hidden_states']

        handle = layer.register_forward_pre_hook(pre_hook, with_kwargs=True)
        try:
            with torch.no_grad():
                out = m(torch.randint(0, 100, (1, 6)))
        finally:
            handle.remove()

        with torch.no_grad():
            expected = layer.input_layernorm(captured['h'])
        torch.testing.assert_close(out.next_mems[0], expected)

    def test_h1_mems_not_post_attention_norm(self):
        """H1: 明確確認 mems 不等於 post_attention_layernorm 的輸出 (舊行為)。"""
        m = build(use_recurrence=True, mem_len=16, num_hidden_layers=2)
        layer = m.model.layers[0]
        with torch.no_grad():
            layer.post_attention_layernorm.weight.fill_(3.0)
            out = m(torch.randint(0, 100, (1, 6)))
            captured = out.next_mems[0]
        # post_attention_layernorm 的 weight 被放大到 3，若 mems 來自它，RMS 會明顯偏大
        self.assertLess(captured.pow(2).mean().sqrt().item(), 2.0)


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
