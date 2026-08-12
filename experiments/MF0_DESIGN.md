# `MF-0` memory formation —— 設計草案，**未實作、未訓練**

規格來源：Codex [173]。**這是第一個直接回答原始目標的實驗**：

> **不標註 WRITE、模型自己學何時記。**
> `K0`／`zdelta`／`NG-O2` 那條線**沒有**回答這件事 —— 它們全部是
> 「已經知道要記什麼、也已經知道要取哪一條」之後的 plumbing。

---

## 1. 這一關問什麼

一串事件流過來，**容量有限**。模型必須自己決定**留下哪些**。

- **沒有 `WRITE` 標籤** —— 不告訴它哪一句該記。
- **沒有 `SEARCH` 標籤** —— 不告訴它何時該查。
- **沒有 importance 標註** —— 不給任何人工的重要性訊號。
- **事件文字在 formation 之後丟棄** —— 不能靠留著原文偷看。
- **唯一的學習訊號**：**未來 query 的 task loss ＋ budget 成本。**

也就是：**「該記什麼」必須從「後來被問到什麼」反推出來。**

---

## 2. 為什麼不是照抄 Titans

Titans 用 **surprise（內部梯度誤差）** 當保留訊號，配 momentum 與 adaptive decay。
它處理**形成與壓縮**確實比我們手工指定的 schema 優雅。

但它更新的是 neural associative memory 的**參數／狀態**，
**不天然給每筆事實一個可版本化的物件** ——
沒有存在性、沒有版本、沒有刪除、無法證明缺席。

⚠️ 而且有一個必須量、不能用美感補的風險：
**surprise 高 ≠ 日後會被問到。** 一個低-surprise 但日後關鍵的事件
（「我的門號密碼是 47」在一段平淡對話中並不 surprising）會被漏掉。
**`MF-0` 正是要量這個。**

---

## 3. 角色分離（Codex [173] 的硬約束）

| 元件 | 可以做 | **不可以**做 |
|---|---|---|
| 學習式 gate（surprise／task-learned） | 決定「這個 event 值不值得**形成／保留**」——**admission／priority policy** | **不是**存在性證明；**不得**用於 query-time 的 accept／abstain |
| typed Store | 授權答案；`commit(full key, version/provenance, z)` 是原子的 | —— |

**沒有 committed record 時，guard 的語意是「沒有可交付的證據」，零交付／abstain ——
不是「該事實在世界中為假」。**

神經記憶可以**提案、壓縮、排序**；**只有 typed Store 有權授權答案。**
這不是把兩個同類模組硬黏在一起，而是把 **lossy optimization** 與
**可稽核的 epistemic commit** 放在各自做得到的層。
**目前這仍是設計命題，沒有實測成功。**

---

## 4. 對照組（同 budget，缺一不可）

| policy | 內容 | 角色 |
|---|---|---|
| `random` | 隨機留 | 地板 |
| **`recency`** | 留最近的 | **真正要打敗的對手** |
| `frozen surprise` | Titans-style，不隨任務訓練 | 被檢定的假說 |
| `learned gate` | 由未來 query 的 task loss ＋ budget 成本學 | 主角 |
| `oracle future-use` | 事後才知道哪些會被問到 | **上界** |

**主指標**：future-query utility，以及與 oracle 的差距（**oracle-gap**）。
**安全指標**：absent key 的**零交付**（沿用既有 guard 語意）。

---

## 5. 事前裁決 —— 開跑前鎖死

> **若 `surprise` 在相同 budget 下不能穩定贏過 `recency`，
> 就封存 Titans-style surprise 作為本任務的 write-policy 候選**，
> 不再把它升級成主線。

> 若 `learned gate` 贏了，才值得開「task-loss ＋ 容量瓶頸學 formation」的下一關。

**即使 `MF-0` PASS**：read/write key agreement、唯一化、自然語言**各自仍未解**，
**不得**用 retention 的成績補過。

---

## 6. 待鎖的規格（下一輪要填死，現在刻意留白）

1. **budget 的定義**：committed record 數上限？bytes？兩者都要？
2. **active window 長度**與事件流的統計（query 距離寫入多遠？重複率？）
3. **query 分布**：均勻抽已寫入的 record，還是有偏態（近的常被問）？
   —— 這一項**直接決定 `recency` 有多強**，必須事前定且說明理由。
4. **learned gate 的參數化**：per-event 標量分數 ＋ top-k 保留？還是逐步 admission？
5. **oracle 上界的可行性**：事後選最優子集是 NP-hard 的一般情形，
   本任務規模下是否可窮舉？不可窮舉時的替代上界是什麼？

**第 3 項是這個設計最容易自欺的地方** —— 把 query 分布設成偏向最近，
`recency` 會很強而 `learned gate` 看起來沒用；設成均勻，`recency` 會很弱
而 `learned gate` 看起來很強。**這個選擇必須事前公開並給理由，不得看到結果再說。**
