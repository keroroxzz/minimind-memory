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

## 5. 訓練與命名的硬約束（**已鎖定，條件式文字已刪**）

**訓練形式已鎖為 direct CE**（Codex [186]／[187]）：
write／read 各自輸出離散 typed product key `K=(entity_code, attribute_code)`，
以 oracle `K*` 的**交叉熵**訓練。

> 名稱鎖為 **`closed-world supervised controlled canonicalization`**。
> **不得**叫自然語言／無標籤／emergent semantic canonicalization。

⚠️ 原本「**若**訓練使用 same/different referent pair 標籤則……」那段條件式文字
**已刪除** —— direct CE 已鎖，保留條件句會讓稱呼看起來還有選擇餘地。

---

## 5.1 ⚠️ 同步界線：held-out 的是**組合**，不是**字典**（Codex [187]）

`W`／`R` 的 **alias literal class 可以互不重疊**，
但 **eval 不得含未見的 atom／alias** —— **每個 eval atom 都必須有 train witness**。

> **真正 held-out 的是：renderer composition／template、完整 surface string、
> 以及 `(entity, attribute)` pair。**
> **不是**字典 OOD。

若弄反，FAIL 只會證明「模型沒看過這個詞」，**完全不能識別 read-write agreement**。

---

## 6. 待鎖 —— **已全部鎖定**（見 `RWA0_prereg.json` v2）

六項（key schema／renderer 與 alias／hard-negative 構造／data-split／
phase-0 追加項／encoder 與 compute）已由 Codex [187] **一次鎖定**，
逐字寫入 `RWA0_prereg.json` v2；舊版保留為 `RWA0_prereg_rev1.json`。

**我原本擔心的「hard-negative 定得太寬或太窄」已不存在** ——
它現在是**完全由構造決定**的：`(a,b,c,e)`／`(a,e,c,d)`／`(a,c,b,d)`，
**禁止**用 edit distance、cosine 或模型表現挑選。
「hard」是**造出來的，不是挑出來的**。

---

## 7. 與已封存結果的關係

- **不得**用 `RWA-0` 的成績補 §4.67（selector）或 `MF0-C`（formation）的 FAIL。
- **不得**宣稱它解決了開放集或自然語言。
- `Store` 的 membership／conflict 契約（§4.57／§4.64／§4.68）**可繼承方法，不繼承驗證**
  —— 若換 key 型別，須重驗。
