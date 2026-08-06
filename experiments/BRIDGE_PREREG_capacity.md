# 預先登記 — **R2a 同時注入容量**：address 選擇撐得到幾條記憶？

> **R2 已拆成兩個子題（Codex [140]），不得合併成一個 R2 PASS：**
> - **R2a `simultaneous active capacity`** —— 本文件。oracle candidate set，
>   測 **active workspace + address-conditioned selection**。
> - **R2b `persistent store capacity`** —— 另立章節。測 store 總量、writer、pool lookup、
>   collision/eviction、query coverage；**不得用 R2a 的 oracle 集合假裝 store 容量**。

**寫於 2026-08-06，在容量 core 訓練之前。** §4.55 已成立 `n∈2..4` 的 address 驅動選擇；
本文件測它隨 `n` 的行為。R2（記憶容量）在七條可靠度裡目前是 ⬜ 未測。

---

## 0. 為什麼這是下一步

§4.55 的 core **只在 `n_fact ∈ 2..4` 訓練過**，所以 95.2% 是**最多 4 條**的成績。
`n=1` 那格已示範訓練分布外會出現 OOD，不得外推。

**這決定它是「記憶系統」還是「4 格暫存器」。**

---

## 1. 事前算好的容量預測（純數學，不需要模型）

`φ` 表：48 個 descriptor，**16 維**單位向量。兩兩 `|cos|` 中位數 0.182、P95 0.475、最大 0.739。

**每題注入 `n` 條時，該題內 address 兩兩最大 `|cos|`（1000 次抽樣）：**

| n | 中位數 | P95 |
|---|---|---|
| 2 | 0.180 | 0.498 |
| 4 | 0.402 | 0.608 |
| **8** | **0.514** | 0.725 |
| **16** | **0.661** | 0.739 |

**預測（事前寫下）**：若崩潰來自 **address 碼容量**，則
**逐題的錯誤應與「該題的最大 `|cos|`」正相關**，且 `n=8→16` 之間應出現明顯退化。
若正確率掉了但**與 `max|cos|` 無關**，則瓶頸不是碼容量，另尋原因。

⚠️ **這是機制分析，不是容量 gate**（Codex [140]）。`max|cos|` 只在 `n≥2`、
同一批 address 組合內做分箱／回歸，並**同報 `value_shuffle` 的 `orig/cf`**。
gate 仍然只有一條：**`latent` 在 `n=2` <95% → core/render invalid，不讀後續 n**。

---

## 2. 設計

- 重訓 core，**`n_fact ∈ 2..16`**（16 是上限：`NAMES` 只有 16 個，
  `make_episode` 用 `rng.sample(NAMES, n_fact)`，名字在同一題內必須互異）。
- 其餘**與 §4.55 完全相同**：同 schema `z'`、同 `φ`、同固定未訓練投影、
  carrier 順序每題隨機、`L0` 與 `latent` 各半、24000 步。
  **只有 `n_fact` 範圍這一個變因改變。**
- 評估 **n ∈ {2, 4, 8, 16}**，`j ∈ {0, 1}`（j=2 在 §4.55 已 censor，不評）。

---

## 3. gate（沿用 §4.55，先鎖再看）

- **`latent` 在 `n=2` 必須 ≥95%**，否則整個 core 記 `invalid`，不解讀容量。
- `floor`（carrier 歸零）必須留在地板（<5%），否則是背答案。

---

## 4. primary 與判讀

**primary = 正確率隨 `n` 的曲線**（`exact`，j=0 與 j=1 分開報）。

| 裁決 | 條件 |
|---|---|
| **容量成立到 n** | 該 `n` 的 95% CI 下界 **≥90%** |
| **退化** | 低於 90% 但顯著高於 `1/n` |
| **崩潰** | CI 含 `1/n`（回到亂挑） |

**必跑 `value_shuffle`（每個 `n` 都要）** —— §4.55 的決定性對照。
只有 `cf` 仍顯著高於 `orig` 時，「address 驅動選擇」才在該 `n` 成立；
若 `cf` 與 `orig` 都塌，是**選擇失效**而不是**取值失效**，兩者不得混報。

**機制診斷（事前登記）**：逐題記錄該題的 `max|cos|`，
報 **正確率 vs `max|cos|` 分箱**。這是 §1 那個預測的直接檢定。

---

## 5. no-address 臂的判讀改為三分（Codex [139] 的要求）

§4.55 的 `addr_zero` 判準按字面 FAIL，因為它預設了一個不存在的 fallback。
本輪起，no-address 臂**一律報三分佈**：

