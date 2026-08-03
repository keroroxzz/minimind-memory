"""C 層 G1 的 invariants（`design_c_layer.md` §4）。

直接跑：`python test/test_memory_module.py`
"""
import os
import sys

import torch

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from model.memory_module import (LATENT_DIM, PERM_N, ActiveWorkspace, LatentSlotAdapter,
                                 LatentSlotsDelivery, LatentStore, MemoryEntry,
                                 OracleMemoryInterface, SyntheticKVAdapter,
                                 SyntheticKVDelivery, latent_to_perm, perm_to_latent)
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

BACKBONE = dict(hidden_size=512, num_hidden_layers=8, num_attention_heads=8,
                num_key_value_heads=2, vocab_size=6400, max_position_embeddings=1024)
ARCH = dict(use_engram=False, use_dense_attention=False, num_loops=2)

_pass = _fail = 0


def check(name, cond, detail=""):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ✅ {name}")
    else:
        _fail += 1
        print(f"  ❌ {name}   {detail}")


# ---------------------------------------------------------------- latent 無損

def test_latent_lossless():
    print("\nlatent 必須資訊完備（規格 §1：否則失敗無法歸因）")
    import itertools
    ok = all(latent_to_perm(perm_to_latent(list(p))) == list(p)
             for p in itertools.permutations(range(PERM_N)))
    check("全部 120 個 S₅ 置換皆可無損還原", ok)
    check(f"latent 維度 = {LATENT_DIM}", perm_to_latent([0, 1, 2, 3, 4]).shape == (LATENT_DIM,))


# ---------------------------------------------------------------- invariant 1

def test_bit_compatible_when_disabled():
    print("\ninvariant 1：關閉記憶時與現有模型 bit-compatible")
    torch.manual_seed(0)
    m = MiniMindForCausalLM(MiniMindConfig(**BACKBONE, **ARCH)).eval()
    ids = torch.randint(0, 6400, (2, 16))
    with torch.no_grad():
        a = m(ids).logits
        b = m(ids).logits
    check("同輸入兩次前向逐位元相同", torch.equal(a, b))
    # ⚠️ 下面這條目前是**同義反覆**（Codex 指出）：它只證明「建立一個尚未接線的
    #    物件不影響輸出」。真正的 bit-compat 要在接進 model 之後，
    #    用 config-off + 舊 checkpoint 做逐位元比對。接線完成後補。
    # 建立記憶模組但不交付 —— 不得改變 core 的輸出
    store = LatentStore()
    store.commit([MemoryEntry("f0", perm_to_latent([1, 0, 2, 3, 4]))])
    _ = OracleMemoryInterface(store)
    with torch.no_grad():
        c = m(ids).logits
    check("建立 store/interface 後輸出逐位元不變", torch.equal(a, c))


# ---------------------------------------------------------------- invariant 2

def test_capacity_does_not_change_core():
    print("\ninvariant 2：store 容量改變不改 core 參數量（§6「可獨立擴容」的操作型定義）")
    torch.manual_seed(0)
    m = MiniMindForCausalLM(MiniMindConfig(**BACKBONE, **ARCH))
    n0 = sum(p.numel() for p in m.parameters())
    store = LatentStore()
    for i in range(1000):
        store.commit([MemoryEntry(f"k{i}", torch.randn(LATENT_DIM))])
    n1 = sum(p.numel() for p in m.parameters())
    check(f"store 0 → {len(store)} 條，core 參數不變（{n0}）", n0 == n1)
    ad = LatentSlotAdapter(512)
    na = sum(p.numel() for p in ad.parameters())
    check(f"delivery adapter 參數獨立計（{na/1e6:.3f}M），不隨容量成長",
          na == sum(p.numel() for p in LatentSlotAdapter(512).parameters()))


# ---------------------------------------------------------------- invariant 3/4

