"""`BR-G3c-D` —— dangling address 的**橋接版 transfer verification**（Codex [158] 回覆鎖定）。

**地位先講清楚**：S₅ 的 `G3c` storage-fault 已在 `research.md` §4.35 **SEALED**。
本檔**不是**新研究發現，是把**同一條安全契約**搬到 bridge 的
`Store → latent-delivery → core` 路徑做**有界驗證**。一次做完，PASS 即 seal，
不擴成 pool sweep／torn write／stale snapshot。

**零訓練。** core、resolver、threshold、schema、pool size 一律不動。
固定 `pool=24`、`n=300`。

### 故障（唯一）

`store.dangle(key)` → `contains(key)=True` 但 `read(key)=None`。
guard log **必須**是 `badread`；**不得**偷變成普通 `absent`
（那會讓這個實驗測到別的東西）。

### Primary gate（事前鎖死）

| | 門檻 |
|---|---|
| dangling 300/300 | `status=badread`、**0 注入／0 交付／0 作答**、`halluc=0`（單側 95% UB 0.99%） |
| healthy 配對 300/300 | `status=ok`、`false_abstain=0`，且 `z`／delivery tensor／decode **逐例與無故障 store 完全相同** |

healthy 那一列是**排除退化解**用的 —— 一個永遠 abstain 的 guard 也能讓 halluc=0。

### fault-fidelity（必要欄）

每個 victim 在 guard 之前記錄 `contains=True, read=None`，
並 assert **沒有任何 fallback／zero latent 被送進 injector**。

`unprotected shadow` 只報「會嘗試對缺內容 entry 交付」的次數，
**不合成假內容再把它的答案叫作自然幻覺** —— 這個故障的安全違反
**就是「交付不存在的內容」這件事本身**（Codex [158]）。
"""
import json, os, random, sys
import torch
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer
import bridge_latent_schema as S
import bridge_renderer as B
import bridge_store as ST
from bridge_latent_gate import gen
from bridge_train_core import DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))
POOL, N = 24, 300


def cp_upper(k, n, conf=0.95):
    """單側 95% Clopper–Pearson 上界；`k=0` 時是 `1 - (1-conf)**(1/n)`。"""
    if k == 0:
        return 1.0 - (1.0 - conf) ** (1.0 / n)
    from math import isclose
    lo, hi = 0.0, 1.0
    for _ in range(200):                       # 二分，夠用且無 scipy 依賴
        mid = (lo + hi) / 2
        s = sum(__import__("math").comb(n, i) * mid ** i * (1 - mid) ** (n - i)
                for i in range(0, k))
        if s > 1 - conf:
            lo = mid
        else:
            hi = mid
    return hi


class Injector:
    """交付的**唯一入口**。呼叫次數即「有沒有把東西送進模型」。"""

    def __init__(self, proj):
        self.proj, self.calls = proj, 0

    def __call__(self, lat):
        assert lat is not None, "fallback／zero latent 不得進入 injector"
        assert torch.isfinite(lat).all() and lat.abs().sum() > 0, "空 latent 不得交付"
        self.calls += 1
        return self.proj(lat.to(DEVICE)).unsqueeze(0)


@torch.no_grad()
def one(m, tok, inj, loops, store, tgt, dis, tv, dv):
    """走一次權威路徑。回傳 (status, z, delivery tensor, decode 字串)。"""
    z, status = ST.retrieve(store, *tgt)
    if status != "ok":
        return status, None, None, None                    # **fail closed**：不注入
    facts = [(tgt[0], tgt[1], tv), (dis[0], dis[1], dv)]
    ep = B.Episode(facts, f"{tgt[0]} {B.ATTRS[tgt[1]]} 是 多少", B.val_str(tv), [0], 0)
    lat = torch.stack([z, store.read(*dis)])
    mc = inj(lat)
    ids = torch.tensor(tok(tok.bos_token + S.render_latent(ep)[0],
                           add_special_tokens=False).input_ids)
    return status, z, mc, gen(m, tok, ids, loops, mc)


