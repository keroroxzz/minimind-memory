"""G1 的 canonical paired renderer（`design_c_layer.md` §5）。

**同一批 canonical 樣本**產生所有條件，逐題配對、hash 驗證：

  L0  values 直接寫在 prompt 裡（positive control，**不是** latent delivery）
  L1  LatentSlots：prompt 不含 values，改由 store 交付
  L2  SyntheticKV：同上，交付形式不同

⚠️ **不可借舊 `inline` 的 99.8%** —— `absent p=0` 與舊 `inline` 的 prompt/latent
分布不同。baseline 必須由同一批樣本 render 出來。
"""
import hashlib
import json
import os
import random
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from dataclasses import dataclass, field

import torch

from model.memory_module import LATENT_DIM, PERM_N, LatentStore, MemoryEntry, perm_to_latent

KEYS = [f"f{i}" for i in range(4)]


@dataclass
class CanonicalSample:
    """一個樣本的**表示無關**核心。三種 render 共用同一個實例。"""
    k: int
    defs: dict                    # key -> permutation（每樣本重抽：背進權重無用且有害）
    present: list                 # 記憶區內的 key（有序）
    state: list
    chain: list                   # 有序、可重複 —— 表達 composition chain
    answer: list
    sample_id: str = field(default="")

    def __post_init__(self):
        if not self.sample_id:
            core = json.dumps({"k": self.k, "defs": {k: self.defs[k] for k in sorted(self.defs)},
                               "present": self.present, "state": self.state,
                               "chain": self.chain, "answer": self.answer},
                              sort_keys=True)
            self.sample_id = hashlib.sha256(core.encode()).hexdigest()[:16]

    @property
    def delivery_id(self) -> str:
        """**model 真正看得到的東西**的指紋。

        `sample_id` 包含未被用到的 defs、key 名字、present 順序 ——
        那些在 oracle 展開之後根本不出現，所以兩個不同的 `sample_id`
        可能 render 成**完全相同**的 `(state, 有序 selected perms, answer)`（Codex 指出）。
        跨 split 的排除必須用這個，不是 `sample_id`。
        """
        core = json.dumps({"k": self.k, "state": self.state,
                           "perms": [self.defs[g] for g in self.chain],
                           "answer": self.answer}, sort_keys=True)
        return hashlib.sha256(core.encode()).hexdigest()[:16]


def make_canonical(k, rng, n_present=2, keys=None):
    """產生一個 canonical 樣本。與 `synth_depth_task.make_absent(p_missing=0)` 同構。

    `keys` 指定可用的 key 身分集合（G2c 用來讓 train/cal/test 完全不交）；
    None 時沿用 G1/G2a/G2b 的 f0..f3。
    """
    KEYS_ = keys if keys is not None else KEYS
    present = sorted(rng.sample(range(len(KEYS_)), n_present))
    defs, seen = {}, set()
    for i in present:
        while True:
            q = list(range(PERM_N)); rng.shuffle(q); t = tuple(q)
            if q != list(range(PERM_N)) and t not in seen:
                seen.add(t); defs[KEYS_[i]] = q; break
    order = [KEYS_[i] for i in present]; rng.shuffle(order)
    state = list(range(PERM_N)); rng.shuffle(state)
    chain = [KEYS_[rng.choice(present)] for _ in range(k)]
    st = list(state)
    for g in chain:
        st = [st[defs[g][i]] for i in range(PERM_N)]
    return CanonicalSample(k=k, defs=defs, present=order, state=state, chain=chain, answer=st)


# ----------------------------------------------------------------------- render

def _nums(xs):
    return " ".join(map(str, xs))


def parse_groups(prompt):
    """從 prompt 切出所有「5 個以空格分隔的數字」群組。

    ⚠️ 不要用 lookbehind regex —— 我第一版寫 `(?<![\\d ])...`，
    那會**排除前面是空格的群組**，而新格式的 value 前面正是 `| `，
    結果 200 題全部誤判。切分比 regex 穩。
    """
    out = []
    for seg in prompt.split("|"):
        seg = seg.strip().removeprefix("x=").removesuffix("求x=").strip()
        parts = seg.split()
        if len(parts) == PERM_N and all(p.isdigit() for p in parts):
            out.append(" ".join(parts))
    return out


PLACEHOLDER = ". . . . ."          # 與一個 value group 同樣是 5 個 token


