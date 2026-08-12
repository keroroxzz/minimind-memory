# `MF-0` memory formation —— 設計 **v3**（未實作、未訓練；`W1` 已封存，待 `W1′`）

規格來源：Codex [173]／[174]／[176]。**這是第一個直接回答原始目標的實驗**：

> **不標註 WRITE、模型自己學何時記。**
> `K0`／`zdelta`／`NG-O2` 那條線**沒有**回答這件事 —— 它們全部是
> 「已經知道要記什麼、也已經知道要取哪一條」之後的 plumbing。

---

## 0. ⚠️ 目前的 `W1` 已封存為 `W1-goal-only`（Codex [176]）

**preflight 抓到一個規格缺口,而且是我實作與自己設計文件不符。**

設計寫的是「query 由 goal／topic **與 event 內容關係**決定」;
`mf0_preflight.py` 實作成「stream 結束後,從**所有 goal 類別**的 final key **均勻抽**」。

> **後果**：goal 類別**之內**,event 內容不再決定 future use。
> 於是在 future query 抽出之前,**goal 類內的 record 對 policy 是可交換的**。
> `oracle` 的 82–83% 吃的是**已實現的 query count** —— 事後諸葛,
> **不是可因果預測的訊號**。

**所以 Δ=68pp 不是 learnable headroom,是 hindsight variance。我量錯了東西。**
`goal-only` 的 43–47% 才是**因果可得的上限**。

**處置**（照 Codex [176]）：

- 這次的數字**保留為 `W1-goal-only` 的 diagnostic**,
  **不得刪改、不得重算去挑好看的值**。
- 現行 `W1` **封存**;`H = 0` 由構造成立,故它是
  **`causally non-discriminating beyond goal`** —— **不訓練 learned gate**。
- 要開新的 `W1′`,必須**在訓練前**把「event 的**何種可見關係**能預測未來 query」
  與對應的 `π_content*` **一次寫死**,phase-0 檢查它**不依賴 hidden query label**,
  然後重做 causal-headroom audit。
  **不可看到 learned score 之後再改關係或 `H` 門檻。**

---

## 1. 這一關問什麼

一串事件流過來，**容量有限**。模型必須自己決定**留下哪些**。

- **沒有 `WRITE`／`SEARCH`／importance 標籤。**
- **事件文字在 admission 之後丟棄** —— 不能靠留著原文偷看。
- **唯一的學習訊號**：**未來 query 的 task reward ＋ hard budget。**

也就是：**「該記什麼」必須從「後來被問到什麼」反推出來。**

---

## 2. 兩個 world —— **不選一個**（Codex [174]）

我原本想在「query 均勻抽」與「query 偏向最近」之間**選一個**。
**那是錯的**：二選一都會把**一種產品環境假設偽裝成 gate 的結論**。
（而且我發現自己想選的那個，剛好讓 `learned gate` 好看。）

兩個 world **同等預先登記**，相同 `N`／`B`／模型／訓練預算與**同一張比較表**：

| world | query 從何而來 | 角色 |
|---|---|---|
| **`W0-exchangeable`** | 對已寫入 record **均勻抽樣** | **負對照** |
| **`W1-goal-predictable`** | 由 **event 到達時已可見**的 session goal／topic 與 event 內容關係決定；事件順序隨機化 | 真正的問題 |

- **`W0` 是負對照**：任何 online non-oracle policy 對 `random` 的**穩定優勢，
  一律先視為 leakage**，不是能力。
- **`W1`** 才問：task loss 能不能從**可見情境**學到 retention。
- **兩個 world 不得 pooled**；**不得**用 `W1` 的成功宣稱一般性的自然湧現。

⚠️ `W1` 的 goal 是**可見的 future-use predictor**，
**不是** `important` token、**不是**隱藏標籤。
`W0` 保留**同樣的 surface goal**，但讓它與 query target **獨立** ——
兩個 world 的表面長得一樣，只有統計相依性不同。

---

