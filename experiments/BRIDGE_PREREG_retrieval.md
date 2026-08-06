# 預先登記 — A 路：exact-key retrieval ＋ typed membership guard（R2b 最小版）

**寫於 2026-08-06，在任何 A 路結果之前。** 規格依 Codex [144]。
使用 **§4.55 的凍結 core**（`bridge_core_latent.pth`，訓練 `n∈2..4`），**不重訓**。

## 0. A 是什麼、不是什麼

**是**：把 exact-key canonicalization、`store.contains`、missing hard-abstain、
wrong-key fault、latent-only end-to-end 與 `consumption | retrieval-correct`
在新 bridge core 上封成 **transfer / safety baseline**。

**不是**：semantic retrieval、memory formation。**研究新意有限**（Codex [144] 原話），
**不得**把 A 的結果稱為語意檢索。
**A 若失敗，B 不得歸因於描述定址。**

## 1. 設計（Codex [144] 收窄後）

- **active injection 固定 `n=2`**（primary）。`n=4` 可另報，**不得混作 primary**。
  **`n=1` 仍不碰**（對該 core 是 OOD）。
- **只掃 `pool ∈ {8, 24, 48}`**。
  **不得**在掃 pool 的同時改 `n` —— 否則又把 R2a active capacity 混進 R2b。
- 每個 pool **固定一批 target keys**，並加入不相關 entries。
- **固定 hit/miss 比例 50/50**、固定 query 分布。

## 2. 權威路徑（typed guard，不是學出來的相似度）

```
canonicalize(name, attr) → store.contains(key)
  absent  → **在注入之前**硬 abstain（fail closed）
  present → read → 斷言 addr 與 key 相符 → 注入 → core 作答
```

**`contains` 是可判定的資料結構事實**，§4.37–4.38 已證明
把它交給學出來的相似度是 category error（halluc 8/300 → **0/300**）。

**學出來的相似度只作 shadow metric，永遠不得覆寫 guard。**

## 3. 必報指標

`retrieval exact`、`guard abstain`、`false_abstain`、`wrong-key halluc`、
`E2E | retrieval-correct`、**lookup latency**、**store bytes**、
以及 **shadow**（沒有 guard 時會怎樣）。

## 4. 事前預測與判讀

> **exact `contains` 的正確率應隨 pool **平坦**。**
> **若不平坦，那是 store／index 的實作問題，不是容量曲線。**（Codex [144]）

- `halluc` 應為 **0**（結構性：absent 在注入前就 abstain）。
- `false_abstain` 應為 **0**（present 一定讀得到）。
- 兩者若非 0 → **plumbing bug**，先修再談。

## 5. 不得宣稱

- `pool ∈ {8,24,48}` 只是**這個 48-descriptor 世界的最小 store plumbing**，
  **不宣稱長期容量**。更大 pool 必須先換**固定 tokenization 的 synthetic descriptors**
  並另立 core／retriever prereg。
- 不得稱 semantic retrieval；不得與 R2a 的 active capacity 混報。

---

# 修訂 — A 的 **transfer-boundary extension**：同實體多屬性（Codex [145]）

**不是推翻 A 的 exact-key guard PASS**（那部分成立且不變），是補 coverage。

## 發現

`make_episode` 用 `rng.sample(NAMES, n_fact)` → **同一題內名字必定互異**。
所以 §4.55 的 core **從訓練到所有評估，從沒見過「同一實體的兩條記憶」**。
§4.55 的結論**隱含只適用於「每個實體最多一條」**。

實測（**OOD，不作能力判準**）：不同名 distractor **99.3%**、同名 **29.3%**。

**機制已定位（n=400）**：失敗不是挑錯 ——
`答成 distractor` 只有 **2.2%**，`第三種` 佔 **72.5%**，
而第三種**是兩值的平均**：`|pred − 兩者平均|` 中位數 **2.5**、
**90.5% 落在兩值之間**（隨機約 33%）。
→ **name-only 匹配 → query 對兩條同名 carrier 匹配度相同 → attention 軟性混合 → value 平均。**

## 修法（只改一個變因）

`make_episode(..., same_name=)`：`False` = 歷史行為（**預設，eid 逐字不變，已驗**）、
`True` = 保證同名、`None` = 自然抽樣。
**`(name, attr)` 一律唯一；同一 exact key 兩個值屬 conflict 軸，不在此。**

**訓練用 `same_name_p=0.5`**：自然抽樣在 `n=2` 只有 **4.6%**（n=3/4 為 12.7%/24.8%），
而 `n=2` 正是主 gate 格 —— 太稀疏教不動。
**這是刻意的分布選擇，必須明寫**，代價是同名題的名字邊際改變。

## 評估（Codex [145]）

- **duplicate-rate factorial：0 vs ≥1 same-name distractor**，
  **分開報**，不得只混進整體平均；隨機化 slot 與 target。
- 不同名／同名各報 `n=2/4`、`j=0/1`；保留 `value_shuffle`／address permutation。
- **core/render gate 先過，才讀 same-name 能力。**
- **錯誤按 entry provenance 分類**（不只按數值）：
  `target-entry correct` / `same-name wrong-entry` / `other-candidate wrong-entry` /
  `third-invalid` / `abstain`；兩條同值時**數值答對不得冒充 binding**，
  報排除碰撞的 `exact_nc`。

## 事前期待（Codex 壓低，明寫）

**不預期同名一定接近 99%。** 重點是**差距在新訓練分布中可量化、並與 address exactness 分開**。
若差距仍大，只能說**舊 core 的 name-only 捷徑被修正**，
**不得**宣稱 address schema 本身失效。
