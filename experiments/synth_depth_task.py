"""深度可控的合成推理任務 —— 直接測「單次前向傳播能做幾步組合推理」。

為什麼需要這個
--------------
1. 通用推理基準（GSM8K 等）在 29M 規模下沒有解析度，四個架構大概率全 0%。
2. Belle 的 exact-match 不可靠：中文數學解答常以餘數或多部分答案結尾，
   「取最後一個數字」四題只對一題。等於在量雜訊。
3. 這個任務的答案由建構方式決定，完全無歧義。

核心設計：**目標只有最終答案，不含任何中間步驟**
----------------------------------------------
CA / looped transformer 買到的是「單次前向傳播內的深度」。
若允許模型輸出 CoT，它可以一個 token 做一步，深度就從「層」轉移到
「自迴歸步數」，架構差異會被完全掩蓋。因此目標序列只有答案本身。

任務格式（k 步依賴鏈，每步都依賴上一步的結果）：
    a=53 b=a+7 c=b*3 d=c-9 求d=          → 81
值域取 mod 100，答案固定 100 類 → 隨機基準 1%。

用法：
    python experiments/synth_depth_task.py --gen             # 產生資料
    python experiments/synth_depth_task.py --run vanilla     # 訓練 + 評測
    python experiments/synth_depth_task.py --run dense
    python experiments/synth_depth_task.py --report
"""
import os
import sys
import json
import time
import math
import random
import argparse

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import torch
from transformers import AutoTokenizer

from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "synth_depth.pt")
RESULTS = os.path.join(HERE, "results_synth.json")
BASE_CKPT = os.path.join(HERE, "ckpt_vanilla.pth")

BACKBONE = dict(hidden_size=512, num_hidden_layers=8, num_attention_heads=8,
                num_key_value_heads=2, vocab_size=6400, max_position_embeddings=1024)
LOOP_KW = dict(use_looped_transformer=True, loop_adapter="shared",
               loop_input_injection=True, loop_index_embed=True)
CONFIGS = {
    "vanilla":   dict(use_engram=False, use_dense_attention=False),
    "dense":     dict(use_engram=False, use_dense_attention=True),
    "engram":    dict(use_engram=True,  use_dense_attention=False),
    "engram+ca": dict(use_engram=True,  use_dense_attention=True),
    # E1：迴圈次數 = 架構深度。num_loops=1 與 vanilla 完全等價
    # (適配器只在 num_loops>1 時建立，loop_index_embed 在 loop 0 恆為 0)
    "loop1": dict(use_engram=False, use_dense_attention=False, num_loops=1, **LOOP_KW),
    "loop2": dict(use_engram=False, use_dense_attention=False, num_loops=2, **LOOP_KW),
    "loop3": dict(use_engram=False, use_dense_attention=False, num_loops=3, **LOOP_KW),
    "loop4": dict(use_engram=False, use_dense_attention=False, num_loops=4, **LOOP_KW),
    # E2：訓練時從 [1,4] 隨機抽圈數，讓模型學會在任意深度都輸出合理結果。
    # 這是 test-time compute scaling 的前提 —— 推論時才能自由加深。
    "loopR": dict(use_engram=False, use_dense_attention=False, num_loops=4,
                  loop_random_min=1, **LOOP_KW),
    # E2b：修正版。從已找到迭代解的 loop4 出發（課程式），圈數分布偏深
    # ({2,3,4,4,4} → 60% 落在 4 圈)，並讓模型看得見剩餘預算。
    # loopR 均勻抽 [1,4] 失敗的原因：25% 的步在 1 圈，而 1 圈天花板 k≈2.7，
    # 那些樣本目標不可達、梯度是噪音，把模型帶往只做 k<=2 的退化解。
    "loopR2": dict(use_engram=False, use_dense_attention=False, num_loops=4,
                   loop_random_choices=[2, 3, 4, 4, 4], loop_budget_embed=True, **LOOP_KW),
}
ENGRAM = dict(engram_offload_cpu=False, engram_layers=[2, 4, 6])

import string
MAX_K = 6                       # 由 --max-k 覆寫
NAMES = string.ascii_lowercase   # 最多支援 k=25
SEQ_LEN = 176                    # k=16 的題目約 80 tokens，留餘裕
DEVICE = "cuda"
SEED = 42