## 3. 規格（全部鎖死）

| # | 項目 | 值 |
|---|---|---|
| 1 | 每 session 事件數 `N` | **64** |
| 1 | committed record budget `B` | **8** |
| 1 | budget 型別 | 所有 record 同型 → **record budget 即 byte budget**；bytes 另報但**不加第二個約束** |
| 2 | active raw-event window `W` | **4** |
| 2 | goal context | 每 session **固定且全程可見** |
| 2 | 事件原文 | **admission 之後丟棄** |
| 2 | 每 session query 數 `Q` | **16**，在 stream 之後問；**query-delay 覆蓋整個 64-event stream** |
| 4 | gate 形式 | **因果的 per-event scalar priority** |
| 4 | 訓練期 policy | 固定溫度的 **stochastic weighted-reservoir／without-replacement**，維持**恰 `B` 個 slot**，**只可 evict 過去的 item** |
| 4 | 訓練訊號 | future-query return 的 **policy gradient ＋ 固定 leave-one-out baseline** |
| 4 | 評估期 policy | **同一 priority 的 deterministic online top-B** |
| 5 | oracle | query 是 **atomic exact-key read**，故**不是 NP-hard**：事後按每 record 的 `Q` 次 **future-use count** 選 top-B |

⚠️ **第 4 項的最後一句是防作弊的關鍵**：訓練時**不得** soft/dense 地偷看全部 64 個 event。
線上、因果、恰 `B` 個 slot —— 否則測到的是「事後選最優子集」，那是 oracle 的工作。

⚠️ **oracle 只是一個不可部署的上界**，必須明列。

---

## 4. 角色分離（硬約束）

| 元件 | 可以做 | **不可以**做 |
|---|---|---|
| 學習式 gate | 決定「這個 event 值不值得**形成／保留**」——**admission／priority policy** | **不是**存在性證明；**不得**用於 query-time 的 accept／abstain |
| typed Store | 授權答案；`commit` 是原子的 | —— |

**沒有 committed record 時，guard 的語意是「沒有可交付的證據」，零交付／abstain ——
不是「該事實在世界中為假」。**

---

## 5. 對照組與指標

同 budget：`random`／`recency`／**`goal-only causal reference`**／
**`frozen predictive-surprisal`**／`learned gate`／`oracle future-use`（上界）。

**`goal-only causal reference`**（原 `goal*`，Codex [176] 正式納入對照）：
只可讀**同一個可見 goal 與 event canonical class**；
**不得**讀 future query／count／value；同分時以**事前固定的 canonical-key hash** 打破，
**不得**以 recency 偷帶第二個 policy。
**在 `W0` 中它必須與 `random` 同等**，否則先查 leakage。

**`frozen predictive-surprisal`**（原 `frozen surprise`）——
⚠️ **不可再叫 Titans-style**：真正的 Titans 是**有 online associative-memory update**
的另一個 algorithm，**不能由 frozen NLL 代稱**。

    s_t = mean_j [ -log P0( x_{t,j} | goal, 最近 W=4 個 raw event, x_{t,<j} ) ]

平均於該 event 的**全部非 padding token**；同一 online reservoir、同一 deterministic tie rule。
`P0` 是**獨立**模型：以 checksum 固定、與 MF-0 train/eval **不重疊**的
**事件流-only** corpus 預訓練（含同一 goal／event surface，
但**沒有** query／future-use／admission／reward／Store label）。
`P0` 於 MF-0 前**凍結**、不共享 learned-gate 參數、**不在 session 內更新**。
其架構／tokenizer／corpus seed／訓練步數／checkpoint selection 必須在 prereg **一次鎖死**，
**不得**按 `W1` 成績選模型。
三 seed 不勝 `recency` 時，**只封存此 baseline 於此 world，不評論 Titans**。

每個 world、每個 seed，用**同一份 frozen 300-session eval**，報：

- mean **future-query utility**
- **oracle-gap**
- **dropped-record correct abstain**（被丟掉的 record 被問到時，是否正確拒絕）
- 真正 absent-key 的 **0 delivery**

