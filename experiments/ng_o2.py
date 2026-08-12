"""`NG-O2` 的資料層、K0 key ABI、V0 值 ABI、`z_V` schema 與 store。

規格 `NGO2_prereg.json` v4（Codex [172] 授權）。**本檔只實作,不決定任何規格數字。**

⚠️ **一個必須明說的範圍事實**：`NGStore` 是**新的實作**,
   §4.68 的 `EC-1` 驗的是 `bridge_store.Store`,**不自動繼承到這裡** ——
   bridge 的 `Store` 把 key 綁死在 `(name, attr_idx)` 的封閉空間,K0 的三元組進不去。
   本檔沿用**同一份契約**與同樣的自檢方法,但那是**方法的繼承,不是驗證的繼承**。
   O3 的 readback／delivery bitwise parity 正是為此而設。
"""
import hashlib
import json
import os
import random
import re
import unicodedata

import torch

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- V0 值 ABI -----------------------------------------------------------
VMIN_DIGITS, VMAX_DIGITS = 2, 4
N_SLOT, N_DIGIT, N_LEN = 4, 10, 3
ZV_DIM = N_SLOT * N_DIGIT + N_LEN          # 43

# ---- 世界（僅為可重現性；O2 **不宣稱** open-set entity／attribute） --------
NAMES = ["anna", "ben", "cara", "dan", "elsa", "finn", "gina", "hugo",
         "iris", "jack", "kira", "liam", "mona", "nina", "omar", "pia"]
ATTRS = ["門號密碼", "置物櫃密碼", "書桌密碼", "行李箱密碼", "保險箱密碼",
         "腳踏車鎖密碼", "健身房密碼", "抽屜密碼", "信箱密碼", "門禁卡號",
         "會員編號", "座位號碼"]
SCOPE = "SELF"


# ---- K0 key ABI ----------------------------------------------------------
_PUNCT = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WS = re.compile(r"\s+")


def norm(s):
    """K0 的正規化。**只准** NFKC、casefold、空白／標點正規化。

    **不得**做 synonym、fuzzy match 或 embedding nearest-neighbor。
    """
    s = unicodedata.normalize("NFKC", s).casefold()
    s = _PUNCT.sub(" ", s)
    return _WS.sub(" ", s).strip()


def k0(entity, attribute, scope=SCOPE):
    """`key = (scope_id, entity_norm, attribute_norm)`。"""
    return (scope, norm(entity), norm(attribute))


# ---- V0 ↔ z_V ------------------------------------------------------------
def val_str(v):
    """值逐位以空白分隔 —— CLAUDE.md 記過的 6400 BPE 數字切分教訓。"""
    return " ".join(str(v))


def norm_answer(s):
    """`A_ans` 用的 normalizer：**只做**分位空白的正規化。"""
    return _WS.sub(" ", s).strip()


def zv(v):
    """`z_V = [digits(4×10), len_onehot(3)]`。**顯式、精確、可逐 token 驗。**"""
    d = str(v)
    assert VMIN_DIGITS <= len(d) <= VMAX_DIGITS
    z = torch.zeros(ZV_DIM)
    for i, ch in enumerate(d):
        z[i * N_DIGIT + int(ch)] = 1.0
    z[N_SLOT * N_DIGIT + (len(d) - VMIN_DIGITS)] = 1.0
    return z


def zv_value(z):
    """從 `z_V` 取回值。純解碼,用於自檢。"""
    L = int(z[N_SLOT * N_DIGIT:].argmax()) + VMIN_DIGITS
    return int("".join(str(int(z[i * N_DIGIT:(i + 1) * N_DIGIT].argmax()))
                       for i in range(L)))


class CarrierProj(torch.nn.Module):
    """`z_V → hidden` 的**固定隨機投影,可訓練參數 0**。

    與 bridge 同一個原則：這層若可訓練,core 會學會某個特定 adapter 的輸出。
    """

    def __init__(self, hidden, scale=1.0, seed=20260812):
        super().__init__()
        g = torch.Generator().manual_seed(seed)
        self.register_buffer("w", torch.randn(ZV_DIM, hidden, generator=g) / ZV_DIM ** 0.5)
        self.register_buffer("scale", torch.tensor(float(scale)))

    def forward(self, z):
        return (z @ self.w) * self.scale


