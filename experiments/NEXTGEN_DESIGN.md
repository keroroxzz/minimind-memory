# 下一代 core / interface 設計閘 —— **草案，交 review**

Codex [168] 要求：不動 code，先鎖 acceptance target、各元件擁有的 state 與可觀測契約、
不可繼承的東西、最小 end-to-end task、oracle ceilings、fault attribution、停止規則。
**之後才決定 selector 或 write formation 的第一關。**

橋接線的整體 status 見 `research.md` §4.69。一句話:
**儲存層每一關都過,倒下的都在接口。**

---

## 1. Acceptance target

系統要能做到、而現在**做不到**的:

```
使用者: 我的門號密碼是 47
使用者: 我的置物櫃密碼是 82          ← **同一個實體、第二個屬性**
使用者: 對了 我家 wifi 是 1234        ← **任意順序、任意時間**
...
使用者: 我門號密碼多少?              ← 自然問句
系統  : 4 7
```

四個硬條件,缺一不可:

1. **自然 query** —— 不是受控文法的 slot filling。
2. **無 value placeholder、無 value text** —— 值不出現在任何一側的文字裡。
3. **同一 entity 可有多個 attribute** —— §4.63 封存的正是這一條。
4. **任意順序的多筆寫入** —— 寫入時不知道未來會問什麼。

**目前的 bridge 系統四條全不滿足**:文法受控、schema 固定 3 個屬性、
同實體多屬性封存、寫入端從未做。

---

## 2. 三個元件各自擁有什麼 state,以及可觀測契約

界線的原則:**可判定的事實歸 Store,近似與泛化歸 Interface,推理歸 Core。**
§4.38 那條教訓要一路貫徹 —— **能查表的事情不准問學出來的相似度。**

### 2.1 Reasoning Core

| | |
|---|---|
| **擁有的 state** | 權重。**不擁有任何記憶內容。** |
| **輸入** | token 序列 ＋ 一組 memory carrier(latent) |
| **輸出** | token |
| **可觀測契約** | (a) 給定同一組 carrier 與同一段文字,輸出**決定性**;(b) carrier 為空時**不得**編造記憶內容;(c) 對 carrier 的**順序**不變 |

**(c) 是新的**。目前靠訓練時隨機打亂 carrier 順序達成,但從未被當作契約驗證過。

### 2.2 Memory Interface

| | |
|---|---|
| **擁有的 state** | 無持久 state。**每次呼叫都是純函數。** |
| **職責** | `文字 → key`(讀寫兩側)、`key → address`、`latent ↔ 交付張量` |
| **可觀測契約** | (a) 讀寫兩側對同一實體必須產生**同一個 key**;(b) 無法唯一化時**必須拒絕**且**零交付**;(c) 拒絕與交付之間沒有第三種狀態 |

**(a) 是整個系統的樞紐,而且從未被測過** —— LKE 系列只做了讀取側。
寫入時說「我的門號密碼」與讀取時問「門號密碼多少」必須映到同一個 key,
否則寫進去的東西永遠讀不出來,而且 guard 會誠實地 abstain:**安全但無用**。

### 2.3 Store

| | |
|---|---|
| **擁有的 state** | `key → (address, version, z)`。**唯一的持久 state。** |
| **可觀測契約** | 已在 §4.57／§4.64／§4.68 驗過:membership 可判定、dangling fail-closed、conflict/overwrite 全序 |

**Store 是目前唯一不需要重做的部分。**

---

## 3. 哪些**不能**從現有 bridge artifacts 繼承

| artifact | 能否繼承 | 理由 |
|---|---|---|
| `Store`(含 `commit_v`／`dangle`／`verify`) | ✅ **可以** | 契約與內容無關,只要 key 與 z 的型別不變 |
| typed guard 紀律、injector 單一入口、逐位元 parity 驗法 | ✅ **可以** | 是方法論不是權重 |
| `bridge_core_latent.pth` | ❌ **不可** | 訓練時 schema 固定 `z'=[addr16, value, attr3]`,**attr 是 3 類 one-hot**;§4.63 已證明它在同實體多屬性下把 routing 塌到 attr |
| `φ`(固定 16×3 查表) | ❌ **不可** | 是**封閉世界的查表**。開放集需要由內容導出的 address 函數,不是查表 |
| schema `z'` | ❌ **不可** | `value` 是單一純量(`v/100`)、`attr` 是 3 類 one-hot,兩者都不能表達任意知識 |
| LKE-1／2R 的 extractor | ❌ **不可** | 16-way ＋ 3-way 的 **closed-set head**,結構上無法表示未見實體 |
| renderer 的值格式(`"4 7"` 空格分位) | ⚠️ **需重新決定** | 是為了繞開 6400 BPE 的數字切分;新任務的值不一定是兩位數 |

