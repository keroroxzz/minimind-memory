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
RESULTS = os.path.join(HERE, "results_synth.json")   # 由 main() 依 --task 覆寫
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
    # E3：跨迴圈狀態通道。與 loop2/loop3 成對比較 —— 若狀態能替代深度，
    # 加了狀態的 loop2 應該接近沒狀態的 loop3。
    "loop2S": dict(use_engram=False, use_dense_attention=False, num_loops=2,
                   loop_state_channel=True, **LOOP_KW),
    "loop3S": dict(use_engram=False, use_dense_attention=False, num_loops=3,
                   loop_state_channel=True, **LOOP_KW),
    "loop1S": dict(use_engram=False, use_dense_attention=False, num_loops=1,
                   loop_state_channel=True, **LOOP_KW),
}
ENGRAM = dict(engram_offload_cpu=False, engram_layers=[2, 4, 6])

import string
MAX_K = 6                       # 由 --max-k 覆寫
NAMES = string.ascii_lowercase   # 最多支援 k=25
SEQ_LEN = 160                    # mem 任務：記憶區 ~72 + k=12 的鏈 ~30 + 答案 5
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


def make_lookup(k, rng, n_gen=N_GEN):
    """純檢索診斷：只要求把被查詢的定義原樣輸出，完全不含組合。

    這是 match-and-copy（induction head）能力的最小測試。
    若這個都學不會，代表失敗在檢索本身，而不是「檢索 + 組合」的疊加。
    """
    defs, used = [], set()
    while len(defs) < n_gen:
        q = list(range(PERM_N)); rng.shuffle(q); t = tuple(q)
        if q != list(range(PERM_N)) and t not in used:
            used.add(t); defs.append(q)
    mem = " ".join(f"f{i}=" + " ".join(map(str, d)) for i, d in enumerate(defs))
    g = rng.randrange(n_gen)
    return mem + f" | 求f{g}=", " ".join(map(str, defs[g])), defs


KEY_POOL = ["ka", "z7", "p2", "mq", "vb", "x9", "ct", "ry", "jw", "n4", "hs", "d6"]


def make_mem(k, rng, n_gen=N_GEN, distractors=0, shuffle=True, rename=False, cf=None):
    """P2a：生成元定義放在 prompt 的記憶區，每個樣本重抽。

    make_perm 的 f0..f7 是固定的 —— 模型把它們背進權重，那正是「參數知識」。
    這裡每樣本重抽定義，背下來不只沒用而是**主動有害**，模型只能去讀記憶區。

    shuffle：記憶條目順序隨機化。**必須開啟** —— 否則 key 與位置完全相關，
             模型可以用「第幾條」取代 key lookup，容量曲線就量不到真東西。
    rename ：改用隨機 key 符號（ka/z7/...），測是否依賴 f0..f7 的固定語意。
    cf     ：'used'   換掉一條實際被用到的定義，答案**必須**改變 → 測記憶跟隨
             'unused' 換掉一條沒被用到的定義，答案**不應**改變 → 測選擇性
             回傳 (prompt, answer, cf_prompt, cf_answer, 換掉的索引)
    """
    total = n_gen + distractors
    defs, seen = [], set()
    while len(defs) < total:
        q = list(range(PERM_N)); rng.shuffle(q); t = tuple(q)
        if q != list(range(PERM_N)) and t not in seen:
            seen.add(t); defs.append(q)

    names = (rng.sample(KEY_POOL, total) if rename else [f"f{i}" for i in range(total)])
    order = list(range(total))
    if shuffle:
        rng.shuffle(order)

    def render(dfs):
        return " ".join(f"{names[i]}=" + " ".join(map(str, dfs[i])) for i in order)

    state0 = list(range(PERM_N)); rng.shuffle(state0)
    chain = [rng.randrange(n_gen) for _ in range(k)]        # 干擾項永遠不被使用

    def run(dfs):
        st = list(state0)
        for g in chain:
            st = [st[dfs[g][i]] for i in range(PERM_N)]
        return " ".join(map(str, st))

    tail = " | x=" + " ".join(map(str, state0)) + "".join(f" {names[g]}" for g in chain) + " 求x="
    prompt, answer = render(defs) + tail, run(defs)
    if cf is None:
        return prompt, answer, defs

    pool = sorted(set(chain)) if cf == "used" else [i for i in range(total) if i not in set(chain)]
    if not pool:
        return None
    for _ in range(50):
        tgt = pool[rng.randrange(len(pool))]
        alt = [d[:] for d in defs]
        q = list(range(PERM_N)); rng.shuffle(q)
        if q == defs[tgt]:
            continue
        alt[tgt] = q
        cf_ans = run(alt)
        # S5 中換掉生成元後結果仍可能碰巧相同 —— used 配對必須丟棄這種，
        # 否則分不出模型是「跟隨記憶」還是「忽略修改」。
        if (cf == "used") == (cf_ans != answer):
            return prompt, answer, render(alt) + tail, cf_ans, tgt
    return None


