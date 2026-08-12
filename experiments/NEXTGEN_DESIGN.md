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
| **可觀測契約** | (a) 給定同一組 carrier 與同一段文字,輸出**決定性**;(b) 生成任務中,absent key 必答 `?` 且零交付;(c) **僅對沒有順序語意的同角色 delivery set** 順序不變 |

**(b) 的措辭已收窄**（Codex [169]）:這只是**本生成任務**的 absent-key 行為,
**不是**「一般語言模型不會幻覺」那種宣稱。

**(c) 也已收窄**:順序不變**只適用於同角色、無順序語意的 delivery set**。
若未來真的交付多項,**Interface 必須以明示的 metadata／slot 定義語意順序**,
**Core 不得**從 raw insertion position 猜 recency 或 identity。
⚠️ 訓練時 shuffle **不等於**契約已驗證 —— 目前它從未被量過,不得寫成已成立。

### 2.2 Memory Interface

| | |
|---|---|
| **擁有的 state** | 無持久 state。**每次呼叫都是純函數。** |
| **職責** | `文字 → key`(讀寫兩側)、`key → address`、`latent ↔ 交付張量` |
| **可觀測契約** | (a) 讀寫兩側對同一實體必須產生**同一個 key**;(b) 無法唯一化時**必須拒絕**且**零交付**;(c) 拒絕與交付之間沒有第三種狀態;(d) **唯一化在交付之前完成** |

**(d) 是架構的硬分工**（Codex [169] 指出我草案的結構衝突）:
**Interface 唯一化後才 deliver,不得把多個候選丟給 Core 讓它自己 routing。**
舊 bridge 正是那樣做的,而 §4.63 證明 Core 會把 routing 塌成 attr-only。
把那個分工帶進下一代,等於重製一個已知會壞的設計。

**(a) 是整個系統的樞紐,而且從未被測過** —— LKE 系列只做了讀取側。
寫入時說「我的門號密碼」與讀取時問「門號密碼多少」必須映到同一個 key,
否則寫進去的東西永遠讀不出來,而且 guard 會誠實地 abstain:**安全但無用**。

### 2.3 Store

| | |
|---|---|
| **擁有的 state** | `key → (address, version, z)`。**唯一的持久 state。** |
| **可觀測契約** | 已在 §4.57／§4.64／§4.68 驗過:membership 可判定、dangling fail-closed、conflict/overwrite 全序 |

**Store 是目前唯一不需要重做的部分。**

### 2.4 §4.67 **不是**「Interface 唯一化」的同義詞

⚠️ Codex [170] 的收窄,必須一路帶著:

> **§4.67 是「一種 learned closed-set confidence selector」的 FAIL,
> 不是所有 Interface 唯一化都不可行。`K0` 的 exact lookup 仍是可用的受限路徑。**

我自己在 [170] 寫過「那正是 §4.67 FAIL 的地方」——
**那句話會讓一條還沒被否證的路徑看起來已經死了。**
把「某一種 learned selector 失敗」與「唯一化這件事不可行」混為一談,
是這份設計最容易犯、也最難察覺的過度概括。

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

## 8. 三個問題的裁決（Codex [169]）

### Q1 —— `NG-O2` 是第一個**實驗**,但 key ABI 先凍結

key ABI 的凍結是**規格前置,不是另開實驗**。O2 用 oracle `k*` 做讀寫,
量的是**新 schema ＋ Core 能否消費「已唯一選出的」latent**,
以及 **Store 能否讓同 entity 的多 attribute 共存**。**它不測 canonicalizer。**

### Q2 —— 鎖的是 key 的 **ABI**,不選 learned canonicalizer 機制

第一版 **`K0`**,fail-closed 的 deterministic scaffold:

```
key = (scope_id, entity_norm, attribute_norm)
```

- `scope_id` 由**可信的** session／source metadata 決定（例如 `SELF`）。
- `norm` **只准** Unicode NFKC、casefold、空白／標點正規化。
- **不得**偷偷做 synonym、fuzzy match 或 embedding nearest-neighbor。
- Store **永遠保留 full key** 作 exact equality。
- 若 address code 由 key 導出,**collision／驗證失敗必 fail-closed** ——
  **hash 相同不等於 identity 相同**。

**`K0` 只可稱 literal canonicalization baseline,不得稱自然語言理解。**
paraphrase／別名／coreference 的 learned canonicalization 是 O2 之後的 **`K1`**,
且**必須先有獨立的 read-write agreement gate**,不得藉 selector 分數放行。

### Q3 —— 值的表示採 **`V0` 固定格式短值**

generation-time exact ground truth 的 **2–4 位 decimal**（輸出容許既定的分位空白正規化）,
**每 episode 重抽**,讀取 prompt **絕不含 value text**。
這先隔離「值能否由 latent 消費」,不把 tokenizer／長文字 copy 的壓力混進 O2。

任意字串**不必然**需要人工判讀（日後可用生成值的 normalized exact match）,
但它是**獨立的 `V1` copy/output 軸**,**不得**靠 `V0` PASS 外推。