def render_L0(s: CanonicalSample):
    """**oracle 已把 chain 解成 selected explicit values**，逐步排在使用處。

    ⚠️ **不可給 definition block、不可留 f-key**（Codex 指出的致命偏差）。
    先前版本是 `f3=… f0=… | x=S f3 求x=` —— 那是 **core-select pointer 任務**，
    core 仍要把 f-key 比對到定義區，正是已知的 binding 瓶頸。
    拿它當 positive control 會把 pointer carrier 與 matching 混進 G1，
    違反「oracle-selection 固定前端」的設計。
    """
    steps = " ".join(f"| {_nums(s.defs[g])}" for g in s.chain)
    return f"| x={_nums(s.state)} {steps} 求x=", _nums(s.answer)


def render_latent(s: CanonicalSample):
    """L1/L2：每個 value group 換成**中性 placeholder**，資訊改由 delivery 提供。

    長度、邊界、步數位置與 L0 **完全一致** —— 只有「該位置有沒有值」在變。
    """
    steps = " ".join(f"| {PLACEHOLDER}" for _ in s.chain)
    return f"| x={_nums(s.state)} {steps} 求x=", _nums(s.answer)


# --------------------------------------------------------- G2a：retrieve 的資料層

POOL_SIZE = 8            # 固定 8 條 —— missing 題也補到 8，避免用「數量」判 missing
ALL_KEYS = [f"f{i}" for i in range(48)]      # G2c 需要每個 split 各 ≥8 個 key


def build_pool(s: CanonicalSample, rng, missing: bool = False, keys=None, pool_size=None):
    """回傳 (pool_symbols, addr_bank, latents_by_symbol, query_keys, target_idx, hit)。

    ⚠️ **render 完全不動**（仍是 5 個 dot）—— key 走 out-of-band metadata（Codex）。
       把首個 dot 換成 key 會同時改動 `zdelta` 依賴的 native scaffold，
       那樣就不只是在測 retrieve。

    ⚠️ **pool 基數恆為 8**：missing 題移除一條 required、補一條額外 distractor，
       否則模型可以用「pool 少了一條」判 missing，而不是真的比對。
    """
    n_pool = pool_size or POOL_SIZE
    required = list(dict.fromkeys(s.chain))                 # 有序去重
    universe = keys if keys is not None else ALL_KEYS
    others = [k for k in universe if k not in required]
    rng.shuffle(others)
    dropped = None
    if missing and required:
        dropped = required[rng.randrange(len(required))]
        required = [k for k in required if k != dropped]
    others = [k for k in others if k != dropped]            # dropped 不得回到 pool
    pool = required + others[:n_pool - len(required)]       # 基數恆定
    assert len(pool) == n_pool, \
        f"key universe 只有 {len(universe)} 個，補不滿 pool={n_pool}（需 ≥ pool+1）"
    rng.shuffle(pool)

    lat = {}
    for k in pool:
        lat[k] = perm_to_latent(s.defs[k]) if k in s.defs else _rand_perm_latent(k, rng)
    target = [pool.index(g) if g in pool else -1 for g in s.chain]
    hit = [t >= 0 for t in target]
    return pool, lat, list(s.chain), target, hit, dropped


def _rand_perm_latent(sym, rng):
    """distractor 的內容 —— 隨機置換，與 required 同分布。"""
    q = list(range(PERM_N)); rng.shuffle(q)
    return perm_to_latent(q)


def render_retrieval_view(s: CanonicalSample):
    """G2b 的 **out-of-band retrieval view** —— 與 delivery 的 prompt 完全分離。

    ⚠️ **不可把 key 塞回 carrier span**（Codex）：delivery 的 prompt 仍是 5 個 dot，
       動它就同時改了 `zdelta` 依賴的 scaffold（[88] 已被擋下同一個錯）。
    ⚠️ 也**不能**取 carrier span 上的 hidden —— 那裡全是 dot，**沒有 key 資訊**。

    所以另開一段序列給凍結的 core 編碼，query 取這裡的 hidden。
    可見的 token 明列如下：**狀態 + 有序的 key 鏈**。

        | x=STATE | f3 f0 f1 求?

    第 i 個 key token 的 hidden 因此同時帶有
    **該 key 的身分**與**前序 context（狀態、前面用過哪些 key）**。
    """
    keys = " ".join(s.chain)
    return f"| x={_nums(s.state)} | {keys} 求?"