# ----------------------------------------------------------------- 置換合成任務
# 為什麼換掉「數值鏈」：
#   純加減 →  答案 = v0 + Σ±rhs。加法可交換可結合，整條鏈塌縮成一個求和，
#             而求和是注意力一層就能做的事 (TC0)。實測 8 層在 k=12 仍有 90%，
#             量到的根本不是深度。
#   含乘法 →  確實不可交換，但把答案熵從 3.08 壓到 0.62 bit，長鏈變成猜比算划算。
#
# 置換合成同時滿足兩個條件：
#   * 不可交換 —— f∘g ≠ g∘f，順序不能重排
#   * 分布不塌縮 —— 置換是雙射，均勻進去必然均勻出來，不存在熵塌縮捷徑
#   * 理論保證 —— S5 的字問題是 NC1-完備，固定深度 (TC0) 解不了任意長度，
#     正是為「量測序列深度」而存在的標準任務
PERM_N = 5
N_GEN = 8


def _generators(seed=20260731):
    rng = random.Random(seed)
    gens = []
    while len(gens) < N_GEN:
        p = list(range(PERM_N)); rng.shuffle(p)
        if p != list(range(PERM_N)) and p not in gens:
            gens.append(p)
    return gens


GENS = _generators()


def make_perm(k, rng):
    """x=<起始置換> f_i f_j ... 求x=<結果置換>"""
    state = list(range(PERM_N)); rng.shuffle(state)
    prompt = "x=" + " ".join(map(str, state))
    for _ in range(k):
        g = rng.randrange(N_GEN)
        state = [state[GENS[g][i]] for i in range(PERM_N)]
        prompt += f" f{g}"
    return prompt + " 求x=", " ".join(map(str, state))


# ----------------------------------------------------------------- 資料產生
def fmt(n):
    """數字一律拆成「以空白分隔的位數」。

    minimind 的 6400 BPE 詞表對數字的切法極不一致：0..99 之中 42 個是單 token、
    58 個是雙 token（'60'→[3873] 但 '97'→[60,58]）。模型得先學會這兩種表示是同
    一回事才談得上算術，這會直接扼殺算術能力。加空白後每個位數固定一個 token。
    """
    return " ".join(f"{n:02d}")


def make_one(k, rng, v0_lo=10, v0_hi=79, ops="+-"):
    """產生一條 k 步依賴鏈。每一步都必須用到前一步的結果。

    train / val 用**互斥的起始值域**來保證不重疊，而不是靠拒絕採樣去找唯一解 ——
    k=1 時整個問題空間只有 90×3×8=2160 種，要求兩萬條唯一會讓迴圈永遠跑不完。
    這樣同時也更嚴格：val 的起始值在訓練時從未出現過，測的是泛化而非記憶。
    """
    v = rng.randint(v0_lo, v0_hi)
    parts = [f"{NAMES[0]}={fmt(v)}"]
    for i in range(1, k + 1):
        op = rng.choice(ops)
        rhs = rng.randint(2, 9)
        v = {"+": v + rhs, "-": v - rhs, "*": v * rhs}[op] % 100
        parts.append(f"{NAMES[i]}={NAMES[i-1]}{op}{rhs}")
    prompt = " ".join(parts) + f" 求{NAMES[k]}="
    return prompt, fmt(v)


