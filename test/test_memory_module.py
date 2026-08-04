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
                                 SyntheticKVDelivery, TiedAddressEncoder,
                                 latent_to_perm, perm_to_latent)
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

BACKBONE = dict(hidden_size=512, num_hidden_layers=8, num_attention_heads=8,
                num_key_value_heads=2, vocab_size=6400, max_position_embeddings=1024)
# ⚠️ **必須顯式開 `use_looped_transformer`**。只給 `num_loops=2` 時，
#    model 內部會 `if not looped: num_loops = 1` —— 迴圈根本沒開，
#    於是「16 個 cache 條目」的測試會空過（模型只用前 8 個，其餘靜默忽略）。
#    與 CLAUDE.md 記過的 `use_engram` 預設陷阱同一個形狀：**顯式釘住每個 flag**。
LOOP_KW = dict(use_looped_transformer=True, loop_adapter="shared",
               loop_input_injection=True, loop_index_embed=True)
ARCH = dict(use_engram=False, use_dense_attention=False, num_loops=2, **LOOP_KW)

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
        # 固定 assert config 狀態 —— 只寫 num_loops 而沒開 use_looped_transformer 時，
        # model 會強制 num_loops=1，於是整條測試空過（實際發生過）。
        assert cfg["use_looped_transformer"] and cfg["num_loops"] == n_loops
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
        # 模型真的用掉全部條目了嗎？只檢查「不崩」會讓多餘的條目被靜默忽略。
        with torch.no_grad():
            used = m(ids, past_key_values=kv, use_cache=True).past_key_values
        check(f"num_loops={n_loops}：模型回傳 {len(used)} 個 cache 條目（需 {8*n_loops}）",
              len(used) == 8 * n_loops)


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


def test_per_position_scale_and_runtime_rms():
    """scale 必須是 layers×loops 個位置，不是 layers 個。

    實測 native V 同層跨 loop 也不同（L1：loop0 0.42 → loop1 0.87），
    用 8 個 scale 重複到 16 個位置等於隱含 recurrent sharing 卻沒寫明（Codex）。
    """
    print("\nSyntheticKV 的 scale 必須逐注入位置（layers×loops），且 forward 實測 RMS")
    # 實測值（synth_absent_loop2 ckpt），loop0 8 個 + loop1 8 個
    tgt_v = [1.71, 0.42, 1.03, 1.47, 1.49, 2.03, 2.09, 2.01,
             1.67, 0.87, 1.16, 1.31, 1.39, 1.72, 2.02, 1.75]
    tgt_k = [1.46, 1.43, 1.42, 1.36, 1.42, 1.37, 1.33, 1.46,
             1.39, 1.36, 1.33, 1.31, 1.38, 1.38, 1.35, 1.45]
    ad = SyntheticKVAdapter(8, 2, 64, num_loops=2, scale_k=tgt_k, scale_v=tgt_v)
    lat = perm_to_latent([1, 0, 2, 3, 4]).view(1, 1, -1).expand(4, 3, -1)
    slots = ad(lat)
    check(f"注入位置數 = layers×loops = 16", len(slots) == 16 and ad.n_slots == 16)
    check("scale 為 16 個值，且同層跨 loop 可不同",
          ad.scale_v.shape == (16,) and float(ad.scale_v[1]) != float(ad.scale_v[9]))
    check("forward 後有實測 RMS（不是只靠初始化假設）", ad.last_rms is not None)
    check("實測值為有限", ad.last_rms["finite"])
    check("last_rms 已 detach（診斷欄位不得持有 autograd graph）",
          not any(t.requires_grad for t in (ad.last_rms["K"], ad.last_rms["V"],
                                            ad.last_rms["ratio_K"], ad.last_rms["ratio_V"])))
    # ⚠️ 只看 CV 會漏掉「所有位置一起偏高/偏低」。逐位置 assert 比值落在容差內，並報 min/max。
    for nm in ("ratio_K", "ratio_V"):
        r = ad.last_rms[nm]
        lo, hi = float(r.min()), float(r.max())
        check(f"{nm} 逐位置皆落在 [0.5, 2.0]（min {lo:.3f} / max {hi:.3f}）",
              0.5 <= lo and hi <= 2.0)
    # loop0 與 loop1 要分開報，不能用 aggregate 掩掉偏差
    r = ad.last_rms["ratio_V"]
    print(f"     ratio_V  loop0 mean {r[:8].mean():.3f}   loop1 mean {r[8:].mean():.3f}")
    # 幅度被釘死是架構約束 —— near-zero 輸入不得產生 NaN/Inf
    z = ad(torch.zeros(2, 3, LATENT_DIM))
    check("exact-zero latent 不產生 NaN/Inf", all(torch.isfinite(t).all() for kv in z for t in kv))
    # normalize-then-scale 會讓全零輸入產生**滿幅度** KV（實測 RMS = target）——
    # 那是憑 scale 生成假記憶。缺項必須真的是零（Codex）。
    check("**exact-zero latent 產生全零 KV**（不得憑 scale 生成假記憶）",
          all(float(t.abs().max()) == 0.0 for kv in z for t in kv))
    half = ad(torch.stack([torch.zeros(3, LATENT_DIM),
                           perm_to_latent([1, 0, 2, 3, 4]).view(1, -1).expand(3, -1)]))
    check("同 batch 內只有非零那筆有輸出（逐樣本遮罩）",
          float(half[0][0][0].abs().max()) == 0.0 and float(half[0][0][1].abs().max()) > 0)
    e = torch.full((2, 3, LATENT_DIM), 1e-7, requires_grad=True)
    o2 = ad(e)
    sum(kk.pow(2).mean() + vv.pow(2).mean() for kk, vv in o2).backward()
    check(f"near-zero(1e-7) backward 有限且有界（|g|max {e.grad.abs().max():.2e}）",
          bool(torch.isfinite(e.grad).all()) and float(e.grad.abs().max()) < 1e3)


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
    named = list(ad.named_parameters())
    gs = [p_.grad for _, p_ in named]
    check("adapter 全部參數收到梯度", all(g is not None for g in gs))
    check("全部梯度皆為有限值", all(torch.isfinite(g).all() for g in gs))
    # 先前寫 `any(nonzero)` 卻宣稱「全部非零」—— 那樣早層被 starve 完全測不到（Codex）。
    zero = [n for (n, _), g in zip(named, gs) if g.abs().sum() == 0]
    check("**每一個** 參數的梯度都非零（排除早層 starve）", not zero, f"全零：{zero}")
    first = ad.net[0].weight.grad
    check("第一層 Linear 權重梯度非零（梯度真的傳回輸入端）", first.abs().sum() > 0)
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