def retrieval_view_key_positions(tok, s):
    """retrieval view 裡每個 key 的**最後一個 token** 的位置（含 BOS 偏移）。"""
    view = render_retrieval_view(s)
    head = f"| x={_nums(s.state)} |"
    base = len(tok(tok.bos_token + head, add_special_tokens=False).input_ids)
    pos, cur = [], base
    for g in s.chain:
        n = len(tok(" " + g, add_special_tokens=False).input_ids)
        cur += n
        pos.append(cur - 1)          # 該 key 的最後一個 token
    return view, torch.tensor(pos)


def render_write_view(key: str) -> str:
    """G2c：write 時用來產生該 key **身分表示**的 canonical 序列。

    與 retrieval view 分開，且**不含任何 context** —— address 只依賴 key 身分，
    這樣同一個 key 在不同題目裡的 address 才會一致。
    """
    return f"| {key} 存"


# ---- canonical key span：**renderer invariant，不是量測腳本的細節**（Codex [92] ACK）

def canonical_key_ids(tok, key):
    """key 的 canonical token 序列 —— **必須含前導空白**。

    這個 tokenizer 是 context-dependent 的：`"f0"→[105,51]` 但 `" f0"→[341,51]`。
    key 在 write view (`| f41 存`) 與 retrieval view (`| x=… | f3 f0 f1 求?`) 裡
    **一律**前面有空白，所以 canonical 形式取 `" "+key`。
    用不帶空白的形式量測，會虛報碰撞（[91] 一度報 28 對，實為 7 對且非精確重疊）。
    tied encoder 的「兩側同一切法」前提就靠這個函式唯一化。
    """
    return tok(" " + key, add_special_tokens=False).input_ids


def locate_key_span(tok, view: str, key: str):
    """在任一 view 裡定位 key 的完整 canonical span，回傳 (ids, positions)。"""
    ids = tok(tok.bos_token + view, add_special_tokens=False).input_ids
    kid = canonical_key_ids(tok, key)
    for i in range(len(ids) - len(kid) + 1):
        if ids[i:i + len(kid)] == kid:
            return torch.tensor(ids), list(range(i, i + len(kid)))
    raise AssertionError(f"{key}: canonical span {kid} 不在 {view!r} → {ids} 裡 —— 切法不一致")


# G2c 的 key 身分切分：train / cal / test **完全不交**，才測得到 unseen-key 泛化。
#
# ⚠️ 每個 split 至少要 **POOL_SIZE + 1** 個，不是 POOL_SIZE：
#    missing 題要把 dropped key 排除在 pool 之外、又得把 pool 補滿 8 條
#    （基數恆為 8 是為了不讓模型用「少一條」判 missing）。
#    split 恰好 8 個時 others 湊不滿，`build_pool` 的 assert 會直接失敗。
KEYS_TRAIN = [f"f{i}" for i in range(28)]
KEYS_CAL = [f"f{i}" for i in range(28, 38)]
KEYS_TEST = [f"f{i}" for i in range(38, 48)]
assert not (set(KEYS_TRAIN) & set(KEYS_CAL)) and not (set(KEYS_CAL) & set(KEYS_TEST)) \
    and not (set(KEYS_TRAIN) & set(KEYS_TEST))
for _n, _s in (("train", KEYS_TRAIN), ("cal", KEYS_CAL), ("test", KEYS_TEST)):
    assert len(_s) > POOL_SIZE, f"{_n} split 只有 {len(_s)} 個 key，missing 題補不滿 pool"

# pool scaling（8→16→32）用的 **純 distractor 身分**：從不當 query target，
# 也從不進 train/cal —— scaling 只加 distractor，不改任何 split 的身分。
# f48..f159 實測 canonical span ≤3 token（在 MAX_SPAN 內）且彼此不重複。
KEYS_DISTRACTOR = [f"f{i}" for i in range(48, 160)]
assert not (set(KEYS_DISTRACTOR) & (set(KEYS_TRAIN) | set(KEYS_CAL) | set(KEYS_TEST)))