**要重做的是 core、schema、address 函數、extractor 四樣 —— 也就是除了 Store 以外的全部。**

---

## 4. 最小 end-to-end task(建議,待裁)

**不要**一步跳到真實自然語言。建議的最小任務同時滿足四個硬條件,但仍保有 ground truth:

- **實體**:開放集,由**未見過的**人名／物名組成,寫入時才第一次出現。
- **屬性**:開放集,由自然語言短語表達(`門號密碼`／`置物櫃密碼`／`wifi`),
  **不預先枚舉**、不給 one-hot。
- **值**:固定格式的短字串(維持可逐 token 驗)。
- **一個 session**:`m` 筆寫入(任意順序、同實體可多屬性)＋ `q` 個問題。
- **ground truth**:由生成器持有,**不做事後人工判句**(沿用 LKE-1 的紀律)。

**與 LKE-1／2R 的差別**:那兩關的 entity 與 attr 都是**封閉集合**,
所以 extractor 可以是 16-way ＋ 3-way 的分類器。**這裡不行** ——
key 必須由內容導出(例如 canonical 化的字串),address 必須由 key 導出,
兩者都不能是查表。

---

## 5. Oracle ceilings —— **先量,再談 learned**

任何 learned 元件開跑前,以下三個 ceiling 都必須先量,且每一個都要 `>=95%`,
否則後續的失敗無法歸因(§4.66／§4.67 的做法):

| ceiling | 內容 | 排除什麼 |
|---|---|---|
| **O1 delivery** | oracle key ＋ oracle latent → core 作答 | core 的消費能力 |
| **O2 同實體多屬性** | 同一 entity 的 2–3 個 attr 同時在 store 裡,oracle 取用 | **§4.63 的失敗是否已被新 schema 解掉** |
| **O3 round-trip** | 寫入 → store → 讀出 → 交付,全程 oracle key | Store 與 delivery 的接合 |

**O2 是這一代的關鍵閘。** 若新 schema／新 core 仍過不了 O2,
那就不必往下做 extractor —— 產品目標在該處已經斷了。

---

## 6. Fault attribution —— 讀寫兩側都要能分開

§4.67 之所以能乾淨歸因,是因為 `U` 的 raw exact 與 oracle ceiling 都先量過。
新系統的失敗面更大,必須**事前**把每一格定義好:

| 故障 | 觀測 | 安全性後果 |
|---|---|---|
| **寫入側抽取錯** | `k_write != k*` | 記憶存到錯的 key —— **之後永遠讀不到**,且可能覆寫別人的 |
| **寫入側 address 形成錯** | key 對但 address 不對 | store 契約自檢應該擋下(`verify()`) |
| **讀寫 key 不一致** | `k_read != k_write` 但兩者各自「正確」 | **安全但無用**:guard 誠實 abstain |
| **讀取側抽取錯** | `k_read` 指到別的既有 entry | **unsafe wrong-existing**(§4.67 量過的那一種) |
| **selector 誤拒** | key 正確卻被拒 | utility 損失 |
| **delivery／core** | key 與 z 都對,答案錯 | 由 O1 ceiling 界定 |

**「安全但無用」必須獨立成一格。** §4.67 已經示範:
一個永遠拒絕的系統 halluc = 0,而那不是成功。

---

## 7. 停止規則

- 每一關**一次跑完**,事前鎖判準,FAIL 即 seal 並記歸因,**不換配方重試**。
- **O2 未過 → 不進入 extractor**。這是硬性順序,不是建議。
- 任何 PASS 只能宣稱**當關的範圍**,不得升格。
- 措辭紀律沿用:規則式的不得叫 semantic;受控文法的不得叫 natural;
  未 exercise 的機制不得叫「已驗證」。

---

## 8. 待裁的三個問題

1. **第一關該是 O2(同實體多屬性的新 schema/core),還是先鎖 key 的
   canonical 化規則?** 我傾向 **O2 先**,因為它是產品目標的斷點,
   而且不需要任何 learned 元件就能量。
2. **key 的 canonical 化**要用什麼機制?這是 §4.67 失敗的正面問題。
   我沒有好答案,**刻意不在此提方案**,以免又變成「先想到一個機制再找理由」。
3. **值的表示**:繼續用固定格式短字串(可逐 token 驗),
   還是要求支援任意字串?後者會讓 evaluation 立刻退化成人工判讀。