def gen(args):
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    rng = random.Random(SEED)
    per_k_train = args.train_per_k
    per_k_val = args.val_per_k

    def build(n_per_k, rng, v0_lo, v0_hi, unique):
        """unique=True 時去重（val 用）；train 允許重複，反正就是重複樣本。"""
        rows = []
        for k in range(1, MAX_K + 1):
            seen, made, attempts = set(), 0, 0
            while made < n_per_k and attempts < n_per_k * 50:
                attempts += 1
                p, a = make_one(k, rng, v0_lo, v0_hi, args.ops)
                if unique:
                    if p in seen:
                        continue
                    seen.add(p)
                rows.append({"k": k, "prompt": p, "answer": a})
                made += 1
            if made < n_per_k:
                print(f"  ⚠️  k={k} 只產生 {made}/{n_per_k} 條（問題空間已窮盡）")
        return rows

    # train / val 共用完整值域 0..99，改以「val 先生成、train 排除它們」確保零重疊。
    #
    # 早期版本用互斥的起始值域 (train 10-79 / val 80-99)。那在混合運算下沒問題 ——
    # 乘法兩三步就把值打散到整個 0..99，模型被迫學會真正的模算術。但在**純加減**下
    # 是致命的：答案永遠是 v0 + Σ±rhs，值黏在 v0 附近，模型只要記住訓練那段帶狀區域
    # 就能把 loss 壓到 0.02，而 94+9=103→03 這種繞回在訓練中幾乎不出現。
    # 實測：訓練值域內 k=1 100% / k=4 99%，val 值域 0% / 0%。
    #
    # 預設純加減：乘法會把答案熵從 3.08 壓到 0.62 bit，長鏈變成猜比算划算
    # (實測純乘法正確率隨深度「上升」到 87%)，會污染深度的量測。
    if args.task == "perm":
        def build_perm(n, rng, unique, exclude=None):
            rows = []
            for k in range(1, MAX_K + 1):
                seen, made, att = set(), 0, 0
                while made < n and att < n * 50:
                    att += 1
                    p, a = make_perm(k, rng)
                    if unique and p in seen: continue
                    if exclude and p in exclude: continue
                    seen.add(p); rows.append({"k": k, "prompt": p, "answer": a}); made += 1
            return rows
        val_rows = build_perm(per_k_val, random.Random(SEED + 999), unique=True)
        vs = {r["prompt"] for r in val_rows}
        train_rows = build_perm(per_k_train, rng, unique=False, exclude=vs)
        assert not ({r["prompt"] for r in train_rows} & vs)
    else:
        val_rows = build(per_k_val, random.Random(SEED + 999), 0, 99, unique=True)
        val_set = {r["prompt"] for r in val_rows}
        train_rows = [r for r in build(int(per_k_train * 1.02), rng, 0, 99, unique=False)
                      if r["prompt"] not in val_set]
        assert not ({r["prompt"] for r in train_rows} & val_set)

    def encode(rows):
        ids, plen, tlen, ks = [], [], [], []
        for r in rows:
            p = tok(tok.bos_token + r["prompt"], add_special_tokens=False)["input_ids"]
            a = tok(r["answer"] + tok.eos_token, add_special_tokens=False)["input_ids"]
            t = len(p) + len(a)
            if t > SEQ_LEN:
                continue
            ids.append(p + a + [tok.pad_token_id] * (SEQ_LEN - t))
            plen.append(len(p)); tlen.append(t); ks.append(r["k"])
        return (torch.tensor(ids, dtype=torch.int16), torch.tensor(plen, dtype=torch.int16),
                torch.tensor(tlen, dtype=torch.int16), torch.tensor(ks, dtype=torch.int8))

    tr = encode(train_rows); va = encode(val_rows)
    torch.save({"train": tr, "val": va, "val_rows": val_rows,
                "seq_len": SEQ_LEN, "pad_token_id": tok.pad_token_id}, DATA)
    print(f"✅ {DATA}")
    print(f"   train {tr[0].shape[0]} 條 / val {va[0].shape[0]} 條，k=1..{MAX_K}")
    print(f"   隨機基準 = 1.0%（答案 100 類）")
    print(f"   範例 k=3: {[r for r in train_rows if r['k']==3][0]}")


# ----------------------------------------------------------------- 訓練 / 評測
def labels_of(ids, plen, tlen):
    lab = ids.clone()
    ar = torch.arange(ids.shape[1], device=ids.device).unsqueeze(0)
    lab[(ar < plen.unsqueeze(1)) | (ar >= tlen.unsqueeze(1))] = -100
    return lab


@torch.no_grad()
def acc_by_k(model, tok, val_rows, max_per_k=200, throttle=1.0, n_loops=None, only_k=None):
    """逐 k 計算正確率。貪婪解碼，答案必須完全相符。"""
    from collections import defaultdict
    hit, tot = defaultdict(int), defaultdict(int)
    pos, wf = defaultdict(float), defaultdict(int)
    by_k = defaultdict(list)
    for r in val_rows:
        by_k[r["k"]].append(r)
    for k in sorted(by_k):
        if only_k and k not in only_k:
            continue
        for r in by_k[k][:max_per_k]:
            g_t0 = time.time()
            ids = tok(tok.bos_token + r["prompt"], add_special_tokens=False,
                      return_tensors="pt").input_ids.to(DEVICE)
            gkw = {"num_loops": n_loops} if n_loops else {}
            out = model.generate(ids, max_new_tokens=6, do_sample=False,
                                 eos_token_id=tok.eos_token_id, **gkw)
            gen_txt = tok.decode(out[0][ids.shape[1]:], skip_special_tokens=True).strip()
            if throttle < 1.0:
                torch.cuda.synchronize()
                time.sleep((time.time() - g_t0) * (1.0 / throttle - 1.0))
            tot[k] += 1
            hit[k] += (gen_txt == r["answer"])
            # 逐位數診斷：mod-100 的個位數是 Z_10 上的淺層電路，十位數要進位才難。
            # 若整體正確率對 k 平坦，很可能是兩者混在一起看不出來。
            gd, pd = r["answer"].split(), gen_txt.split()
            if len(pd) == len(gd):
                pos[k] += sum(a == b for a, b in zip(pd, gd)) / len(gd)
            wf[k] += (len(pd) == len(gd))
    return {k: {"correct": hit[k], "total": tot[k], "acc": hit[k] / max(tot[k], 1),
                "pos_acc": pos[k] / max(tot[k], 1), "wellformed": wf[k] / max(tot[k], 1)}
            for k in sorted(tot)}