# ---- G2c-cal-v2 的**全新未查看**身分集合（§4.29 預先登記）
#
# 舊 test（f38..f47）已被查看過，不可拿擴大 cal 後再測同一批當 confirmatory。
# cal2 / test2 各 40 個、由同一 generator 固定 seed IID 抽出、**數量事前定死**，
# 不因 cosine 分布重生 —— 否則修法會退化成 hard-negative 策展（Codex）。
_v2 = list(KEYS_DISTRACTOR)
random.Random(20260804).shuffle(_v2)
KEYS_CAL2, KEYS_TEST2 = sorted(_v2[:40]), sorted(_v2[40:80])
assert len(KEYS_CAL2) == len(KEYS_TEST2) == 40
assert not (set(KEYS_CAL2) & set(KEYS_TEST2))
assert not ((set(KEYS_CAL2) | set(KEYS_TEST2)) &
            (set(KEYS_TRAIN) | set(KEYS_CAL) | set(KEYS_TEST)))


def build_store(s: CanonicalSample) -> LatentStore:
    """每樣本重建 store —— 定義每樣本重抽，背進權重無用且有害。

    store **凍結**（無梯度、無 learned write policy），但內容是 per-episode 的。
    """
    st = LatentStore()
    st.commit([MemoryEntry(k, perm_to_latent(s.defs[k]), {"src": "oracle"})
               for k in s.present])
    return st


def resolve_chain(s: CanonicalSample):
    """oracle 把 chain 解成**有序、可重複**的 latent list。"""
    return torch.stack([perm_to_latent(s.defs[g]) for g in s.chain])


# ----------------------------------------------------------------------- 資料集

def build_dataset(max_k, n_per_k, seed, exclude=None, keys=None):
    """去重與跨 split 排除都用 **`delivery_id`**。

    - 不可用 render 出來的字串：render 形式會影響抽樣（§4.18 的同一個病）
    - 不可用 `sample_id`：它含 model 看不到的欄位（未用到的 defs、key 名字、
      present 順序），實測 train/val 的 `sample_id` 交集為 0 但
      **`delivery_id` 交集為 1** —— 那是真的重疊（Codex 預測到的）
    """
    rng = random.Random(seed)
    exclude = set(exclude or ())
    out = []
    for k in range(1, max_k + 1):
        seen = set()
        made = attempts = 0
        while made < n_per_k and attempts < n_per_k * 50:
            attempts += 1
            s = make_canonical(k, rng, keys=keys)
            did = s.delivery_id
            if did in seen or did in exclude:
                continue
            seen.add(did); out.append(s); made += 1
    return out


def pairing_checksum(samples):
    return hashlib.sha256("\n".join(s.sample_id for s in samples).encode()).hexdigest()[:16]


def delivery_checksum(samples):
    """**model 可見內容**的指紋。split disjoint 以此為準，不是 pairing_checksum。"""
    return hashlib.sha256("\n".join(s.delivery_id for s in samples).encode()).hexdigest()[:16]


def encode(tok, prompt, answer, seq_len):
    """tokenize 並產生 labels：**只有 answer(+EOS) 進 loss**，prompt 全部 ignore。

    renderer 的字串正確不代表 encode 正確 —— 這一層要單獨驗（Codex）。
    """
    p = tok(tok.bos_token + prompt, add_special_tokens=False)["input_ids"]
    a = tok(answer + tok.eos_token, add_special_tokens=False)["input_ids"]
    ids = p + a
    if len(ids) > seq_len:
        return None
    labels = [-100] * len(p) + a
    pad = seq_len - len(ids)
    return (ids + [tok.pad_token_id] * pad, labels + [-100] * pad, len(p), len(ids))


