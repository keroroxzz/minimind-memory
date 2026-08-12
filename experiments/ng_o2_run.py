"""`NG-O2` 三-seed train/eval —— **一次跑完,跑完直接照 prereg 判讀**。

授權：Codex [172]（`NGO2_prereg.json` v4）。

- **train stream 一份、固定順序**,三個 seed 共用;`seed` **只改 model init 與訓練 RNG**。
- **eval artifact 一份、單一 checksum**,三個最終 checkpoint 在**逐位相同**的輸入上量。
- **`n=1` 單一 target carrier**,train 與 eval 皆然。
- **checkpoint 只用最終步**,不挑點、不早停、不做 model selection。

O1 繞過 Store;O2 走 Store;O3 比較兩條路徑的 readback／delivery bitwise 與各自 decode。
**三者不得互相補過。**
"""
import json, os, random, sys, time
import torch
import torch.nn.functional as F
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer
import ng_o2 as N
from bridge_latent_gate import wilson
from ng_o2_phase0 import build_store
from bridge_train_core import DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))
P = json.load(open(os.path.join(HERE, "NGO2_prereg.json")))
T = P["training_spec_frozen"]
ARCH = dict(hidden_size=512, num_hidden_layers=8, num_attention_heads=8,
            num_key_value_heads=2, use_engram=False, use_moe=False,
            use_dense_attention=False, use_latent_attention=False,
            use_recurrence=False, use_looped_transformer=True, num_loops=4)
TRAIN_SEED = 20260812


def train_rows(n):
    """**固定順序**的 train stream。決定性產生,不落盤;以 fingerprint 證明同一性。"""
    rng = random.Random(TRAIN_SEED + 1)
    for _ in range(n):
        m = 2 if rng.random() < 0.5 else 3
        b = N.base_episode(rng, m)
        yield N.variant(b, rng.randrange(m), rng)


def train_fingerprint(k=2000):
    import hashlib
    rows = [[r["q_entity"], r["q_attr"], r["answer"], r["target_idx"]]
            for r in train_rows(k)]
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()[:16]


def batch(tok, rows):
    """query 文字 ＋ **一個** carrier;label 只在答案 token 上。"""
    xs, ys, zs, mx = [], [], [], 0
    for r in rows:
        p = tok.bos_token + N.query_text(r["q_entity"], r["q_attr"])
        full = tok(p + " " + N.val_str(r["answer"]) + tok.eos_token,
                   add_special_tokens=False).input_ids
        plen = len(tok(p, add_special_tokens=False).input_ids)
        y = list(full); y[:plen] = [-100] * plen
        xs.append(full); ys.append(y); zs.append(N.zv(r["answer"]))
        mx = max(mx, len(full))
    pad = tok.pad_token_id or 0
    X = torch.tensor([f + [pad] * (mx - len(f)) for f in xs])
    Y = torch.tensor([y + [-100] * (mx - len(y)) for y in ys])
    Z = torch.stack(zs).unsqueeze(1)                 # (bs, 1, ZV_DIM) —— n=1
    return X, Y, Z


def loss_of(m, proj, X, Y, Z):
    mc = proj(Z)
    logits = m(X, memory_carriers=mc).logits
    Y = torch.cat([Y.new_full((Y.shape[0], mc.shape[1]), -100), Y], dim=1)
    assert logits.shape[1] == Y.shape[1]
    return F.cross_entropy(logits[:, :-1].reshape(-1, logits.size(-1)).float(),
                           Y[:, 1:].reshape(-1), ignore_index=-100)


def train(tok, seed, steps, bs):
    torch.manual_seed(seed)                          # **seed 只改 init 與訓練 RNG**
    cfg = MiniMindConfig(vocab_size=len(tok), **ARCH)
    m = MiniMindForCausalLM(cfg).to(DEVICE)
    proj = N.CarrierProj(ARCH["hidden_size"]).to(DEVICE)
    sc = N.fit_scale(proj, m.get_input_embeddings().weight)
    dec = [p for n_, p in m.named_parameters()
           if p.dim() >= 2 and "embed" not in n_ and "norm" not in n_]
    nod = [p for n_, p in m.named_parameters()
           if p.dim() < 2 or "embed" in n_ or "norm" in n_]
    opt = torch.optim.AdamW([{"params": dec, "weight_decay": 0.01},
                             {"params": nod, "weight_decay": 0.0}],
                            lr=6e-4, betas=(0.9, 0.95))
    sch = torch.optim.lr_scheduler.OneCycleLR(opt, 6e-4, total_steps=steps,
                                              pct_start=0.03)
    stream = train_rows(steps * bs)
    m.train(); t0 = time.time()
    for step in range(1, steps + 1):
        rows = [next(stream) for _ in range(bs)]
        X, Y, Z = batch(tok, rows)
        loss = loss_of(m, proj, X.to(DEVICE), Y.to(DEVICE), Z.to(DEVICE))
        opt.zero_grad(set_to_none=True); loss.backward()
        torch.nn.utils.clip_grad_norm_(m.parameters(), 1.0)
        opt.step(); sch.step()
        time.sleep(0.8 * 0.02)                       # throttle（CLAUDE.md 的標準做法）
        if step % 2000 == 0:
            print(f"      step {step}/{steps} loss={loss.item():.4f} "
                  f"{(time.time()-t0)/60:.0f} min")
    return m.eval(), proj, sc


