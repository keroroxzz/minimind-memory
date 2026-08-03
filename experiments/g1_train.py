"""G1 訓練：L0（explicit-value positive control）與 G1a/G1b（latent delivery）。

規格 `design_c_layer.md`。實作順序（§5.5）：
  unit invariants → paired latent checksum → **L0 smoke → L0 正式**
  → G1a small-batch overfit → G1a 正式 → 失敗才 G1b

⚠️ **L0 不經 latent carrier**，它是 explicit-value 的 positive control。
   **L0 若失敗，停在 core/training pipeline，不得用 adapter 或 selector 解釋** ——
   那兩者根本沒參與。
"""
import argparse
import hashlib
import json
import math
import os
import sys
import time

import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader, TensorDataset

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer

import g1_renderer as R
from model.memory_module import (ActiveWorkspace, InlineLatentAdapter, LatentSlotAdapter,
                                 LatentSlotsDelivery, SyntheticKVAdapter, SyntheticKVDelivery)
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
BACKBONE = dict(hidden_size=512, num_hidden_layers=8, num_attention_heads=8,
                num_key_value_heads=2, vocab_size=6400, max_position_embeddings=1024)
# ⚠️ 顯式釘住 —— 只寫 num_loops 而不開 use_looped_transformer 時，
#    model 會強制 num_loops=1，整個實驗會靜默退化成單圈（§4.22 記過）。
LOOP_KW = dict(use_looped_transformer=True, loop_adapter="shared",
               loop_input_injection=True, loop_index_embed=True)
ARCH = dict(use_engram=False, use_dense_attention=False, num_loops=2, **LOOP_KW)


def build_tensors(tok, samples, seq_len, render):
    ids, labels, ks = [], [], []
    for s in samples:
        pr, an = render(s)
        e = R.encode(tok, pr, an, seq_len)
        assert e is not None, f"樣本超長（k={s.k}）—— 不靜默丟棄"
        ids.append(e[0]); labels.append(e[1]); ks.append(s.k)
    return (torch.tensor(ids), torch.tensor(labels), torch.tensor(ks))


def make_delivery(kind, n_slots_layers, num_loops, art_path):
    """建立 delivery。SyntheticKV 的 scale 用 artifact 裡實測的逐位置 native RMS。"""
    if kind == "inline":
        return InlineLatentAdapter(BACKBONE["hidden_size"]).to(DEVICE)
    if kind == "slots":
        return LatentSlotsDelivery(LatentSlotAdapter(BACKBONE["hidden_size"])).to(DEVICE)
    art = json.load(open(art_path))["native_kv_rms"]
    return SyntheticKVDelivery(SyntheticKVAdapter(
        n_slots_layers, BACKBONE["num_key_value_heads"],
        BACKBONE["hidden_size"] // BACKBONE["num_attention_heads"],
        num_loops=num_loops, scale_k=art["per_entry_K"], scale_v=art["per_entry_V"])).to(DEVICE)


def deliver(delivery, kind, model, latents, mask, ids=None, pos=None):
    """回傳要餵進 forward 的 kwargs。latents (B,k,25)。

    `inline` 需要 `ids`（該批的 token 序列）與 `pos`（value span 的位置，k×5）。
    """
    if kind == "inline":
        e = model.model.embed_tokens(ids)                      # (B,T,H)
        span = delivery(latents).reshape(latents.shape[0], -1, BACKBONE["hidden_size"])
        e = e.clone()
        e[:, pos] = span.to(e.dtype)                           # 同位置替換
        return {"inputs_embeds": e}, 0
    ws = ActiveWorkspace(kv=None)
    if kind == "slots":
        return {"memory_carriers": delivery.apply(ws, latents, mask).carriers}, latents.shape[1]
    freqs = (model.model.freqs_cos, model.model.freqs_sin)
    return {"past_key_values": delivery.apply(ws, latents, mask, freqs=freqs).kv}, latents.shape[1]


