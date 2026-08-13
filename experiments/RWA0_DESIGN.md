# `RWA-0` read-write agreement —— **design-only 草案**（未實作、未訓練）

規格來源：Codex [175]。**在 `CI-1` seal 之前不得實作／訓練**，
且**不得**以 `CI-1` 的結果重寫本檔的 task 或 gates（Codex [185]）。

---

## 1. 為什麼是這一關

`research.md` §4.69 盤點裡的兩個未解 blocker，一個是 §4.67 的 selector，
另一個就是**讀寫一致**：

> 寫入時說「我的門號密碼」與讀取時問「門號密碼多少」**必須映到同一個 key**。
> 不一致 → 寫進去的**永遠讀不出來**，而 guard 會誠實 abstain：**安全但無用**。

**它吸引人的地方是不需要 confidence 就能量** —— 你**同時擁有兩側**，
`k_write == k_read` 是**可判定的事實**。正好繞開 §4.67 倒下的地方。

---

## 2. ⚠️ `k_write == k_read` 單獨**完全不夠**（Codex [175]）

> 若所有東西都映到**同一個 key**，`k_write == k_read` 會 100% 成立 ——
> 而那是一個**把所有記憶混成一條**的系統。

因此主閘**必須**同時包含 false-merge 的負例。

---

## 3. 定位：**controlled compositional agreement probe**

- 同一個**隱藏 referent** 由**獨立的 write／read renderer** 產生。
- **template family 與 alias family 均不重疊**。
- 以 **held-out surface family** 評估。

**PASS 仍不得稱**「自然語言／指涉已解」。

---

## 4. 三段式主閘（缺一不可）

| | 條件 |
|---|---|
| **(a) 同指涉** | 同一 referent 的 write／read pair：`k_w = k_r = k*` |
| **(b) 不得 false-merge** | **同 entity 異 attr**、**同 attr 異 entity**、**近似字面 hard-negative** 三個 stratum，各自 key **不相等**；`false merge = 0/300` **per stratum** |
| **(c) end-to-end** | 以**寫入的 key** 再讀取，**無 wrong-existing delivery** |

**(b) 是防「全部映到同一 key」的那道閘**；沒有它，(a) 可以被退化解騙過。

---

## 5. 訓練與命名的硬約束

> 若訓練使用 **same/different referent pair 標籤**，
> **必須**叫 **`supervised semantic canonicalization`**，
> **不得**叫無標籤的自然湧現。
>
> 若**只作 eval**（用既有 encoder 探測），則叫 **既有 encoder probe**。

兩者**不得混稱**。這一項在起草時就要決定並寫死，不得跑完再選稱呼。

---

## 6. 待鎖（下一輪要填死，現在**刻意留白**）

1. **surface family 的定義**：write／read 的 template 與 alias 如何切、
   held-out family 怎麼選？
2. **hard-negative 的構造**：「近似字面」的精確定義（編輯距離？共享 token？）——
   這個定義**實質決定 (b) 的難度**，且我處在會挑好看的位置。
3. **是否訓練**（決定第 5 節的稱呼）。
4. **n 與 stratum 比例**、以及 (b) 的 `0/300` 是否 per stratum per replica。
5. **encoder 規格**（若訓練）：架構／budget／seeds，須訓練前一次鎖死。

**第 2 項是本設計最容易自欺的地方** —— 把 hard-negative 定得太寬，(b) 就變成送分題；
定得太窄，(b) 變成不可能。**這個選擇必須事前公開並給理由。**

---

## 7. 與已封存結果的關係

- **不得**用 `RWA-0` 的成績補 §4.67（selector）或 `MF0-C`（formation）的 FAIL。
- **不得**宣稱它解決了開放集或自然語言。
- `Store` 的 membership／conflict 契約（§4.57／§4.64／§4.68）**可繼承方法，不繼承驗證**
  —— 若換 key 型別，須重驗。
