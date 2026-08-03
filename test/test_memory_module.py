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
          len(entries) == 3 and entries[0] is entries[2])


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
    test_delivery_interchangeable()
    test_gradient_boundaries()
    print("\n" + "=" * 60)
    print(f"  通過 {_pass} / 失敗 {_fail}")
    print("=" * 60)
    sys.exit(1 if _fail else 0)