def test_update_visible_and_commit_detaches():
    print("\ninvariant 3：entry update 立即可見；invariant 4：commit 切斷 graph")
    store = LatentStore()
    store.commit([MemoryEntry("f0", perm_to_latent([0, 1, 2, 3, 4]))])
    v0 = store.read(["f0"])[0].version
    store.commit([MemoryEntry("f0", perm_to_latent([1, 0, 2, 3, 4]))])
    e = store.read(["f0"])[0]
    check("更新後同一 episode 內讀得到新值", latent_to_perm(e.latent) == [1, 0, 2, 3, 4])
    check(f"version 遞增（{v0} → {e.version}）", e.version > v0)

    leaf = torch.zeros(LATENT_DIM, requires_grad=True)
    store.commit([MemoryEntry("g0", leaf * 2)])
    stored = store.read(["g0"])[0].latent
    check("commit 後的 latent 不帶 grad（跨 episode 切斷）",
          not stored.requires_grad and stored.grad_fn is None)


# ---------------------------------------------------------------- invariant 5

def test_empty_result_is_explicit():
    print("\ninvariant 5：查不到時要有明確的空結果，不可靜默回垃圾")
    store = LatentStore()
    store.commit([MemoryEntry("f0", perm_to_latent([0, 1, 2, 3, 4]))])
    iface = OracleMemoryInterface(store)
    entries, support = iface.select_many(["f0", "f9", "f0"])
    check("缺項回 None", entries[1] is None and entries[0] is not None)
    check("support 標出缺項", support.tolist() == [1.0, 0.0, 1.0])
    check("有序且可重複（表達 composition chain）",
          len(entries) == 3 and latent_to_perm(entries[0].latent) == latent_to_perm(entries[2].latent))


def test_read_is_a_real_snapshot():
    print("\nread() 必須是真 snapshot —— 呼叫端改副本不得污染 store")
    store = LatentStore()
    store.commit([MemoryEntry("f0", perm_to_latent([0, 1, 2, 3, 4]), {"src": "a"})])
    e = store.read(["f0"])[0]
    e.latent.zero_(); e.metadata["src"] = "TAMPERED"
    again = store.read(["f0"])[0]
    check("就地改 latent 不影響 store", latent_to_perm(again.latent) == [0, 1, 2, 3, 4])
    check("就地改 metadata 不影響 store", again.metadata["src"] == "a")


# ---------------------------------------------------------------- invariant 6

def test_delivery_interchangeable():
    print("\ninvariant 6：三種 Delivery 可互換 —— architecture/count 不變")
    torch.manual_seed(0)
    m = MiniMindForCausalLM(MiniMindConfig(**BACKBONE, **ARCH))
    n_core = sum(p.numel() for p in m.parameters())
    ws = ActiveWorkspace(hidden=torch.randn(2, 6, 512), kv=None)
    lat = torch.stack([perm_to_latent([1, 0, 2, 3, 4]), perm_to_latent([0, 2, 1, 3, 4])])
    lat = lat.unsqueeze(0).expand(2, -1, -1)
    mask = torch.ones(2, 2, dtype=torch.bool)

    d1 = LatentSlotsDelivery(LatentSlotAdapter(512))
    w1 = d1.apply(ws, lat, mask)
    check("LatentSlots 產生 carriers (B,k,H)", w1.carriers.shape == (2, 2, 512))

    d2 = SyntheticKVDelivery(SyntheticKVAdapter(8, 2, 64))
    w2 = d2.apply(ws, lat, mask)
    check("SyntheticKV 產生 8 層 KV", len(w2.kv) == 8)
    check("每層 K/V 形狀為 (B,k,n_kv,head_dim)",
          w2.kv[0][0].shape == (2, 2, 2, 64) and w2.kv[0][1].shape == (2, 2, 2, 64))
    check("切換 delivery 後 core 參數量不變", sum(p.numel() for p in m.parameters()) == n_core)
    check("兩者皆標記為 latent path（InlineTokens 不是）",
          d1.is_latent_path and d2.is_latent_path)


# ---------------------------------------------------------------- 梯度邊界