def make_inline(k, rng):
    """P2b：runtime 已完成 exact-key lookup，運算元直接內聯在使用處。

    完全沒有 key、沒有搜尋 —— 核心只需要「依序套用交給它的運算元」。
    這是「把選擇搬到核心外面」的極端情形，用來隔離：
      * 核心能不能用交給它的內容計算（本任務）
      * 核心能不能自己找到該用哪一條（mem 任務，已知上限 2 個候選）

    與 perm 任務（定義背在權重裡）相減，即為「從記憶讀取」的深度代價。
    """
    state = list(range(PERM_N)); rng.shuffle(state)
    parts = ["x=" + " ".join(map(str, state))]
    for _ in range(k):
        q = list(range(PERM_N)); rng.shuffle(q)
        if q == list(range(PERM_N)):
            q = [q[1], q[0]] + q[2:]
        state = [state[q[i]] for i in range(PERM_N)]
        parts.append(" ".join(map(str, q)))
    return " | ".join(parts) + " 求x=", " ".join(map(str, state))


def make_remote(k, rng):
    """條件 D：值已解析，但放在**遠端 memory block**，不在使用處旁邊。

    與 inline 的唯一差別是位置 —— 順序仍然定義套用次序，沒有 key、沒有搜尋。
    inline 同時改變了「顯式程度／token 頻寬／locality」三件事；
    這個條件把 locality 單獨拉出來測。

      inline : x=... | p1 | p2 | p3 求x=
      remote : p1 | p2 | p3 || x=... 求x=
    """
    state = list(range(PERM_N)); rng.shuffle(state)
    perms = []
    for _ in range(k):
        q = list(range(PERM_N)); rng.shuffle(q)
        if q == list(range(PERM_N)):
            q = [q[1], q[0]] + q[2:]
        perms.append(q)
        state = [state[q[i]] for i in range(PERM_N)]
    block = " | ".join(" ".join(map(str, q)) for q in perms)
    return block + " || x=" + " ".join(map(str, [*range(PERM_N)][:0]) ) + \
        " ".join(map(str, _remote_start(perms, state))) + " 求x=", " ".join(map(str, state))


