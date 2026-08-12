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

---

# 修訂 — mixed-name **compositional-generalization** gate（Codex [151]）

**寫於訓練之前。** 上一輪 `same_name_p=0.5` 的 run（`bridge_core_sn.pth`）已 gate FAIL
（easy 75.2%／hard 99.2%），**同 seed 重跑會逐位元相同**，故本輪**不是重跑**。

## 唯一的差異：held-out 組合（效度修補，非救分）

上一輪 **48 個 descriptor 全部都在訓練裡**，
所以無法分辨「**學會用屬性區分**」與「**背下特定同名配對**」。

- 事前固定一份 **split artifact**：保留 **10/48（20.8%）** 的 `(name, attr)` 組合不進訓練。
- **分層保證**：每個 `name` 在訓練中仍有 ≥2 個組合、每個 `attr` 仍有 ≥12 個 name
  → **不會出現未見 token 或未見單屬性**。
- **其餘全部不變**：比例仍 `same_name_p=0.5`、`n` 仍 2..4、同 tokenization、同 query 模板，
  **loss／address／schema／consumer 一律不動**。
- **split 比例與 seed 事前鎖死，不得看結果改 split。**

## 評估：2×2 四格，全部都要報

| | easy（全不同名） | hard（目標有同名兄弟） |
|---|---|---|
| **seen 組合** | | |
| **held-out 組合** | | |

分開報可區分「**連已見組合都做不好**」與「**已見會、組合泛化不會**」。
**held-out 不是為了讓模型更容易** —— 它是為了**防止把配對背誦誤稱為屬性使用**。

## 門檻（**不放寬**，Codex [151]）

**四格的點估計都必須 ≥95%**（held-out 亦同）。
另報 95% Wilson CI，**但不得用 CI 下限替代門檻**。
保留 `target/distractor/third` provenance 與 `value_shuffle` 欄位。

## 處置（事前寫死）

任一格未過 → 記為 **mixed-name／compositional-generalization gate FAIL**。
**不讀另一格的漂亮數字、不開機制假說、不調配方。**
結論停在「**目前 core／訓練預算不支援 mixed-name**」，再另立新 core 規格。

**我的事前預期（寫在跑之前）**：easy 上輪 75.2%，加 held-out 只會更難或持平，
**我預期本輪仍 gate FAIL**。

---

# `EXP-MN1` —— 新 core 實驗（Codex [153] 審核通過並鎖定）

**唯一主張**：修復 mixed-name 能力。**唯一介入**：`24000 → 60000` 步。

**全部固定**：ARCH、schema `z'`、`φ`、carrier 投影、carrier 順序隨機、
`L0`/`latent` 各半、`same_name_p=0.5`、held-out **同一份 split artifact**、
`n_fact` 2..4、loss（不加權）、tokenizer、query 模板、consumer。

## Gate（Codex 鎖定，不新增難度）

在 `n=2, j=0`，**四個 provenance cell（seen/held-out × easy/hard）的點估計各自 ≥95%**，
**四格全過才 PASS**。另報樣本數與 95% Wilson CI，
**不以 CI 下限取代點估計門檻**。`n=3,4` 與 `j≥2` **只作描述／non-goal，不得補過主閘**。

## 成功時的措辭上限

只能宣稱**「同一架構在 60k 步、此固定分布下通過」**，
**不得**宣稱架構一般性已足夠。

## 停止規則（Codex 收緊）

- **完整跑滿一次 60k。**
- 中途 checkpoint **只供事後軌跡診斷**，**不挑最佳點、不續訓改配方**。
- **四格任一未過即停止**，結論寫成
  **「同一架構在 60k 步仍未通過 mixed-name gate」**。
- **不得**再加步數、改 loss／架構／資料，或開機制假說。
- 純基礎設施錯誤：**僅可用同一已鎖規格重跑並標 invalid**。
- 容量或架構改動 → **另立新 experiment**。

---

# `EXP-MN3` —— address-necessity sampling（Codex [156] 回覆鎖定）

**`EXP-MN2`（width-only，`hidden=768`）在開跑前撤銷 —— `not run`，不是 FAIL。**
理由：§4.62 的干預證據顯示失敗不是容量不足，而是有效 routing cue 被換成 attr one-hot。
**容量未被邏輯排除**，只是不作第一個槓桿。

## 唯一介入

**資料 generator 的關係分層**，回到 `hidden=512`。其餘沿用 `EXP-MN1`：
60k 步、同一 split artifact、schema `z'`、`ADDR_DIM=16`、projection 規則、loss、
L0/carrier 各半、`n` 分布、隨機 carrier order、seed、bs、lr、scheduler。
**不得**改 loss、width、`ADDR_DIM` 或 curriculum。

`--relation-strat` 與 `--same-name-p` **互斥**（程式內 assert）。

## 分層定義（相對於被問的那條 fact）

| | 關係 | 為什麼 |
|---|---|---|
| `R_addr` | 異名／**同 attr** | attr 完全無法分辨，**只有 addr 能定位** |
| `R_attr` | 同名／**異 attr** | 名字無法分辨，attr 可以 |

- `n=2`：`R_addr : R_attr = 1:1`，由 **batch 內索引奇偶**決定 → 計數**結構上**等量，
  不靠隨機收斂；仍以 assert 驗證。
- `n=3,4`：每題**至少各有一條** `R_addr` 與 `R_attr` distractor，其餘為異名／異 attr。

**這不是調權重，是改變什麼策略算最優**：舊分布裡 attr 幾乎總能挑出 target，
attr-only shortcut 近乎最優；分層後它不再是。

## Gate（開跑前鎖死）

1. **階層一 —— L0/text sanity**：`n=2, j=0` 既有指標 `≥95%`。不過就停。
2. **階層二 —— primary**：`seen / held-out` × `R_addr / R_attr / R_both` **六格**，
   每格 `A_ans` 點估計各自 `≥95%`，**六格全過才 PASS**。
   另報 n 與 95% Wilson CI，**但不得用 CI 下限替代點估計門檻**。
3. **佐證（不可救 FAIL）**：`R_addr` 的凍結 `swap_addr` fidelity check，
   n=200，distractor-output `≥90%`。它只證明 routing 確實走 addr，
   **不能**用來抵銷任何 accuracy 未過的格子。

## 停止規則

完整跑一次 60k。中途 checkpoint 只作軌跡診斷，**不挑點、不續訓**。
**任一 primary 格 FAIL → 封存這條 fixed-schema mixed-name 線**，
不自動接 width／address／loss 實驗，轉回 retrieval 軸，
且屆時所有結論必須帶「**不涵蓋同實體多屬性**」的範圍限制。

## 指令

```bash
python bridge_train_core_latent.py --steps 60000 --heldout --relation-strat \
       --ckpt-every 8000 --out bridge_core_mn3.pth
python bridge_mn3_eval.py bridge_core_mn3.pth 250
```