def test_synthetic_kv_full_forward():
    """shape-only 測不出 blocker：cache 條目數是 layers × num_loops。

    先前 adapter 只產生 8 個，loop2 需要 16 —— 第二圈會索引越界，
    而只檢查 shape 的測試完全看不到。這裡跑**完整的 core forward**。
    """
    print("\nSyntheticKV 必須能通過完整的 core forward（不只 shape）")
    torch.manual_seed(0)
    for n_loops in (1, 2):
        cfg = dict(ARCH); cfg["num_loops"] = n_loops
        m = MiniMindForCausalLM(MiniMindConfig(**BACKBONE, **cfg)).eval()
        ad = SyntheticKVAdapter(8, 2, 64, num_loops=n_loops)
        lat = torch.stack([perm_to_latent([1, 0, 2, 3, 4])]).unsqueeze(0).expand(2, -1, -1)
        kv = SyntheticKVDelivery(ad).apply(
            ActiveWorkspace(kv=None), lat, torch.ones(2, 1, dtype=torch.bool)).kv
        check(f"num_loops={n_loops}：產生 {len(kv)} 個 cache 條目（需 {8*n_loops}）",
              len(kv) == 8 * n_loops)
        ids = torch.randint(0, 6400, (2, 5))
        try:
            with torch.no_grad():
                out = m(ids, past_key_values=kv, use_cache=True)
            ok, why = out.logits.shape == (2, 5, 6400), ""
        except Exception as ex:
            ok, why = False, f"{type(ex).__name__}: {ex}"
        check(f"num_loops={n_loops}：完整 forward 通過", ok, why)


def test_rope_matches_core_exactly():
    """adapter 的旋轉必須與 core 的 `apply_rotary_pos_emb` 逐位元相同。

    adapter 自行重算只覆蓋 default rope_base；core 若用自訂 theta / YaRN，
    「同樣的旋轉」就不成立（Codex）。所以正解是**吃 core 的 freqs buffer**。
    """
    print("\nRoPE 必須與 core 逐位元一致（不是自己重算一份近似的）")
    from model.model_minimind import apply_rotary_pos_emb
    torch.manual_seed(0)
    m = MiniMindForCausalLM(MiniMindConfig(**BACKBONE, **ARCH))
    ad = SyntheticKVAdapter(8, 2, 64, num_loops=1)
    k = torch.randn(2, 3, 2, 64)
    cos, sin = m.model.freqs_cos[:3], m.model.freqs_sin[:3]
    mine = ad._rope(k, cos, sin)
    _, theirs = apply_rotary_pos_emb(k.clone(), k.clone(), cos, sin, cos, sin)
    check("吃 core freqs 時與 apply_rotary_pos_emb 逐位元相同", torch.equal(mine, theirs))
    fb_cos, fb_sin = ad._fallback_freqs(3, k.device)
    check("fallback 與 core 預設 freqs 數值一致（rope_base 相同時）",
          torch.allclose(fb_cos, cos, atol=1e-5))


def test_synthetic_kv_backward():
    """cache 注入路徑必須真的可訓 —— 先前梯度測試只覆蓋 LatentSlotAdapter。"""
    print("\nSyntheticKV 端到端反向：core 凍結、只有 adapter 收梯度")
    torch.manual_seed(0)
    m = MiniMindForCausalLM(MiniMindConfig(**BACKBONE, **ARCH))
    for p_ in m.parameters():
        p_.requires_grad_(False)
    ad = SyntheticKVAdapter(8, 2, 64, num_loops=ARCH["num_loops"])
    lat = perm_to_latent([1, 0, 2, 3, 4]).view(1, 1, -1).expand(2, 2, -1)
    kv = SyntheticKVDelivery(ad).apply(
        ActiveWorkspace(kv=None), lat, torch.ones(2, 2, dtype=torch.bool),
        freqs=(m.model.freqs_cos, m.model.freqs_sin)).kv
    ids = torch.randint(0, 6400, (2, 5))
    out = m(ids, past_key_values=kv, use_cache=True)
    out.logits.float().pow(2).mean().backward()
    gs = [p_.grad for p_ in ad.parameters()]
    check("adapter 全部參數收到梯度", all(g is not None for g in gs))
    check("梯度為有限值且非全零",
          all(torch.isfinite(g).all() for g in gs) and any(g.abs().sum() > 0 for g in gs))
    check("core 參數完全沒有梯度（凍結）", all(p_.grad is None for p_ in m.parameters()))