if __name__ == "__main__":
    print("=" * 62)
    print("  G1 canonical paired renderer —— 三關檢查")
    print("=" * 62)
    ok = True

    ds = build_dataset(max_k=4, n_per_k=50, seed=42)
    ds2 = build_dataset(max_k=4, n_per_k=50, seed=42)
    print(f"\n樣本 {len(ds)} 個，pairing checksum {pairing_checksum(ds)}")

    # 關卡 1：canonical hash pairing
    print("\n關卡 1：三種 render 必須來自同一批樣本")
    same = pairing_checksum(ds) == pairing_checksum(ds2)
    print(f"  {'✅' if same else '❌'} 決定性：同 seed 兩次產生的 checksum 相同")
    ok &= same
    per_render = {}
    for name, fn in [("L0", render_L0), ("latent", render_latent)]:
        per_render[name] = hashlib.sha256(
            "\n".join(f"{s.sample_id}:{fn(s)[1]}" for s in ds).encode()).hexdigest()[:16]
    aligned = per_render["L0"] == per_render["latent"]
    print(f"  {'✅' if aligned else '❌'} L0 與 latent 的 (sample_id, answer) 序列一致："
          f"{per_render['L0']}")
    ok &= aligned

    # 關卡 2：L0 input/label alignment
    # ⚠️ 必須從 **prompt 裡實際出現的 value groups** 獨立 parse 並 replay，
    #    不可用 s.defs/s.chain 重算 —— 那只是在驗證我自己的內部一致性（Codex）。
    print("\n關卡 2：L0 的 input 與 label 對齊（從 prompt 獨立 replay）")
    import re as _re
    bad = []
    for s in ds:
        p, a = render_L0(s)
        groups = parse_groups(p)
        if len(groups) != s.k + 1:                       # state + k 個 value
            bad.append((s.sample_id, "群組數不符")); continue
        st = [int(x) for x in groups[0].split()]
        for g in groups[1:]:
            q = [int(x) for x in g.split()]
            st = [st[q[i]] for i in range(PERM_N)]
        if _nums(st) != a:
            bad.append((s.sample_id, "replay 不等於 label"))
    print(f"  {'✅' if not bad else '❌'} {len(ds)} 題可由 prompt 內的 value groups "
          f"獨立 replay 出 label{'' if not bad else f'  例：{bad[0]}'}")
    ok &= not bad
    has_key = [s.sample_id for s in ds if _re.search(r"f\d", render_L0(s)[0])]
    print(f"  {'✅' if not has_key else '❌'} L0 prompt **不含任何 f-key**"
          f"（否則就退化成 pointer 任務）")
    ok &= not has_key

    # latent render 不得洩漏 value。
    # ⚠️ 不可用「defs 的字串是否出現在 prompt 裡」判斷 —— state 本身就是一個置換，
    #    某個 def 恰好等於 state 的機率約 1.7%/def，實測 200 題誤判 6 題。
    #    正確做法是**數 5 個數字的群組個數**：latent prompt 只該有 state 一組。
    leak = []
    for s in ds:
        pr = render_latent(s)[0]
        g = parse_groups(pr)
        if len(g) != 1 or g[0] != _nums(s.state):
            leak.append((s.sample_id, g))
    print(f"  {'✅' if not leak else '❌'} latent prompt 只含 state 一組數字，無任何 value"
          f"{'' if not leak else f'  例：{leak[0]}'}")
    ok &= not leak
    shape_ok = all(len(parse_groups(render_L0(s)[0])) == s.k + 1 for s in ds)
    print(f"  {'✅' if shape_ok else '❌'} 對照：L0 的 prompt 有 k+1 組數字（state + k 個 value）")
    ok &= shape_ok
    same_len = all(len(render_L0(s)[0].split()) == len(render_latent(s)[0].split()) for s in ds)
    print(f"  {'✅' if same_len else '❌'} L0 與 latent 的 token 數完全相同"
          f"（只有『該位置有沒有值』在變）")
    ok &= same_len

    # 關卡 3：store / oracle 一致性
    print("\n關卡 3：store 交付的內容與 L0 寫在 prompt 裡的相同")
    from model.memory_module import latent_to_perm
    mism = []
    for s in ds[:200]:
        store = build_store(s)
        for e, g in zip(store.read(s.chain), s.chain):
            if e is None or latent_to_perm(e.latent) != s.defs[g]:
                mism.append(s.sample_id)
        lat = resolve_chain(s)
        if lat.shape != (s.k, LATENT_DIM):
            mism.append(s.sample_id)
    print(f"  {'✅' if not mism else '❌'} oracle 解出的 latent 與 defs 逐項相同，"
          f"且形狀為 (k, {LATENT_DIM})")
    ok &= not mism

    # 關卡 4：train/val 的 canonical ID 必須不相交
    print("\n關卡 4：train / val 的 canonical ID 不相交")
    tr = build_dataset(max_k=4, n_per_k=50, seed=42)
    va = build_dataset(max_k=4, n_per_k=20, seed=42 + 999,
                       exclude={x.delivery_id for x in tr})   # val 明確排除 train
    inter = {s.sample_id for s in tr} & {s.sample_id for s in va}
    print(f"  {'✅' if not inter else '❌'} 交集 {len(inter)} 個"
          f"（train {len(tr)} / val {len(va)}）")
    print(f"     train sample/delivery checksum {pairing_checksum(tr)} / {delivery_checksum(tr)}")
    print(f"     val   sample/delivery checksum {pairing_checksum(va)} / {delivery_checksum(va)}")
    ok &= not inter

    # 關卡 5：**實際 tokenizer** 的長度，不是 whitespace 欄位數
    print("\n關卡 5：用正式 tokenizer 驗長度（先前只數了空白分隔的欄位）")
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(os.path.join(os.path.dirname(__file__), "..", "model"))
    SEQ = 192
    mism, maxlen, dropped = [], 0, {}
    for s in ds:
        e0 = encode(tok, render_L0(s)[0], render_L0(s)[1], SEQ)
        e1 = encode(tok, render_latent(s)[0], render_latent(s)[1], SEQ)
        if e0 is None or e1 is None:
            dropped[s.k] = dropped.get(s.k, 0) + 1
            continue
        maxlen = max(maxlen, e0[3], e1[3])
        if e0[2] != e1[2] or e0[3] != e1[3]:
            mism.append((s.sample_id, e0[2], e1[2]))
    print(f"  {'✅' if not mism else '❌'} L0 與 latent 的 **實際 token 數**逐題相同"
          f"{'' if not mism else f'  例：{mism[0]}'}")
    ok &= not mism
    print(f"  {'✅' if not dropped else '❌'} 無樣本超長被丟棄（max_len {maxlen}/{SEQ}）"
          f"{'' if not dropped else f'  依 k：{dropped}'}")
    ok &= not dropped
    # ⚠️ 先前只驗 ds[0]，那等於只驗 k=1。要逐題驗，否則 k>1 時
    #    context-dependent 的 token merge 可能讓 span 錯位而測不到（Codex）。
    span_bad = []
    for s in ds:
        a = tok(render_L0(s)[0], add_special_tokens=False).input_ids
        b = tok(render_latent(s)[0], add_special_tokens=False).input_ids
        if len(a) != len(b):
            span_bad.append((s.sample_id, "長度不符")); continue
        d = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
        if len(d) != s.k * PERM_N:
            span_bad.append((s.sample_id, f"相異 {len(d)} != {s.k * PERM_N}"))
    print(f"  {'✅' if not span_bad else '❌'} **逐題**相異 token 恰為 k×{PERM_N} 個，"
          f"其餘逐位相同{'' if not span_bad else f'  例：{span_bad[0]}'}")
    ok &= not span_bad

    # 關卡 6：delivery_id 才是 model 可見的重疊判準
    print("\n關卡 6：跨 split 以 delivery_id 排除（sample_id 不夠）")
    dup_sid_diff_did = len({s.sample_id for s in ds}) - len({s.delivery_id for s in ds})
    print(f"     ds 內 sample_id {len({s.sample_id for s in ds})} 個 → "
          f"delivery_id {len({s.delivery_id for s in ds})} 個（多對一 {dup_sid_diff_did}）")
    inter_d = {s.delivery_id for s in tr} & {s.delivery_id for s in va}
    print(f"  {'✅' if not inter_d else '❌'} train/val 的 delivery_id 交集 {len(inter_d)} 個")
    ok &= not inter_d
    r_tr = {(render_L0(s)[0], render_L0(s)[1]) for s in tr}
    r_va = {(render_L0(s)[0], render_L0(s)[1]) for s in va}
    print(f"  {'✅' if not (r_tr & r_va) else '❌'} 直接比對 (L0 prompt, answer) 交集 "
          f"{len(r_tr & r_va)} 個")
    ok &= not (r_tr & r_va)

    # 關卡 6.5：production 規模（max_k=24）的長度／dropped
    # max_len 43 只來自 k≤4 的 smoke，不能代表 k=24（Codex）。
    print("\n關卡 6.5：production max_k=24 的長度與 dropped")
    prod = build_dataset(max_k=24, n_per_k=40, seed=7)
    pmax, pdrop, lens_by_k = 0, {}, {}
    for s in prod:
        e = encode(tok, render_L0(s)[0], render_L0(s)[1], SEQ)
        if e is None:
            pdrop[s.k] = pdrop.get(s.k, 0) + 1; continue
        pmax = max(pmax, e[3]); lens_by_k.setdefault(s.k, set()).add(e[3])
    print(f"  {'✅' if not pdrop else '❌'} k=1..24 無 dropped（max_len {pmax}/{SEQ}）"
          f"{'' if not pdrop else f'  依 k：{pdrop}'}")
    ok &= not pdrop
    fixed = all(len(v) == 1 for v in lens_by_k.values())
    print(f"  {'✅' if fixed else '❌'} 同 k 的長度固定（結構決定，小樣本即可代表）"
          f"；k=24 為 {sorted(lens_by_k[24])[0]} tokens")
    ok &= fixed

    # 關卡 7：loss masking
    print("\n關卡 7：labels 只含 answer(+EOS)")
    bad_lbl = []
    for s in ds[:200]:
        for render in (render_L0, render_latent):
            pr, an = render(s)
            ids, labels, plen, tlen = encode(tok, pr, an, SEQ)
            kept = [i for i, l in enumerate(labels) if l != -100]
            if kept != list(range(plen, tlen)):
                bad_lbl.append((s.sample_id, "非 ignore 的位置不等於 answer 區間")); break
            dec = tok.decode([labels[i] for i in kept], skip_special_tokens=True).strip()
            if dec != an:
                bad_lbl.append((s.sample_id, f"decode={dec!r} != {an!r}")); break
    print(f"  {'✅' if not bad_lbl else '❌'} 由 labels 反解逐題等於 answer，"
          f"prompt/value/placeholder 全不進 loss{'' if not bad_lbl else f'  例：{bad_lbl[0]}'}")
    ok &= not bad_lbl

    ex = ds[2]
    print(f"\n範例（k={ex.k}, id={ex.sample_id}）")
    print(f"  L0      {render_L0(ex)[0]}")
    print(f"  latent  {render_latent(ex)[0]}")
    print(f"  answer  {render_L0(ex)[1]}")
    art = {"tokenizer": os.path.basename(os.path.abspath(
               os.path.join(os.path.dirname(__file__), "..", "model"))),
           "seq_len": SEQ, "max_len_k4": maxlen, "max_len_k24": pmax,
           "train_delivery_checksum": delivery_checksum(tr),
           "val_delivery_checksum": delivery_checksum(va),
           "train_sample_checksum": pairing_checksum(tr),
           "val_sample_checksum": pairing_checksum(va),
           "n_train": len(tr), "n_val": len(va), "placeholder": PLACEHOLDER,
           "latent_dim": LATENT_DIM, "gates_passed": bool(ok)}
    out = os.path.join(os.path.dirname(__file__), "g1_renderer_artifact.json")
    json.dump(art, open(out, "w"), indent=2, ensure_ascii=False)
    print(f"\nartifact -> {os.path.basename(out)}")

    print("\n" + "=" * 62)
    print(f"  {'全部通過' if ok else '有失敗'}")
    sys.exit(0 if ok else 1)