- `1/n fallback`（答了某條被注入記憶的值）
- `abstain / floor`（答不出來、parse 失敗）
- `wrong-answer`（答了不屬於任何記憶的值）

**先報完整分布，再裁 selection。不得為了讓判準過關而加 loss 或調 threshold。**

---

## 6. 不得宣稱

- `n=16` **仍不是** R2 的「1K → 1M」；這是**同時注入**的條數，不是 store 的總量。
  **不得**把本輪結果寫成 R2 通過 —— 本輪最多只是 **R2a**。
- **措辭上限（Codex [140] 鎖死）**：R2a 若過，只能說
  **「在 16 個已見名字集合、latent-only、j=0/1 下，同時注入容量達到 16」**，
  **不得**宣稱可無限增長或一般長期 pool capacity。
- **`n>16` 需另開 prereg**：擴名字會同時改動 tokenization、query 難度與 address 生成分布，
  **那是另一個變因**；要超過 16 得先換成**固定 tokenization 的 synthetic identity/address**
  並另做 core ceiling，**不得直接加 `NAMES`**。
- retriever 仍是 oracle（給定集合）。
- 精確 address、封閉 48 個 descriptor、單一 seed（技術債沿用）。
- carrier 仍佔序列位置；**成本隨 `n` 線性增加**，本輪應**順帶量測** tokens/s 與 VRAM
  （R7 要求成本三軸並陳）。

---

# 修訂 v2（2026-08-06，首輪 gate FAIL 之後、重訓之前）

**首輪（`bridge_core_cap.pth`）已判 `core/render invalid`（`research.md` §4.56），
FAIL 不回填、不改名。本節是修正後的新 prereg，產出新 artifact。**

## v2-1 根因與修法（Codex [141] 選 (b)）

首輪的 prereg 寫「只改 `n_fact` 一個變因」——**那句話是錯的**。
沿用「`L0` 與 `latent` 各半」時，擴 `n_fact` **同時**讓 `L0` 分支變質成
近乎不可解的任務（文字裡塞 16 條事實再找出被問那條），半數預算被它吃掉。

**修法（b）：`L0` 只在 `n≤4` 出現（format anchor），`latent` 涵蓋 2..16。**

Codex 否掉的兩個替代方案，理由一併記下：
- **(a) 降低 L0 比例到 10%** —— 仍會以**高 loss 噪音污染梯度**。
- **(c) curriculum** —— **同時改變時間與資料分布，成功後更難歸因**。

## v2-2 必須明寫的訓練支持差異

`L0` 與 `latent` 的 `n` 分布**刻意不同**。因此：

- **`L0` 只作 format anchor，不得用來當大 `n` 的 ceiling。**
- **不得**拿 `L0` 與 `latent` 在 `n=16` 做「公平」的 accuracy 比較。
- 每個 mode 的**樣本數、loss weight、`n` 分布**固定並在結果中報明。

## v2-3 gate（重訓**之前**鎖死，不得放寬）

- **`latent`、`n=2`、`j=0` ≥95%**（以 CI 下界為準）。
  未過 → **整個 run `core/render invalid`，不讀任何 capacity**。
- `L0` **只檢查小 `n` 的格式錨點**，
  **不得**把「原本大 `n` 就不可能的 L0」變成 gate。
- `j=2` censor、`n=1` OOD 規則**不變**。
- **不得因為「R2 比較難」而放寬 95%。**
  若新設計仍過不了 `n=2`，**R2a 止於 invalid，不得宣稱「容量低」**。

## v2-4 `max|cos|` 分析（首輪的版本已作廢）

首輪把 `n=2/4/8/16` 混在一起分箱 → **無效分析，不得引用**。

- 有效 run 之後，**在每個固定 `n` 內部**分箱／回歸，
  或在模型中控制 `n` 並**預先鎖 interaction**。
- 至少報 **n-specific CI** 與 **`value_shuffle` 的 `orig`/`cf`**。
- **不得在重跑前先看結果挑 bin。**

## v2-5 順序

`n=2` gate 過 → 才讀 `n=4/8/16` → 才讀 cos 關係。

---

# 修訂 v3 — **fixed-load 診斷（`n=8`）**，事前標明為診斷（Codex [142]）

**兩輪 R2a 皆 `invalid`（§4.56），本節不是第三次 R2a，而是一次
事前標成 diagnostic 的單變因裁決 run。**

## v3-1 目的：分辨兩個假說

- **H_A**：混合 `n` 訓練造成 optimization interference
- **H_B**：大 `n` 對本架構本質做不到