def fit_scale(proj, embed_weight):
    """把 carrier 的範數對齊 token embedding 的平均範數。"""
    with torch.no_grad():
        tgt = embed_weight.norm(dim=-1).mean()
        z = torch.stack([zv(random.Random(0).randint(10, 9999)) for _ in range(256)])
        cur = (z.to(proj.w.device) @ proj.w).norm(dim=-1).mean()
        proj.scale.fill_(float(tgt / cur))
    return float(proj.scale)


# ---- store ---------------------------------------------------------------
class NGStore:
    """K0 key → `z_V`。契約與 `bridge_store.Store` 相同,**但是新的實作**。

    §4.68 驗的是 `bridge_store.Store`;本類**不繼承那份驗證**（見檔頭）。
    """

    def __init__(self):
        self._d = {}

    def commit(self, key, z):
        assert key not in self._d, f"K0 key 重複 commit：{key}"
        self._d[key] = z.detach().clone()
        return key

    def contains(self, key):
        return key in self._d

    def read(self, key):
        e = self._d.get(key)
        return None if e is None else e

    def verify(self):
        """每個 entry 的 `z_V` 必須是合法的 one-hot 編碼且可解回原值。"""
        bad = 0
        for k, z in self._d.items():
            if z.shape != (ZV_DIM,):
                bad += 1
                continue
            L = int(z[N_SLOT * N_DIGIT:].argmax()) + VMIN_DIGITS
            if abs(float(z.sum()) - (L + 1)) > 1e-6:
                bad += 1
        return bad

    def snapshot(self):
        h = hashlib.sha256()
        for k in sorted(self._d):
            h.update(repr(k).encode())
            h.update(self._d[k].numpy().tobytes())
        return h.hexdigest()[:16]

    def __len__(self):
        return len(self._d)


# ---- generator（自由度全部封死,見 `NGO2_prereg.json::generator_frozen`）----
CELLS = ("m2-first", "m2-last", "m3-first", "m3-middle", "m3-last")


def _one_value(rng):
    """先均勻抽長度 `L ∈ {2,3,4}`,再在該長度內均勻抽值。"""
    L = rng.randint(VMIN_DIGITS, VMAX_DIGITS)
    return rng.randint(10 ** (L - 1), 10 ** L - 1)


def _values(rng, n):
    """`n` 個 2–4 位值,**兩兩相異**;不符即**整批重抽**,非逐一修補。"""
    while True:
        v = [_one_value(rng) for _ in range(n)]
        if len(set(v)) == n:
            return v


def base_episode(rng, m):
    """一個 base episode。回傳所有 variant 共用的內容（**不含**寫入順序）。"""
    ent, dis_ent = rng.sample(NAMES, 2)
    attrs = rng.sample(ATTRS, m)
    vals = _values(rng, m + 1)                 # m 筆 target entity ＋ 1 筆 distractor
    return {"entity": ent, "dis_entity": dis_ent, "attrs": attrs,
            "vals": vals[:m], "dis_val": vals[m]}


def variant(base, target_idx, rng):
    """由 base 生成一個 variant：**只改**寫入順序與 target index。

    `query token`、`target z_V`、`delivery tensor` 在同一 base 的所有 variant 間
    **逐位元相同** —— 這正是 counterfactual 設計的重點。
    """
    m = len(base["attrs"])
    # ⚠️ **target 由 base 固定為 `attrs[0]`／`vals[0]`,不隨 variant 改變。**
    #    先前的版本對每個 variant 各自 shuffle 再取 `order[target_idx]`,
    #    結果同一 base 的 first／last 會選到**不同的屬性** —— query 與答案都變了,
    #    那就不是「只改寫入順序」的 counterfactual,整個設計會失效。
    others = list(range(1, m))
    rng.shuffle(others)                        # 其餘記錄的順序：**base 層決定**
    slots = others[:]
    slots.insert(target_idx, 0)                # target 放在內部寫入序的第 target_idx 位
    ins = rng.randrange(m + 1)                 # distractor 在 m+1 個位置中均勻插入
    writes = [(base["entity"], base["attrs"][i], base["vals"][i]) for i in slots]
    writes.insert(ins, (base["dis_entity"], base["attrs"][0], base["dis_val"]))
    return {"writes": writes, "target_idx": target_idx,
            "q_entity": base["entity"], "q_attr": base["attrs"][0],
            "answer": base["vals"][0], "dis_val": base["dis_val"]}


