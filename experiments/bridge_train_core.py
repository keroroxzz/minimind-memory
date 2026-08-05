"""橋接任務的新 core —— **同時**在兩種 render 上訓練。

§4.50 鎖死的規格，逐條對應：

- **core 格式是一級變因**：同一語法、同一答案，訓練時 `L0`（值寫在文字裡）與
  `carrier`（值由**非文字**管道在 placeholder 位置到達）**各佔一半**。
  G4b 的 oracle 天花板一度掉到 **0.0%**，只因為 core 沒見過那個格式 ——
  那個錯與記憶完全無關，這裡從訓練就杜絕。
- carrier 訓練用 **oracle inline**（把 placeholder 的 embedding 換成真值 token 的
  embedding），**零參數**。這樣 core 學會的是「值會從別的地方到」，
  而不是學會某個特定 adapter 的輸出 —— 後者會讓後續的 delivery 實驗失去意義。
- **最大固定 loop budget**（`num_loops=4`）。真正的 `num_loops` 之後依
  **只看 `L0`** 的預先登記規則從 depth sweep 選，**不沿用 S₅ 的 2**。
- 載體位置**隨機化**（實體數 2..4、屬性、問哪個、哪些 slot 走 carrier 全隨機），
  隔離「消費／融合」而非重演 writer-location。

⚠️ `use_engram` 在 `MiniMindConfig` 預設是 **True** —— 每個旗標都必須顯式釘死，
   否則會靜默跑出 134M 參數的模型（CLAUDE.md 記過的坑）。

訓練完成後才做三個 gate（另一支腳本）：
  (i) `L0` 顯式答案天花板與 loop ceiling
  (ii) oracle latent 在 `j=0` 的 **consumer ceiling**
  (iii) oracle latent 在 `j=1`（最多 `j=2`）的 **fusion ceiling**
任何 `L0` < 95% 的格子 **censor** —— 不把「core 看不懂語法」誤判成 memory FAIL。
"""
import argparse
import json
import os
import random
import sys
import time

import torch
import torch.nn.functional as F

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer

import bridge_renderer as B
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

# **每個旗標顯式釘死**，尤其 use_engram（預設 True）
ARCH = dict(hidden_size=512, num_hidden_layers=8, num_attention_heads=8,
            num_key_value_heads=2, max_seq_len=256,
            use_looped_transformer=True, num_loops=4, loop_lora_rank=8,
            use_engram=False, use_dense_attention=False, use_recurrence=False,
            use_latent_attention=False, use_moe=False)