def _remote_start(perms, final):
    """由最終狀態與置換序列反推起始狀態（保持與 inline 相同的資料分佈）。"""
    st = list(final)
    for q in reversed(perms):
        inv = [0] * PERM_N
        for i, v in enumerate(q):
            inv[v] = i
        st = [st[inv[i]] for i in range(PERM_N)]
    return st


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
    """四個任務共用同一個生成骨架，差別只在「怎麼產一條樣本」。

    TASKS 把每個任務歸約成一個 (k, rng) -> (prompt, answer) 的函式，
    其餘（去重、train/val 互斥、樣本數控制）完全共用。
    """
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))

    TASKS = {
        # 數值鏈：已知有缺陷，保留供對照。純加減可交換 → 塌縮成求和(TC0)；
        # 含乘法則把答案熵從 3.08 壓到 0.62 bit，長鏈變成猜比算划算。
        "chain":  lambda k, rng: make_one(k, rng, 0, 99, args.ops),
        # S5 置換合成：不可交換且分布不塌縮，量深度的正確工具
        "perm":   lambda k, rng: make_perm(k, rng),
        # 定義搬進 prompt 的記憶區、每樣本重抽 → 背下來主動有害
        "mem":    lambda k, rng: make_mem(k, rng, args.n_gen, args.distractors)[:2],
        # 純檢索（match-and-copy），不含組合。用來分離「檢索」與「運用檢索結果」
        "lookup": lambda k, rng: make_lookup(k, rng)[:2],
        # runtime 解析完成、運算元內聯：隔離「用」與「找」
        "inline": lambda k, rng: make_inline(k, rng),
        # 同樣已解析，但放在遠端 block：把 locality 單獨拉出來測
        "remote": lambda k, rng: make_remote(k, rng),
    }
    make = TASKS[args.task]

    def build(n_per_k, rng, unique, exclude=None):
        rows = []
        for k in range(1, MAX_K + 1):
            seen, made, att = set(), 0, 0
            while made < n_per_k and att < n_per_k * 50:
                att += 1
                pr, a = make(k, rng)
                if unique and pr in seen:
                    continue
                if exclude and pr in exclude:
                    continue
                seen.add(pr)
                rows.append({"k": k, "prompt": pr, "answer": a})
                made += 1
            if made < n_per_k:
                print(f"  ⚠️  k={k} 只產生 {made}/{n_per_k} 條（問題空間已窮盡）")
        return rows

    # val 先生成並去重，train 再排除它們 —— 保證零重疊。
    # 不要改用「切分狀態空間」來做 held-out：那在混合運算下安全，但在純加減下致命
    # （答案永遠黏在 v0 附近，實測訓練值域內 100%、值域外 0%，而 loss 只有 0.02）。
    val_rows = build(args.val_per_k, random.Random(SEED + 999), unique=True)
    if args.task == "mem":
        # 成對反事實必須與基準**同源** —— 早期版本用新的 rng 另外產生一個問題，
        # 只把它的反事實半邊接到既有 row 上，結果是在比較兩個不相干的問題：
        # unused_invariance 恆為 0%（模型明明 100% 正確），S_stale/C_cf 全無意義。
        # 這裡改成從同一次 make_mem 呼叫同時取得基準與反事實。
        cf_rng = random.Random(SEED + 4242)
        paired = []
        for r in val_rows:
            got = make_mem(r["k"], cf_rng, args.n_gen, args.distractors, cf="used")
            if not got:
                continue
            pr, a, cp, ca, _ = got
            row = {"k": r["k"], "prompt": pr, "answer": a,
                   "cf_used_prompt": cp, "cf_used_answer": ca}
            got2 = make_mem(r["k"], cf_rng, args.n_gen, args.distractors, cf="unused")
            if got2:
                pr2, a2, cp2, ca2, _ = got2
                row.update(unused_base_prompt=pr2, unused_base_answer=a2,
                           cf_unused_prompt=cp2, cf_unused_answer=ca2)
            paired.append(row)
        val_rows = paired
        n_u = sum(1 for r in val_rows if "cf_used_prompt" in r)
        n_n = sum(1 for r in val_rows if "cf_unused_prompt" in r)
        print(f"  成對反事實：used {n_u} 對 / unused {n_n} 對（與基準同源）")
        n_u = sum(1 for r in val_rows if "cf_used_prompt" in r)
        n_n = sum(1 for r in val_rows if "cf_unused_prompt" in r)
        print(f"  反事實配對：used {n_u} 對 / unused {n_n} 對")
    val_set = {r["prompt"] for r in val_rows}
    train_rows = build(args.train_per_k, random.Random(SEED), unique=False, exclude=val_set)
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