**做法**：`latent` 固定 `n=8`（`nf_min=nf_max=8`），`L0` 維持 `n≤4` 的 format anchor，
其餘完全不變、24000 步。

## v3-2 主張範圍（Codex [142] 事前鎖死，**兩個方向都收窄**）

- **過（`n=8` ≥95%）** → 只能說**「在此固定 `n`、此訓練預算下能學會 8 條」**。
  **不代表**單一系統能處理變動 `n`，**不代表** R2a 容量成立，
  **不得**把 `n=8` 的 accuracy 塞回舊的 R2a 曲線。
  形式上證明的是**混合 `n` 訓練干擾**。
- **不過（<95%）** → **不得**叫「真容量上限」。
  只能說**「本架構＋此 optimizer/steps 在 fixed-8 下未達可判性」**，
  容量上限**仍與 optimization 未分離**。

## v3-3 gate

**`n=8`（本 run 唯一訓練過的格）≥95%，不因「只是診斷」而放寬。**
`n=2` **不再是**本診斷的 gate；舊 R2a 的 `n=2` gate FAIL 與完整曲線
**永久保留、不回寫**。其餘 `n` 為 OOD，僅供參考、不作判準。

## v3-4 之後怎麼走（事前寫下，避免看到結果再挑）

- **fixed-8 過** → 下一版 R2a prereg 改成**每個 `n` 等量的梯度／batch**
  （非 uniform episode 抽樣），或**預先鎖 curriculum**；同一 core 再測 n=2/4/8/16。
- **fixed-8 不過** → **容量線暫停**，改查 address render／optimizer／schema。
  **不得再用配方微調把 FAIL 推成容量結果。**

## v3-5 逐 `n` 各訓一個 core = `R2a-0`，**不是 R2a**

若之後要做，只能記為 **`R2a-0` fixed-load feasibility/calibration**，
且**每個 core 只在自己的 `n` 上宣稱**。
它**不回答**「一個 core 支援變動 active `n`」——那才是 R2a 要問的。
**本輪只跑固定 `n=8`，不投入四次訓練。**

---

# 修訂 v4 — **一次性 precision follow-up**（事後提出，Codex [143] 批准並加嚴）

## v4-0 這是什麼、不是什麼

- **是**：對 `bridge_core_fix8.pth` 的一次性 **confirmatory evaluation**，
  只為解決 `n=250` 下的解析度不足（±3.5pp）。
- **不是**：把 `n=250` 的 gate 改判 PASS。
  **原 run 永遠記 `92.4%`，原 prereg 判定永遠是「未達可判性」。**
- **本節在看到 92.4% 之後才提出**，因此明確標示為 **post-hoc precision follow-up**。

## v4-1 執行規則（跑之前鎖死，不得更改）

- 同一個 **frozen checkpoint**（`bridge_core_fix8.pth`，不重訓、不調任何東西）。
- **全新固定的 test episodes**（新 seed，與 `n=250` 那批不重疊）。
- **只看一次，到 `n=1000` 為止**；**無中途停看、不再追加**。
- **無論結果如何都照報。**

## v4-2 判讀（**採用比原 gate 更高的證據標準**，Codex [143] 明令要寫明）

- **`fixed-8 capability PASS`** ⇔ **單側 95% CI 下限 ≥ 95%**（Clopper–Pearson）。
- 點估計 ≥95% 但下限仍低 → 記為 **「改善但 gate 未封」**，**不是 PASS**。
- **這比原本的「點估計 ≥95%」更嚴格。** 明寫：
  **新 follow-up 採更高證據標準，不得與舊結果混報。**

## v4-3 未過的處置（事前寫死）

若本次仍未達門檻：

> **R2a 容量線暫停。** **不得**繼續查 `fit_scale`、步數、loss 配方
> 並把它們當成容量證據。那些可另立
> **「fixed-8 optimization/adapter repair」** 新題，但**它們是新介入**，
> **不得沿 R2a gate 反覆調到過為止**。

最準確的結論措辭（Codex [143] 給定）：

> **「fixed-8 在既定 core／optimizer／delivery protocol 下未取得可判定的 ≥95% 能力；
> 不能分辨真容量與優化不足。」**
> 容量曲線與 `max|cos|` 分析**封存，不報容量上限**。

## v4-4 過了也只證明 fixed-8

**不代表**變動 `n` 的 R2a。日後重開須先寫**新的 optimization protocol**
（固定 `n` 平衡、明確 scale、預算）**與新的 gate**；
本輪的兩次 invalid、一次 fixed-8 不確定、與本 follow-up 全部保留為 artifact，**不回寫歷史**。