def run(name, args):
    d = torch.load(DATA)
    (tr_ids, tr_plen, tr_tlen, _), (va_ids, va_plen, va_tlen, _) = d["train"], d["val"]
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))

    kw = dict(CONFIGS[name])
    if kw.get("use_engram"):
        kw.update(ENGRAM)
    torch.manual_seed(SEED); torch.cuda.manual_seed_all(SEED)
    model = MiniMindForCausalLM(MiniMindConfig(**BACKBONE, **kw))
    init_path = args.init_from or (BASE_CKPT if args.from_pretrain else None)
    if init_path and os.path.exists(init_path):
        sd = torch.load(init_path, map_location="cpu")
        miss, unexp = model.load_state_dict(sd, strict=False)
        if unexp:
            raise RuntimeError(f"checkpoint 與架構不符：{unexp[:3]}")
        miss = [k for k in miss if not k.endswith(("freqs_cos", "freqs_sin"))]
        print(f"  從 {os.path.basename(init_path)} 出發"
              f"{f'（{len(miss)} 個新參數零初始化）' if miss else ''}", flush=True)
    model = model.to(DEVICE)

    torch.cuda.empty_cache(); torch.cuda.reset_peak_memory_stats()
    bs, steps, accum = args.batch_size, args.steps, args.accum
    # 梯度累積：loop3/4 的 activation 記憶體撐不住 bs=64（實測 loop3 在 11.4 GiB OOM）。
    # 降 micro-batch 但保持等效 batch 不變，避免把「記憶體限制」變成組間的混淆因子。
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=0.01,
                            betas=(0.9, 0.95), fused=True)
    g = torch.Generator().manual_seed(SEED)
    n = tr_ids.shape[0]
    order = torch.cat([torch.randperm(n, generator=g)
                       for _ in range(steps * bs * accum // n + 2)])

    print(f"\n{'='*60}\n  {name} — {steps} 步\n{'='*60}", flush=True)
    model.train(); t0 = time.time()
    run_loss = torch.zeros((), device=DEVICE); run_n = 0
    for step in range(1, steps + 1):
        lr = args.lr * min(1.0, step / 200) * (0.1 + 0.9 * 0.5 *
             (1 + math.cos(math.pi * min(1.0, step / steps))))
        for pg in opt.param_groups:
            pg["lr"] = lr
        step_t0 = time.time()
        opt.zero_grad(set_to_none=True)
        for micro in range(accum):
            base = ((step - 1) * accum + micro) * bs
            sel = order[base:base + bs]
            ids = tr_ids[sel].to(DEVICE).long()
            lab = labels_of(ids, tr_plen[sel].to(DEVICE).long(), tr_tlen[sel].to(DEVICE).long())
            with torch.amp.autocast(DEVICE, dtype=torch.bfloat16):
                out = model(ids, labels=lab)
                loss = (out.loss + out.aux_loss) / accum
            loss.backward()
            run_loss += out.loss.detach() / accum
        run_n += 1
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        opt.step()

        if args.throttle < 1.0:
            # duty cycle 節流：算完一步就休息，把平均功耗壓下來。
            # nvidia-smi -pl 鎖功耗需要 sudo，所以用 sleep 達成同樣效果。
            torch.cuda.synchronize()
            dt = time.time() - step_t0
            time.sleep(dt * (1.0 / args.throttle - 1.0))
        if step % 500 == 0:
            print(f"  step {step:5d}/{steps} loss={(run_loss/run_n).item():.4f} "
                  f"{step/(time.time()-t0):.1f} it/s", flush=True)
            run_loss = torch.zeros((), device=DEVICE); run_n = 0

    model.eval()
    if args.eval_loops:
        loops = [int(x) for x in args.eval_loops.split(",")]
        only = [int(x) for x in args.eval_k.split(",")] if args.eval_k else None
        by_loop = {}
        print(f"\n  test-time scaling：同一組權重，推論圈數 {loops}", flush=True)
        for L in loops:
            pk = acc_by_k(model, tok, d["val_rows"], max_per_k=args.eval_per_k,
                          throttle=args.throttle, n_loops=L, only_k=only)
            by_loop[L] = pk
            ov = sum(v["correct"] for v in pk.values()) / max(sum(v["total"] for v in pk.values()), 1)
            seen = " ".join(f"k{k}:{v['acc']:.0%}" for k, v in sorted(pk.items()))
            print(f"    {L} 圈{'（訓練沒見過）' if L > 4 else '':　<8s}  整體 {ov:6.1%}   {seen}", flush=True)
        res = json.load(open(RESULTS)) if os.path.exists(RESULTS) else {}
        res[name + "_ttscale"] = {str(L): {str(k): v for k, v in pk.items()}
                                  for L, pk in by_loop.items()}
        json.dump(res, open(RESULTS, "w"), indent=2, ensure_ascii=False)
        per_k = by_loop[loops[-1]]
    else:
        per_k = acc_by_k(model, tok, d["val_rows"], max_per_k=args.eval_per_k,
                         throttle=args.throttle)
    elapsed = time.time() - t0
    overall = sum(v["correct"] for v in per_k.values()) / max(sum(v["total"] for v in per_k.values()), 1)
    print(f"\n  逐深度正確率（置換全對基準 1/120=0.8%；逐位置基準 20%）：")
    print(f"    {'k':>3s} {'全對':>9s} {'逐位置':>9s} {'格式正確':>9s}")
    for k, v in per_k.items():
        print(f"    {k:>3d} {v['acc']:8.1%} {v['pos_acc']:8.1%} {v['wellformed']:8.1%}")
    print(f"  整體 {overall:.1%}   {elapsed/60:.1f} min", flush=True)

    ck = os.path.join(HERE, f"synth_{name.replace('+','_')}.pth")
    torch.save({k: v.cpu() for k, v in model.state_dict().items()}, ck)

    res = json.load(open(RESULTS)) if os.path.exists(RESULTS) else {}
    res[name] = {"per_k": {str(k): v for k, v in per_k.items()}, "overall": overall,
                 "params_M": sum(p.numel() for p in model.parameters()) / 1e6,
                 "wall_clock_s": elapsed, "steps": steps,
                 "peak_vram_GiB": torch.cuda.max_memory_allocated() / 2**30}
    json.dump(res, open(RESULTS, "w"), indent=2, ensure_ascii=False)
    del model, opt; torch.cuda.empty_cache()


def report():
    res = json.load(open(RESULTS))
    ks = sorted({int(k) for r in res.values() for k in r["per_k"]})
    print(f"\n{'config':12s} " + " ".join(f"{'k='+str(k):>8s}" for k in ks) + f" {'整體':>8s}")
    print("-" * (13 + 9 * len(ks) + 9))
    for n in CONFIGS:
        if n in res:
            r = res[n]
            row = " ".join(f"{r['per_k'].get(str(k),{'acc':float('nan')})['acc']:7.1%} " for k in ks)
            print(f"{n:12s} {row}{r['overall']:7.1%}")
    print("\n隨機基準 1.0%。若某架構的曲線隨 k 衰減得較慢，即為單次前向推理深度的直接證據。")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--gen", action="store_true")
    ap.add_argument("--run", default=None)
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--train-per-k", type=int, default=20000)
    ap.add_argument("--val-per-k", type=int, default=300)
    ap.add_argument("--steps", type=int, default=6000)
    ap.add_argument("--batch-size", type=int, default=64, help="micro-batch")
    ap.add_argument("--accum", type=int, default=1, help="梯度累積步數；等效 batch = batch_size*accum")
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--eval-per-k", type=int, default=200)
    ap.add_argument("--eval-loops", default=None, help='E2：逗號分隔的推論圈數，例如 "1,2,3,4,6,8"')
    ap.add_argument("--eval-k", default=None, help='只評這些 k，例如 "2,4,8,12,16,24"')
    ap.add_argument("--from-pretrain", type=int, default=1)
    ap.add_argument("--init-from", default=None, help="指定初始 checkpoint（課程式微調用）")
    ap.add_argument("--max-k", type=int, default=6, help="最大推理深度")
    ap.add_argument("--ops", default="+-", help='數值鏈的運算集合')
    ap.add_argument("--task", default="perm", choices=["perm", "chain"],
                    help='perm=置換合成(預設，真正量深度)；chain=數值鏈(已知有缺陷)')
    ap.add_argument("--throttle", type=float, default=1.0,
                    help="GPU duty cycle 上限，例如 0.4 代表算 40%% 休 60%%")
    a = ap.parse_args()
    MAX_K = a.max_k
    if a.throttle < 1.0:
        print(f"⚙️  GPU 節流至 {a.throttle:.0%} duty cycle（降溫用，時間約 {1/a.throttle:.1f}x）")
    if a.gen: gen(a)
    elif a.run: run(a.run, a)
    elif a.report: report()
    else: ap.print_help()