@torch.no_grad()
def greedy(model, tok, prompt, kw, prefix_len, num_loops, max_new=8):
    """自寫的 greedy decode。

    不能直接用 `model.generate` —— 它把 kwargs 傳給**每一步** forward，
    carrier 會在增量解碼時重複前綴；而且它的 `past_len` 會把 memory prefix
    算進去，於是 `input_ids[:, past_len:]` 會切掉真實 token。
    """
    x = tok(tok.bos_token + prompt, add_special_tokens=False,
            return_tensors="pt").input_ids.to(DEVICE)
    out = model(x, use_cache=True, num_loops=num_loops, **kw)
    pkv, got = out.past_key_values, []
    for _ in range(max_new):
        nxt = out.logits[:, -1].argmax(-1, keepdim=True)
        if nxt.item() == tok.eos_token_id:
            break
        got.append(nxt.item())
        out = model(nxt, past_key_values=pkv, use_cache=True, num_loops=num_loops)
        pkv = out.past_key_values
    return tok.decode(got, skip_special_tokens=True).strip()


@torch.no_grad()
def evaluate(model, tok, samples, render, num_loops, throttle=1.0, max_per_k=100,
             delivery=None, kind=None):
    from collections import defaultdict
    hit, tot = defaultdict(int), defaultdict(int)
    seen = defaultdict(int)
    model.eval()
    for s in samples:
        if seen[s.k] >= max_per_k:
            continue
        seen[s.k] += 1
        pr, an = render(s)
        t0 = time.time()
        kw, plen = {}, 0
        if delivery is not None:
            lat = R.resolve_chain(s).unsqueeze(0).to(DEVICE)
            ids_, pos_ = None, None
            if kind == "inline":
                from g1_oracle_inline import value_positions
                a_, b_, pos_ = value_positions(tok, s)
                ids_ = b_.unsqueeze(0).to(DEVICE); pos_ = pos_.to(DEVICE)
            kw, plen = deliver(delivery, kind, model, lat,
                               torch.ones(1, lat.shape[1], dtype=torch.bool, device=DEVICE),
                               ids=ids_, pos=pos_)
        g = greedy(model, tok, pr, kw, plen, num_loops)
        if throttle < 1.0:
            torch.cuda.synchronize(); time.sleep((time.time() - t0) * (1 / throttle - 1))
        tot[s.k] += 1; hit[s.k] += (g == an)
    model.train()
    return {k: {"acc": hit[k] / tot[k], "n": tot[k]} for k in sorted(tot)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="L0", choices=["L0", "G1a", "G1b"])
    ap.add_argument("--delivery", default="slots", choices=["slots", "kv", "inline"])
    ap.add_argument("--smoke", action="store_true", help="小規模，只驗 pipeline 學得動")
    ap.add_argument("--max-k", type=int, default=4)
    ap.add_argument("--train-per-k", type=int, default=5000)
    ap.add_argument("--val-per-k", type=int, default=100)
    ap.add_argument("--steps", type=int, default=6000)
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--seq-len", type=int, default=192)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--throttle", type=float, default=0.8)
    ap.add_argument("--init-from", default="ckpt_vanilla.pth")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    if a.smoke:
        a.train_per_k, a.steps, a.val_per_k = 800, 1200, 60

    torch.manual_seed(a.seed)
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))

    tr = R.build_dataset(a.max_k, a.train_per_k, a.seed)
    va = R.build_dataset(a.max_k, a.val_per_k, a.seed + 999,
                         exclude={s.delivery_id for s in tr})
    inter = {s.delivery_id for s in tr} & {s.delivery_id for s in va}
    assert not inter, f"train/val delivery_id 交集 {len(inter)}"
    print(f"  train {len(tr)} / val {len(va)}   delivery checksum "
          f"{R.delivery_checksum(tr)} / {R.delivery_checksum(va)}")

    is_l0 = a.stage == "L0"
    render = R.render_L0 if is_l0 else R.render_latent
    X, Y, K = build_tensors(tok, tr, a.seq_len, render)
    print(f"  最長 {int((Y != -100).sum(1).max() + (X != tok.pad_token_id).sum(1).max())}"
          f" 以內 / seq_len {a.seq_len}；label 位置數 {int((Y != -100).sum())}")

    cfg = MiniMindConfig(**BACKBONE, **ARCH)
    assert cfg.use_looped_transformer and cfg.num_loops == 2
    model = MiniMindForCausalLM(cfg).to(DEVICE)
    ip = os.path.join(HERE, a.init_from)
    if os.path.exists(ip):
        miss, unexp = model.load_state_dict(torch.load(ip, map_location="cpu"), strict=False)
        miss = [k for k in miss if not k.endswith(("freqs_cos", "freqs_sin"))]
        if unexp:
            raise SystemExit(f"❌ checkpoint 與架構不符：{unexp[:3]}")
        print(f"  從 {a.init_from} 出發（{len(miss)} 個新參數零初始化）")

    delivery, fp_extra = None, {}
    if not is_l0:
        delivery = make_delivery(a.delivery, BACKBONE["num_hidden_layers"],
                                 ARCH["num_loops"],
                                 os.path.join(HERE, "g1_renderer_artifact.json"))
        if a.stage == "G1a":
            for p_ in model.parameters():
                p_.requires_grad_(False)          # plug-compatibility：core 凍結
        n_core = sum(p_.numel() for p_ in model.parameters() if p_.requires_grad)
        n_del = sum(p_.numel() for p_ in delivery.parameters())
        # 「同 config」不能只停在文字：把**初始 adapter 權重**的 hash 記下來，
        # 才能證明兩次跑的起點真的相同（Codex）。
        blob = b"".join(v.detach().cpu().numpy().tobytes()
                        for _, v in sorted(delivery.state_dict().items()))
        init_hash = hashlib.sha256(blob).hexdigest()[:16]
        print(f"  delivery={a.delivery}  可訓參數：core {n_core/1e6:.3f}M / "
              f"delivery {n_del/1e6:.3f}M   初始權重 sha {init_hash}")
        fp_extra = {"delivery_params": n_del, "adapter_init_sha": init_hash}
        assert (a.stage != "G1a") or n_core == 0, "G1a 必須完全凍結 core"

    fp_extra = fp_extra if not is_l0 else {}
    fp = {**fp_extra, "stage": a.stage, "arch": ARCH, "backbone": BACKBONE, "max_k": a.max_k,
          "steps": a.steps, "lr": a.lr, "bs": a.batch_size, "seed": a.seed,
          "seq_len": a.seq_len, "train_per_k": a.train_per_k,
          "init_from": a.init_from, "smoke": a.smoke,
          "delivery": a.delivery if a.stage != "L0" else None,
          "delivery_checksum": R.delivery_checksum(tr)}
    h = hashlib.sha256(json.dumps(fp, sort_keys=True, default=str).encode()).hexdigest()[:10]
    tag = a.stage + ("" if is_l0 else f"_{a.delivery}") + ("_smoke" if a.smoke else "")
    ck = os.path.join(HERE, f"g1_{tag}_{h}.pth")
    if os.path.exists(ck):
        raise SystemExit(f"❌ 已存在同組 config 的 checkpoint：{os.path.basename(ck)}")

    # --- delivery（G1a/G1b 才有）---
    # 逐 k 分批 —— 同一批的 k 必須相同，否則 latent 要 padding，
    # 而遮罩掉的 carrier 仍佔位置，會把「有沒有值」以外的變因帶進來。
    by_k = {}
    for i, k in enumerate(K.tolist()):
        by_k.setdefault(k, []).append(i)
    lat_by_k = {k: torch.stack([R.resolve_chain(tr[i]) for i in idx]) for k, idx in by_k.items()} \
        if not is_l0 else {}
    pos_by_k = {}
    if not is_l0 and a.delivery == "inline":
        from g1_oracle_inline import value_positions
        for k, idx in by_k.items():                   # 同 k 的結構相同 → 位置相同
            pos_by_k[k] = value_positions(tok, tr[idx[0]])[2].to(DEVICE)

    params = list(delivery.parameters()) if a.stage == "G1a" else \
        [p_ for p_ in list(model.parameters()) + (list(delivery.parameters()) if delivery else [])
         if p_.requires_grad]
    opt = torch.optim.AdamW(params, lr=a.lr, weight_decay=0.01)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, a.lr, total_steps=a.steps, pct_start=0.05)
    scaler = torch.amp.GradScaler("cuda", enabled=DEVICE == "cuda")
    print(f"\n{'=' * 58}\n  {a.stage}{' smoke' if a.smoke else ''} — {a.steps} 步"
          f"（throttle {a.throttle:.0%}）\n{'=' * 58}", flush=True)

    model.train()
    if delivery is not None:
        delivery.train()
    step = 0; t0 = time.time()
    ks = sorted(by_k)
    g = torch.Generator().manual_seed(a.seed)
    while step < a.steps:
        # L0 走 DataLoader；G1a/G1b 逐 k 自行取樣（同批 k 必須相同）
        batches = dl if is_l0 else range(10 ** 9)
        for item in batches:
            if step >= a.steps:
                break
            kw = {}
            if is_l0:
                x, y = item
            else:
                kk = ks[step % len(ks)]
                idx = torch.randint(len(by_k[kk]), (a.batch_size,), generator=g)
                sel = torch.tensor([by_k[kk][i] for i in idx.tolist()])
                x, y = X[sel], Y[sel]
                lat = lat_by_k[kk][idx].to(DEVICE)
                mask = torch.ones(lat.shape[:2], dtype=torch.bool, device=DEVICE)
                pos_k = pos_by_k.get(kk)
            x, y = x.to(DEVICE).long(), y.to(DEVICE).long()
            s0 = time.time()
            with torch.amp.autocast("cuda", dtype=torch.bfloat16, enabled=DEVICE == "cuda"):
                if not is_l0:
                    kw, plen = deliver(delivery, a.delivery, model, lat, mask,
                                       ids=x if a.delivery == "inline" else None,
                                       pos=pos_k if a.delivery == "inline" else None)
                logits = model(x, **kw).logits
                if not is_l0:
                    logits = logits[:, plen:] if a.delivery == "slots" else logits
                loss = F.cross_entropy(logits[:, :-1].reshape(-1, logits.size(-1)).float(),
                                       y[:, 1:].reshape(-1), ignore_index=-100)
            opt.zero_grad(set_to_none=True)
            scaler.scale(loss).backward()
            scaler.unscale_(opt); torch.nn.utils.clip_grad_norm_(params, 1.0)
            scaler.step(opt); scaler.update(); sched.step(); step += 1
            if a.throttle < 1.0 and DEVICE == "cuda":
                torch.cuda.synchronize(); time.sleep((time.time() - s0) * (1 / a.throttle - 1))
            if step % 200 == 0:
                print(f"  step {step:5d}/{a.steps} loss={loss.item():.4f} "
                      f"{step / (time.time() - t0):.1f} it/s", flush=True)

    if delivery is not None:
        delivery.eval()
    per_k = evaluate(model, tok, va, render, ARCH["num_loops"], a.throttle,
                     delivery=delivery, kind=a.delivery if delivery else None)
    overall = sum(v["acc"] * v["n"] for v in per_k.values()) / sum(v["n"] for v in per_k.values())
    print(f"\n  逐深度正確率（全對基準 1/120 = 0.8%）：")
    for k, v in per_k.items():
        print(f"    k={k:>2d}  {v['acc']:6.1%}  (n={v['n']})")
    print(f"  整體 {overall:.1%}   {(time.time() - t0) / 60:.1f} min", flush=True)

    state = {k: v.cpu() for k, v in model.state_dict().items()}
    if delivery is not None:
        state = {"model": state, "delivery": {k: v.cpu() for k, v in delivery.state_dict().items()}}
    torch.save(state, ck)
    json.dump({**fp, "per_k": {str(k): v for k, v in per_k.items()}, "overall": overall,
               "wall_clock_s": time.time() - t0, "checkpoint": os.path.basename(ck)},
              open(ck[:-4] + ".json", "w"), indent=2, ensure_ascii=False)
    out = a.out or f"results_g1_{tag}.json"
    json.dump({**fp, "per_k": {str(k): v for k, v in per_k.items()}, "overall": overall},
              open(os.path.join(HERE, out), "w"), indent=2, ensure_ascii=False)
    print(f"  -> {os.path.basename(ck)}  /  {out}")


if __name__ == "__main__":
    main()