# --------------------------------------------------------- G3a：write formation 的資料層

def render_define_view(key: str, perm) -> str:
    """G3a：writer 觀察到的**事件**（一次寫入的來源）。

        | f3 = 3 1 0 2 4 定

    writer 必須從這裡**形成** latent，而不是直接拿 `perm_to_latent(perm)`。
    形成出來的 latent 要能被**凍結的、獨立訓練過的** `zdelta` 交付介面消費 ——
    那才是「可分離、可抽換的記憶模組」這個主張的實測。

    ⚠️ 這裡刻意**不含 chain、不含 state** —— 寫入是 per-entry 的，
       不可以偷看下游要問什麼，否則測到的是「看題目寫答案」而非 write formation。
    """
    return f"| {key} = {_nums(perm)} 定"


def define_view_value_span(tok, key, perm):
    """define view 裡 permutation 那 5 個 token 的位置（writer 的讀取點）。"""
    view = render_define_view(key, perm)
    head = f"| {key} ="
    base = len(tok(tok.bos_token + head, add_special_tokens=False).input_ids)
    n = len(tok(" " + _nums(perm), add_special_tokens=False).input_ids)
    assert n == PERM_N, f"value span 應為 {PERM_N} 個 token，實得 {n}：{perm}"
    return view, list(range(base, base + n))