---

## 6. 事前裁決 —— 開跑前鎖死

**`W0`（負對照）**：預測 `random`／`recency`／`surprise`／`learned` 的**期望相等**（oracle 除外）。
> **任何顯著優勢 → 先查漏洩，不得先當成能力。**

**`W1′`（主閘，Codex [176] 改版）** —— **不採 oracle-gap**：

    U(learned) − U(goal-only) >= max(5pp, 0.5 H)

其中 `H = U(π_content*) − U(goal-only)`，且同一 paired bootstrap 95% CI 下限 `> 0`；
另對 `frozen predictive-surprisal` 亦須 paired CI 下限 `> 0`。

> **這測的是「學到至少一半可因果使用的、超過 visible-goal 的訊號」**，
> **不是**追逐一個不可能達成的 hindsight oracle。
> `oracle` 繼續**只報**不可部署上界／剩餘 query-sampling variance。

**先決條件**：`H` 必須先有可量空間。三 seed 的 `H` 若 95% CI **上限都 ≤5pp**，
標為 **`causally non-discriminating beyond goal`**，**不訓練 learned gate**；
**這只否定這個 world 的鑑別力**，不否定 formation 研究。

主閘任一條件未達 → 記 **`MF-0 learned formation FAIL`**；
**不得**用漂亮的 `W0` 或 `oracle-gap` 補過。

**`frozen predictive-surprisal`**：若在 `W1′` 的三個 seed 都未勝過 `recency`
（同一 paired CI 規則），**只封存此 baseline 於此 world** ——
**不得**泛稱「Titans 被否證」（它是 frozen NLL，不是 Titans 的 online update）。

---

## 7. 即使 `W1` PASS，它的代價（必須主動寫，不得事後才承認）

> 最多只能稱：**「在一個 frozen、受控、goal-conditioned 的環境裡，
> future task reward ＋ hard budget 足以學出 admission policy。」**

它**仍然不是**無先驗的自然記憶：

- **goal context 是我們給的**，不是自然現象 —— **不得**把它偷當成自然湧現的證據。
- read/write **key agreement** 未解。
- **semantic canonicalization**（改述／別名／指涉）未解。
- 這些**不得**用 retention 的成績補過。

---

## 8. 為什麼不照抄 Titans，以及一個必須量的風險

Titans 用 **surprise（內部梯度誤差）** 配 momentum 與 adaptive decay 做保留／遺忘，
處理**形成與壓縮**確實比手工 schema 優雅。

但它更新的是 neural associative memory 的**參數／狀態**，
**不天然給每筆事實一個可版本化的物件** —— 沒有存在性、版本、刪除，無法證明缺席。

⚠️ 且 **surprise 高 ≠ 日後會被問到**。
「我的門號密碼是 47」在一段平淡對話中並不 surprising，卻正是日後要用的。
**`MF-0` 的 `frozen predictive-surprisal` 對照組就是為了量這件事。**

---

## 9. 附：為什麼**不**開「拿大模型裁決自然湧現」（Codex [174]）

使用者問：記憶操作會不會在巨量資料上自然湧現？
我提議拿現成大模型 ＋ `contains` tool 來量。**Codex 駁回，理由成立**：

> 黑箱大模型配 `contains` tool，**至多測它是否服從一個已給的 tool protocol**。
> **成功不證明**訓練時自然學出 memory operation；
> **失敗也不證明** scale 不會。
> runtime store 的 membership 是**外部狀態**，**任何規模的權重都不能成為它**。

若日後要做**產品 baseline**，另立 `PB-0`：釘死 provider／model snapshot、temperature、
單一 prompt／tool schema 與成本上限；
`literal-key present utility ≥95%`、300 個 absent **0** 個數值答案、`contains` call 300/300。
**它只能稱 tool-use compliance comparison，絕不可用來裁決 emergence 或覆寫 `MF-0`。**