def main(core="bridge_core_latent.pth", n=N):
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    blob = torch.load(os.path.join(HERE, core), map_location="cpu")
    arch = dict(blob["arch"])
    m = MiniMindForCausalLM(MiniMindConfig(vocab_size=blob["vocab"], **arch)).to(DEVICE).eval()
    m.load_state_dict(blob["model"])
    loops = arch["num_loops"]
    proj = S.CarrierProj(arch["hidden_size"]).to(DEVICE)
    proj.scale.fill_(blob["carrier_scale"])

    print(f"  BR-G3c-D  dangling transfer verification   凍結 {core}（**零訓練**）")
    print(f"  prereg: Codex [158]；pool={POOL} 固定，n={n}")
    print("  ⚠️ 這是 S₅ §4.35 已 SEALED 契約的**橋接版轉移驗證**，不是新研究發現\n")

    rng = random.Random(31337)
    allp = [(nm, ai) for nm in B.NAMES for ai in range(len(B.ATTRS))]
    desc = rng.sample(allp, POOL)
    vals = {k: rng.randint(B.VMIN, B.VMAX) for k in desc}

    # 乾淨 store：healthy 參照就從它算，之後逐位元比對
    clean = ST.Store()
    for k in desc:
        clean.commit(k[0], k[1], vals[k])
    assert clean.verify() == 0, "乾淨 store 契約自檢不過"

    inj = Injector(proj)
    st = {"badread": 0, "absent": 0, "wrongkey": 0, "ok": 0}
    fidelity = {"contains_true": 0, "read_none": 0}
    dang_delivered = dang_answered = 0
    healthy_ok = healthy_same = false_ab = 0
    shadow_unsafe = 0
    mism = []

    for i in range(n):
        victim = desc[rng.randrange(POOL)]
        others = [k for k in desc if k != victim]
        dis = others[rng.randrange(len(others))]
        # healthy 配對：**另一個** entry 當 target，全程不注入故障
        # healthy 配對必須**完全避開 victim**（含 distractor）——
        # 否則 healthy 那一列自己就讀到壞內容，測到的就不是「故障不外溢」了
        hv = others[rng.randrange(len(others))]
        hcand = [k for k in others if k != hv]
        hdis = hcand[rng.randrange(len(hcand))]

        # victim 必須是「正常情況下 retrieve 會 ok」的 query target（Codex [158]）
        assert ST.retrieve(clean, *victim)[1] == "ok"

        # ---- healthy 參照（乾淨 store）
        c0 = inj.calls
        r_st, r_z, r_mc, r_out = one(m, tok, inj, loops, clean, hv, hdis,
                                     vals[hv], vals[hdis])
        assert inj.calls == c0 + 1

        # ---- 注入故障：每題一個獨立 store，避免故障累積成別的實驗
        faulted = ST.Store()
        for k in desc:
            faulted.commit(k[0], k[1], vals[k])
        faulted.dangle(*victim)

        # fault-fidelity：**guard 之前**先記錄，證明故障確實是 dangling 而非 absent
        fidelity["contains_true"] += int(faulted.contains(*victim))
        fidelity["read_none"] += int(faulted.read(*victim) is None)

        # unprotected shadow：沒有 typed guard 時只看「索引有沒有」就往下交付
        if faulted.contains(*victim):
            shadow_unsafe += 1        # 會對**缺內容**的 entry 嘗試交付 —— 這就是安全違反

        c1 = inj.calls
        d_st, _, _, d_out = one(m, tok, inj, loops, faulted, victim, dis,
                                vals[victim], vals[dis])
        st[d_st] = st.get(d_st, 0) + 1
        dang_delivered += int(inj.calls > c1)
        dang_answered += int(d_out is not None)

        # ---- healthy 在**同一個故障 store** 上必須逐位元不變
        c2 = inj.calls
        h_st, h_z, h_mc, h_out = one(m, tok, inj, loops, faulted, hv, hdis,
                                     vals[hv], vals[hdis])
        healthy_ok += int(h_st == "ok")
        false_ab += int(h_st != "ok")
        same = (h_st == r_st == "ok"
                and torch.equal(h_z, r_z) and torch.equal(h_mc, r_mc)
                and h_out == r_out)
        healthy_same += int(same)
        if not same and len(mism) < 5:
            mism.append({"i": i, "st": [r_st, h_st], "out": [r_out, h_out]})
        assert inj.calls == c2 + 1

    ub = cp_upper(0, n) if dang_answered == 0 else cp_upper(dang_answered, n)
    print(f"  {f'dangling {n} 題':<28s}")
    print(f"    guard status              {st}")
    print(f"    fault fidelity            contains=True {fidelity['contains_true']}/{n}   "
          f"read=None {fidelity['read_none']}/{n}")
    print(f"    交付次數（injector 呼叫）  {dang_delivered}/{n}")
    print(f"    作答次數                  {dang_answered}/{n}")
    print(f"    halluc（讀到壞內容仍作答）  {dang_answered}/{n}   單側95% UB {ub:.2%}")
    print(f"    unprotected shadow        {shadow_unsafe}/{n} 會對缺內容 entry 嘗試交付")
    print(f"\n  {f'healthy 配對 {n} 題':<28s}")
    print(f"    status=ok                 {healthy_ok}/{n}")
    print(f"    false_abstain             {false_ab}/{n}")
    print(f"    與無故障 store 逐位元相同   {healthy_same}/{n}（z／delivery tensor／decode）")
    if mism:
        print(f"    前幾筆不符：{mism}")

    ok_d = (st.get("badread", 0) == n and dang_delivered == 0 and dang_answered == 0)
    ok_h = (healthy_ok == n and false_ab == 0 and healthy_same == n)
    print()
    if ok_d and ok_h:
        print("  → **BR-G3c-D PASS**：dangling 全數 fail closed 且未交付；")
        print("    healthy 路徑逐位元不受影響（排除「guard 永遠擋」的退化解）。")
        print("    依 prereg **seal**，不擴成 pool sweep／torn／stale 線。")
    else:
        print(f"  → **BR-G3c-D FAIL**（dangling {'✓' if ok_d else '✗'} / "
              f"healthy {'✓' if ok_h else '✗'}）")
        print("    依 prereg：保留原始違規紀錄，修正僅限實作已預先指定的")
        print("    `badread → fail-closed` contract，並以完全同一 episodes/spec 重跑；")
        print("    **不得**變故障、樣本、門檻，或加入 learned support 補救。")
    json.dump({"core": core, "n": n, "pool": POOL, "status": st,
               "fault_fidelity": fidelity, "delivered": dang_delivered,
               "answered": dang_answered, "halluc_ub": ub,
               "shadow_unsafe": shadow_unsafe,
               "healthy": {"ok": healthy_ok, "false_abstain": false_ab,
                           "bitwise_same": healthy_same},
               "pass": bool(ok_d and ok_h)},
              open(os.path.join(HERE, "results_bridge_g3c_dangling.json"), "w"),
              indent=2, ensure_ascii=False)
    print("  -> results_bridge_g3c_dangling.json")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "bridge_core_latent.pth",
         int(sys.argv[2]) if len(sys.argv) > 2 else N)