def test_tied_encoder_is_order_aware():
    """G2c：pooling 不得抹掉 token 順序。

    canonical key span 有 2~3 個 token，而 `f34`=[341,54,55] 與 `f43`=[341,55,54]
    是 anagram —— mean pooling 下 |cos|=1.000，身分在池化階段就死了
    （experiments/g2c_identity_gate.py 關 3 實測）。
    """
    print("\n\nG2c tied encoder（g2c_identity_gate 關 3/關 4）：")
    torch.manual_seed(0)
    enc = TiedAddressEncoder(in_dim=8, addr_dim=6, width=16)
    e = torch.randn(3, 8)                                  # 三個 token 的 embedding
    a = torch.stack([e[0], e[1], e[2]]).unsqueeze(0)       # " f34"
    b = torch.stack([e[0], e[2], e[1]]).unsqueeze(0)       # " f43"，同集合、順序相反
    za, zb = enc(a), enc(b)
    cos = (za * zb).sum().abs().item()
    check("anagram span 不得產生同一 address", cos < 0.999, f"|cos|={cos:.4f}")

    m = torch.tensor([[True, True, True]])
    w = enc.pos_logit[:3].masked_fill(~m[0], float("-inf")).softmax(-1)
    check("位置權重初始化為遞增斜坡（非 mean）", bool((w[1] > w[0]) and (w[2] > w[1])),
          f"w={w.tolist()}")


def test_tied_encoder_masks_padding():
    """短 span 補到同長後，padding 位置不得影響 address（否則長度洩漏成身分）。"""
    torch.manual_seed(0)
    enc = TiedAddressEncoder(in_dim=8, addr_dim=6, width=16)
    e = torch.randn(2, 8)
    two = torch.stack([e[0], e[1], torch.randn(8)]).unsqueeze(0)      # 第三格是垃圾
    two2 = torch.stack([e[0], e[1], torch.randn(8) * 99]).unsqueeze(0)  # 換成別的垃圾
    m = torch.tensor([[True, True, False]])
    z1, z2 = enc(two, m), enc(two2, m)
    check("masked padding 不影響 address", torch.allclose(z1, z2, atol=1e-6),
          f"max|diff|={(z1 - z2).abs().max().item():.2e}")


def test_tied_encoder_is_actually_tied():
    """address 與 query 必須是同一組權重 —— 否則兩塔可以漂成私有暗號。"""
    torch.manual_seed(0)
    enc = TiedAddressEncoder(in_dim=8, addr_dim=6, width=16)
    span = torch.randn(1, 3, 8)
    a = enc(span)                       # write 側
    q = enc(span)                       # query 側，同一個 module
    check("同一 span 兩側產生完全相同的向量", torch.equal(a, q))
    check("address 為單位長度", abs(a.norm().item() - 1.0) < 1e-5)


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
    test_per_position_scale_and_runtime_rms()
    test_synthetic_kv_backward()
    test_gradient_boundaries()
    test_tied_encoder_is_order_aware()
    test_tied_encoder_masks_padding()
    test_tied_encoder_is_actually_tied()
    print("\n" + "=" * 60)
    print(f"  通過 {_pass} / 失敗 {_fail}")
    print("=" * 60)
    sys.exit(1 if _fail else 0)