@torch.no_grad()
def cf_eval(model, tok, val_rows, n=300, n_loops=None):
    """成對反事實評測。回傳四個指標，其中 C_cf 才是真正的門檻。"""
    def gen_one(prompt):
        ids = tok(tok.bos_token + prompt, add_special_tokens=False,
                  return_tensors="pt").input_ids.to(DEVICE)
        kw = {"num_loops": n_loops} if n_loops else {}
        out = model.generate(ids, max_new_tokens=8, do_sample=False,
                             eos_token_id=tok.eos_token_id, **kw)
        return tok.decode(out[0][ids.shape[1]:], skip_special_tokens=True).strip()

    m = dict(full=0, mem=0, stale=0, both=0, n_used=0, unused_inv=0, n_unused=0)
    for r in [x for x in val_rows if "cf_used_prompt" in x][:n]:
        p0, a0 = gen_one(r["prompt"]), r["answer"]
        p1, a1 = gen_one(r["cf_used_prompt"]), r["cf_used_answer"]
        m["n_used"] += 1
        m["full"] += (p0 == a0)
        m["mem"] += (p1 == a1)
        m["stale"] += (p1 == a0)          # 換了記憶卻還是給舊答案
        m["both"] += (p0 == a0 and p1 == a1)
    for r in [x for x in val_rows if "cf_unused_prompt" in x][:n]:
        p0 = gen_one(r["unused_base_prompt"])      # 必須用 unused 配對自己的基準
        p1 = gen_one(r["cf_unused_prompt"])
        m["n_unused"] += 1
        m["unused_inv"] += (p0 == p1)     # 改無關條目，輸出不應變
    u, v = max(m["n_used"], 1), max(m["n_unused"], 1)
    return {"A_full": m["full"]/u, "A_mem": m["mem"]/u, "S_stale": m["stale"]/u,
            "C_cf": m["both"]/u, "unused_invariance": m["unused_inv"]/v,
            "n_used": m["n_used"], "n_unused": m["n_unused"]}


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

    if args.cf_eval and any("cf_used_prompt" in r for r in d["val_rows"]):
        cf = cf_eval(model, tok, d["val_rows"], n=args.cf_n)
        print(f"\n  成對反事實（n={cf['n_used']}）：")
        print(f"    A_full  原始正確率           {cf['A_full']:6.1%}")
        print(f"    A_mem   反事實正確率         {cf['A_mem']:6.1%}")
        print(f"    S_stale 換了記憶仍給舊答案   {cf['S_stale']:6.1%}  ← 越低越好")
        print(f"    C_cf    兩個世界都對         {cf['C_cf']:6.1%}  ← 真正的門檻")
        print(f"    unused  改無關條目輸出不變   {cf['unused_invariance']:6.1%}", flush=True)
        per_k["_cf"] = cf

    tag = f"{args.task}{args.n_gen}" if args.task == "mem" else args.task
    ck = os.path.join(HERE, f"synth_{tag}_{name.replace('+','_')}.pth")
    torch.save({k: v.cpu() for k, v in model.state_dict().items()}, ck)

    res = json.load(open(RESULTS)) if os.path.exists(RESULTS) else {}
    res[name] = {"per_k": {str(k): v for k, v in per_k.items()}, "overall": overall,
                 # 記下訓練分布 —— 天花板 k* 對它極度敏感（同一模型在
                 # k<=12 訓練是 k*~7，在 k<=24 訓練是 k*~2.69）。
                 # 沒有這欄，跨檔比較就會不知不覺比到兩種不同的東西。
                 "train_dist": {"task": args.task, "max_k": MAX_K, "steps": steps,
                                "n_gen": args.n_gen, "ops": args.ops,
                                "train_per_k": None},
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
    ap.add_argument("--cf-eval", type=int, default=1, help="mem 任務跑成對反事實評測")
    ap.add_argument("--cf-n", type=int, default=200, help="反事實配對數")
    ap.add_argument("--n-gen", type=int, default=N_GEN, help="記憶區中的定義數（1=不需搜尋）")
    ap.add_argument("--distractors", type=int, default=0, help="記憶區中未被使用的干擾定義數")
    ap.add_argument("--out", default=None, help="結果檔名，預設 results_<task>.json")
    ap.add_argument("--task", default="perm", choices=["perm", "chain", "mem", "lookup", "inline", "remote"],
                    help='perm=置換合成(預設，真正量深度)；chain=數值鏈(已知有缺陷)')
    ap.add_argument("--throttle", type=float, default=1.0,
                    help="GPU duty cycle 上限，例如 0.4 代表算 40%% 休 60%%")
    a = ap.parse_args()
    MAX_K = a.max_k
    # 每個任務寫自己的結果檔，避免不同任務互相覆蓋
    # （先前得手動 cp 出六個快照才不會弄丟）
    RESULTS = os.path.join(HERE, a.out) if a.out else os.path.join(HERE, f"results_{a.task}.json")
    if a.throttle < 1.0:
        print(f"⚙️  GPU 節流至 {a.throttle:.0%} duty cycle（降溫用，時間約 {1/a.throttle:.1f}x）")
    if a.gen: gen(a)
    elif a.run: run(a.run, a)
    elif a.report: report()
    else: ap.print_help()