# G3a 的 **permutation** 三分：train/val 用不交的置換集合，測 write 的泛化。
# （key 身分的泛化是 G2c 的事，已判 open-set calibration fail；這裡固定用 f0..f3。）
def perm_splits(seed=20260804, n_val=24):
    """S₅ 的 119 個非恆等置換（`make_canonical` 排除恆等），切成 train / val。"""
    import itertools
    allp = [list(p) for p in itertools.permutations(range(PERM_N))
            if list(p) != list(range(PERM_N))]
    rng = random.Random(seed)
    rng.shuffle(allp)
    return allp[n_val:], allp[:n_val]


# ------------------------------------------- G4b：指稱／時序 binding 的資料層

FILLER = "| 略"          # 不引入任何 entity 的填充事件


def render_stream(writes, n_filler):
    """一段**有序的事件 stream**：若干次 literal 寫入 + 若干個填充事件。

        | f2 = 4 0 1 3 2 定 | f9 = 1 2 0 4 3 定 | f7 = 3 1 0 2 4 定 | 略 | 略

    最後一次寫入的 entity 就是「前者」的指涉對象；填充事件只拉開距離、
    **不改變指涉**，這樣 `distance` 才是乾淨的分層變數。
    """
    ev = " ".join(render_define_view(k, p) for k, p in writes)
    return (ev + " " + " ".join([FILLER] * n_filler)).strip()