def test_nested_metadata_snapshot():
    print("\nmetadata 是 nested 時也不可污染 store（shallow copy 不夠）")
    store = LatentStore()
    store.commit([MemoryEntry("f0", perm_to_latent([0, 1, 2, 3, 4]), {"tags": {"a": 1}})])
    e = store.read(["f0"])[0]
    e.metadata["tags"]["a"] = 999
    check("改 nested metadata 不影響 store", store.read(["f0"])[0].metadata["tags"]["a"] == 1)


def test_kv_merge_length_guard():
    print("\nKV 合併前必須檢查長度，zip 會靜默截短")
    ad = SyntheticKVAdapter(8, 2, 64, num_loops=1)
    d = SyntheticKVDelivery(ad)
    lat = perm_to_latent([1, 0, 2, 3, 4]).view(1, 1, -1)
    short = [(torch.randn(1, 2, 2, 64), torch.randn(1, 2, 2, 64))] * 4    # 只有 4 層
    try:
        d.apply(ActiveWorkspace(kv=short), lat, torch.ones(1, 1, dtype=torch.bool))
        ok = False
    except AssertionError:
        ok = True
    check("長度不符時當場 assert 失敗，不靜默截短", ok)


def test_delivery_is_nn_module():
    print("\nDelivery 必須是 nn.Module —— 否則 adapter 進不了 state_dict/optimizer/.to()")
    d1 = LatentSlotsDelivery(LatentSlotAdapter(512))
    d2 = SyntheticKVDelivery(SyntheticKVAdapter(8, 2, 64))
    check("LatentSlotsDelivery 是 nn.Module", isinstance(d1, torch.nn.Module))
    check("SyntheticKVDelivery 是 nn.Module", isinstance(d2, torch.nn.Module))
    check("adapter 參數出現在 state_dict", any("adapter" in k for k in d1.state_dict()))
    check("adapter 參數可被 optimizer 看到", len(list(d1.parameters())) > 0)


def test_gradient_boundaries():
    print("\n梯度邊界（規格 §3）：唯一可學的是 delivery")
    store = LatentStore()
    store.commit([MemoryEntry("f0", perm_to_latent([1, 0, 2, 3, 4]))])
    iface = OracleMemoryInterface(store)
    entries, support = iface.select_many(["f0"])
    check("oracle selection 無梯度", not support.requires_grad)
    check("store 內容凍結（非 leaf、不可訓）", not entries[0].latent.requires_grad)

    ad = LatentSlotAdapter(512)
    lat = entries[0].latent.unsqueeze(0).unsqueeze(0)
    out = ad(lat)
    check("delivery 輸出可回傳梯度", out.requires_grad)
    out.sum().backward()
    check("adapter 參數收到梯度", all(p.grad is not None for p in ad.parameters()))


if __name__ == "__main__":
    print("=" * 60)
    print("  C 層 G1 invariants（design_c_layer.md §4）")
    print("=" * 60)
    test_latent_lossless()
    test_bit_compatible_when_disabled()
    test_capacity_does_not_change_core()
    test_update_visible_and_commit_detaches()
    test_empty_result_is_explicit()
    test_read_is_a_real_snapshot()
    test_delivery_interchangeable()
    test_nested_metadata_snapshot()
    test_delivery_is_nn_module()
    test_kv_merge_length_guard()
    test_rope_matches_core_exactly()
    test_synthetic_kv_full_forward()
    test_synthetic_kv_backward()
    test_gradient_boundaries()
    print("\n" + "=" * 60)
    print(f"  通過 {_pass} / 失敗 {_fail}")
    print("=" * 60)
    sys.exit(1 if _fail else 0)