def build_batch(tok, rng, bs, js, carrier_p=0.5):
    """一半 L0、一半 carrier；carrier 的值由 oracle inline 送達。"""
    X, Y, EMB = [], [], []
    maxlen = 0
    items = []
    for _ in range(bs):
        j = js[rng.randrange(len(js))]
        ep = B.make_episode(rng, j)
        use_carrier = rng.random() < carrier_p
        mask = B.random_mask(rng, ep, p=0.6, force_used=True) if use_carrier \
            else [False] * len(ep.facts)
        p_c, ans = B.render(ep, mask)
        full = tok(tok.bos_token + p_c + " " + ans + tok.eos_token,
                   add_special_tokens=False).input_ids
        plen = len(tok(tok.bos_token + p_c, add_special_tokens=False).input_ids)
        y = list(full); y[:plen] = [-100] * plen
        # oracle inline：carrier 位置要換成 L0 版本同位置的 token
        src = tok(tok.bos_token + B.render(ep, None)[0],
                  add_special_tokens=False).input_ids
        pos = [i for i, (a, b) in enumerate(zip(
            tok(tok.bos_token + p_c, add_special_tokens=False).input_ids, src))
            if a != b] if any(mask) else []
        items.append((full, y, pos, src))
        maxlen = max(maxlen, len(full))
    pad = tok.pad_token_id or 0
    for full, y, pos, src in items:
        X.append(full + [pad] * (maxlen - len(full)))
        Y.append(y + [-100] * (maxlen - len(y)))
        EMB.append((pos, src))
    return torch.tensor(X), torch.tensor(Y), EMB


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", type=int, default=24000)
    ap.add_argument("--bs", type=int, default=48)
    ap.add_argument("--lr", type=float, default=6e-4)
    ap.add_argument("--js", type=int, nargs="+", default=[0, 1, 2])
    ap.add_argument("--seed", type=int, default=20260805)
    ap.add_argument("--throttle", type=float, default=0.8)
    ap.add_argument("--out", default="bridge_core.pth")
    a = ap.parse_args()

    torch.manual_seed(a.seed)
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    cfg = MiniMindConfig(vocab_size=len(tok), **ARCH)
    m = MiniMindForCausalLM(cfg).to(DEVICE)
    n = sum(p.numel() for p in m.parameters())
    print(f"  new core {n/1e6:.2f}M   num_loops={ARCH['num_loops']} "
          f"(最大 budget，真正的值之後由只看 L0 的 depth sweep 選)")
    print(f"  訓練 j={a.js}；**L0 與 carrier 各半**，carrier 走 oracle inline（零參數）")
    print(f"  use_engram={ARCH['use_engram']}（顯式釘死；預設是 True，會靜默變 134M）\n")

    opt = torch.optim.AdamW(
        [{"params": [p for k, p in m.named_parameters()
                     if p.dim() >= 2 and "embed" not in k and "norm" not in k],
          "weight_decay": 0.01},
         {"params": [p for k, p in m.named_parameters()
                     if p.dim() < 2 or "embed" in k or "norm" in k],
          "weight_decay": 0.0}], lr=a.lr, betas=(0.9, 0.95))
    sch = torch.optim.lr_scheduler.OneCycleLR(opt, a.lr, total_steps=a.steps,
                                              pct_start=0.03)
    scaler = torch.amp.GradScaler("cuda", enabled=DEVICE == "cuda")
    rng = random.Random(a.seed)
    m.train(); t0 = time.time()
    for step in range(1, a.steps + 1):
        X, Y, EMB = build_batch(tok, rng, a.bs, a.js)
        X, Y = X.to(DEVICE), Y.to(DEVICE)
        s0 = time.time()
        with torch.amp.autocast("cuda", dtype=torch.bfloat16,
                                enabled=DEVICE == "cuda"):
            e = m.model.embed_tokens(X)
            for bi, (pos, src) in enumerate(EMB):       # oracle inline 替換
                for p in pos:
                    e[bi, p] = m.model.embed_tokens(
                        torch.tensor(src[p], device=DEVICE))
            logits = m(X, inputs_embeds=e).logits
            loss = F.cross_entropy(
                logits[:, :-1].reshape(-1, logits.size(-1)).float(),
                Y[:, 1:].reshape(-1), ignore_index=-100)
        opt.zero_grad(set_to_none=True)
        scaler.scale(loss).backward()
        scaler.unscale_(opt); torch.nn.utils.clip_grad_norm_(m.parameters(), 1.0)
        scaler.step(opt); scaler.update(); sch.step()
        if a.throttle < 1.0 and DEVICE == "cuda":
            torch.cuda.synchronize(); time.sleep((time.time()-s0)*(1/a.throttle-1))
        if step % 500 == 0:
            el = (time.time()-t0)/60
            print(f"  step {step:6d}/{a.steps} loss={loss.item():.4f} "
                  f"{step/(time.time()-t0):.1f} it/s  {el:.0f} min", flush=True)
        if step % 4000 == 0 or step == a.steps:
            torch.save({"model": m.state_dict(), "arch": ARCH, "step": step,
                        "vocab": len(tok)},
                       os.path.join(HERE, a.out))
    print(f"\n  -> {a.out}   {(time.time()-t0)/60:.0f} min")
    print(f"  下一步：三個 gate（L0 天花板與 loop ceiling / j=0 consumer / "
          f"j=1 fusion），\n  任何 L0 < 95% 的格子 censor。")
    json.dump({"arch": ARCH, "steps": a.steps, "lr": a.lr, "bs": a.bs,
               "js": a.js, "seed": a.seed, "params_M": n/1e6},
              open(os.path.join(HERE, "bridge_core_config.json"), "w"), indent=2)


if __name__ == "__main__":
    main()