def render_reference_view(stream: str) -> str:
    """G4b 的 **out-of-band 指稱 view** —— resolver 讀這裡，**交付的 prompt 不動**。

    ⚠️ 這是 §4.27 就立過的 invariant：**交付用的 prompt 必須維持凍結 core
       訓練過的格式**（`| x=… | … 求x=`）。第一版把 stream 前綴與 `（前者）`
       後綴直接塞進交付 prompt，oracle resolver 的天花板因此掉到 **0.0%** ——
       core 根本不認得那個格式。指稱要另開一段序列，與 G2b 的 retrieval view 同理。

        | f4 = … 定 | f18 = … 定 | f29 = … 定 | 略 | 略 求?（前者）

    ⚠️ 指稱放在**讀取端**是刻意的：錯指到**另一個已存在的 entity** 時，
       exact-membership guard 會放行（那個 key 確實在 store 裡），
       於是**自信地交付錯誤內容** —— 那才是這一關要測的危險錯誤。
       若把指稱放在寫入端，錯指只會讓目標 key 沒有內容、退化成安全的 abstain。
    """
    return f"{stream} 求?（前者）"


def reference_view_key_positions(tok, stream, writes):
    """G4b-B：reference view 裡**每個 entity 各自** canonical key span 的最後一個 token。

    與 G2b 的 `retrieval_view_key_positions` 同構 —— 這是本專案既有的 readout 介面。
    ⚠️ 同一個 key 若在 stream 裡出現多次，取**第一次**（`make_reference_episode`
       用 `rng.sample`，本來就不重複）。
    """
    view = render_reference_view(stream)
    ids = tok(tok.bos_token + view, add_special_tokens=False).input_ids
    pos = []
    for kx, _ in writes:
        kid = canonical_key_ids(tok, kx)
        for i in range(len(ids) - len(kid) + 1):
            if ids[i:i + len(kid)] == kid:
                pos.append(i + len(kid) - 1); break
        else:
            raise AssertionError(f"{kx} 的 canonical span 不在 reference view 裡")
    return torch.tensor(ids), pos


def make_reference_episode(rng, keys, perms, n_entity, n_filler, k=1):
    """回傳 (stream, writes, target, s)。

    `writes` 是**有序**的 (key, perm) 列表；`target` = 最後一次寫入的 key。
    所有被寫入的 entity 都**真的進 store** —— 錯指才會落在「已存在的別條」上。
    """
    ks = rng.sample(keys, n_entity)
    ws = [(k_, list(perms[rng.randrange(len(perms))])) for k_ in ks]
    target, tperm = ws[-1]
    state = list(range(PERM_N)); rng.shuffle(state)
    st = list(state)
    for _ in range(k):
        st = [st[tperm[i]] for i in range(PERM_N)]
    s = CanonicalSample(k=k, defs={target: tperm}, present=[target], state=state,
                        chain=[target] * k, answer=st)
    return render_stream(ws, n_filler), ws, target, s