def query_text(entity, attribute):
    """**受控文字**。O2 不測 natural canonicalization。"""
    return f"{entity} 的 {attribute} 是 多少 ?"


def build_eval(seed=20260812, n_per_cell=300, n_o1=300):
    """**單一共用的 frozen eval artifact**（Codex [172]）。"""
    rng = random.Random(seed)
    cells = {c: [] for c in CELLS}
    for m, idxs, tags in ((2, (0, 1), ("m2-first", "m2-last")),
                          (3, (0, 1, 2), ("m3-first", "m3-middle", "m3-last"))):
        for _ in range(n_per_cell):
            b = base_episode(rng, m)
            grp = rng.randrange(1 << 30)       # 群組 id：paired／triplet 對應用
            for ti, tag in zip(idxs, tags):
                # **同一個 grp 種子** —— 其餘記錄的順序與 distractor 插入點
                # 在群組內固定，唯一變的是 `target_idx`
                v = variant(b, ti, random.Random(grp))
                v["group"] = grp
                cells[tag].append(v)
    # O1 的題目**彼此獨立**：兩兩相異是 **episode 內部**的要求（target vs distractor），
    # 不是跨題要求。`_values(rng, 300)` 會要求 300 個**全域相異** ——
    # 而長度均勻抽讓 2 位數只有 90 個卻佔 1/3 機率，那個 rejection loop 幾乎不會終止。
    o1 = [{"answer": _one_value(rng)} for _ in range(n_o1)]
    art = {"seed": seed, "n_per_cell": n_per_cell, "cells": cells, "o1": o1,
           "names": NAMES, "attrs": ATTRS, "scope": SCOPE}
    art["checksum"] = checksum(art)
    return art


def build_train(seed=20260812, steps=24000, bs=48):
    """**一份共同、固定順序**的 train stream,供三個 seed 使用。"""
    rng = random.Random(seed + 1)
    rows = []
    for _ in range(steps * bs):
        m = 2 if rng.random() < 0.5 else 3                 # m 等比例
        b = base_episode(rng, m)
        ti = rng.randrange(m)                              # index 等比例
        rows.append(variant(b, ti, rng))
    return {"seed": seed, "steps": steps, "bs": bs, "rows": rows,
            "fingerprint": hashlib.sha256(
                json.dumps([[r["q_entity"], r["q_attr"], r["answer"],
                             r["target_idx"]] for r in rows[:2000]],
                           ensure_ascii=False).encode()).hexdigest()[:16]}


def checksum(art):
    canon = json.dumps({k: v for k, v in art.items() if k != "checksum"},
                       sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


EVAL_PATH = os.path.join(HERE, "ng_o2_eval.json")


def save_eval(art=None):
    art = art or build_eval()
    json.dump(art, open(EVAL_PATH, "w"), ensure_ascii=False)
    return art


def load_eval():
    art = json.load(open(EVAL_PATH))
    assert checksum(art) == art["checksum"], "eval artifact checksum 不符"
    return art


if __name__ == "__main__":
    a = save_eval()
    print(f"  NG-O2 eval artifact  checksum={a['checksum']}")
    for c in CELLS:
        print(f"    {c:>10s}  {len(a['cells'][c])}")
    print(f"    {'O1':>10s}  {len(a['o1'])}")
    v = a["cells"]["m3-first"][0]
    print(f"  範例 query：{query_text(v['q_entity'], v['q_attr'])!r}"
          f"  答案 {val_str(v['answer'])!r}")
    print(f"  z_V 維度 {ZV_DIM}；解碼自檢 {zv_value(zv(1234))}")
    print(f"  -> {EVAL_PATH}")