@torch.no_grad()
def answer(m, tok, proj, z, q):
    """單一 carrier 交付 → 貪婪生成 → normalizer 後整串比對。"""
    ids = torch.tensor(tok(tok.bos_token + q, add_special_tokens=False).input_ids)
    mc = proj(z.unsqueeze(0).unsqueeze(0).to(DEVICE))
    out, past = [], None
    x = ids.unsqueeze(0).to(DEVICE)
    for _ in range(12):
        o = m(x, memory_carriers=mc if past is None else None,
              past_key_values=past, use_cache=True)
        past = o.past_key_values
        nxt = int(o.logits[0, -1].argmax())
        if nxt == tok.eos_token_id:
            break
        out.append(nxt)
        x = torch.tensor([[nxt]], device=DEVICE)
    return N.norm_answer(tok.decode(out, skip_special_tokens=True)), mc


def main():
    art = N.load_eval()
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    fp = train_fingerprint()
    print(f"  NG-O2 三-seed run（Codex [172] 授權）")
    print(f"  eval artifact checksum={art['checksum']}   train fingerprint={fp}")
    print(f"  seeds={T['seeds']}  steps={T['steps']}  bs={T['batch_size']}"
          f"  **n=1 交付、checkpoint 只用最終步**\n")

    out, fails = {}, []
    for seed in T["seeds"]:
        print(f"  ==== seed {seed}")
        m, proj, sc = train(tok, seed, T["steps"], T["batch_size"])
        print(f"    carrier scale={sc:.4f}")
        r = {"scale": sc}

        # ---- O1：繞過 Store
        ok = 0
        for e in art["o1"]:
            a, _ = answer(m, tok, proj, N.zv(e["answer"]),
                          N.query_text("anna", "門號密碼"))
            ok += int(a == N.norm_answer(N.val_str(e["answer"])))
        lo, hi = wilson(ok, len(art["o1"]))
        r["O1"] = [ok, len(art["o1"])]
        o1ok = ok / len(art["o1"]) >= 0.95
        if not o1ok:
            fails.append(f"seed{seed}/O1")
        print(f"    O1（繞過 Store）      {ok}/{len(art['o1'])} = "
              f"{ok/len(art['o1']):.1%} [{lo:.0%},{hi:.0%}]{'' if o1ok else '  ✗'}")

        # ---- O3：direct vs Store round-trip，逐題 bitwise ＋ 各自 decode
        same = d_ok = s_ok = 0
        for e in art["o1"]:
            q = N.query_text("anna", "門號密碼")
            zd = N.zv(e["answer"])
            st = N.NGStore(); st.commit(N.k0("anna", "門號密碼"), zd)
            zs_ = st.read(N.k0("anna", "門號密碼"))
            ad, mcd = answer(m, tok, proj, zd, q)
            as_, mcs = answer(m, tok, proj, zs_, q)
            gt = N.norm_answer(N.val_str(e["answer"]))
            d_ok += int(ad == gt); s_ok += int(as_ == gt)
            same += int(torch.equal(zd, zs_) and torch.equal(mcd, mcs) and ad == as_)
        r["O3"] = {"bitwise_same": same, "direct_ok": d_ok, "store_ok": s_ok,
                   "n": len(art["o1"])}
        o3ok = (same == len(art["o1"]) and d_ok / len(art["o1"]) >= 0.95
                and s_ok / len(art["o1"]) >= 0.95)
        if not o3ok:
            fails.append(f"seed{seed}/O3")
        print(f"    O3（round-trip）      bitwise {same}/{len(art['o1'])}   "
              f"direct {d_ok/len(art['o1']):.1%}   store {s_ok/len(art['o1']):.1%}"
              f"{'' if o3ok else '  ✗'}")

        # ---- O2：五個 store-populated cell
        r["O2"] = {}
        for cell in N.CELLS:
            ok = wrong_rec = 0
            eps = art["cells"][cell]
            for e in eps:
                st = build_store(e["writes"])
                z = st.read(N.k0(e["q_entity"], e["q_attr"]))
                a, _ = answer(m, tok, proj, z,
                              N.query_text(e["q_entity"], e["q_attr"]))
                ok += int(a == N.norm_answer(N.val_str(e["answer"])))
                wrong_rec += int(a == N.norm_answer(N.val_str(e["dis_val"])))
            lo, hi = wilson(ok, len(eps))
            cok = ok / len(eps) >= 0.95
            if not cok:
                fails.append(f"seed{seed}/O2-{cell}")
            print(f"    O2 {cell:>10s}  {ok}/{len(eps)} = {ok/len(eps):>6.1%} "
                  f"[{lo:.0%},{hi:.0%}]  拿錯record {wrong_rec}"
                  f"{'' if cok else '  ✗'}")
            r["O2"][cell] = {"ok": ok, "n": len(eps), "wrong_record": wrong_rec}
        out[str(seed)] = r

    print()
    if fails:
        print(f"  → **NG-O2 FAIL**（{', '.join(fails)}）")
        print("    依 prereg：seal，依 fault_attribution 記明歸因；")
        print("    **不換 schema／不調 core／不改 cell 比例**去救。")
    else:
        print("  → **NG-O2 PASS**。只可稱「同 entity 多 record 可共存，")
        print("    且已選出的 target 可被消費」，**必須同時引用 honesty note**：")
        print("    架構改動是把 §4.63 的失敗模式**以構造排除**，不是解掉；")
        print("    它既沒有修復、也沒有測到 read/write key agreement 與 unique selection。")
    json.dump({"eval_checksum": art["checksum"], "train_fingerprint": fp,
               "seeds": out, "fails": fails, "pass": not fails},
              open(os.path.join(HERE, "results_ng_o2.json"), "w"),
              indent=2, ensure_ascii=False)
    print("  -> results_ng_o2.json")


if __name__ == "__main__":
    main()
