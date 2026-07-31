"""把推理資料集 tokenize 成固定張量，供架構對照的 reasoning SFT 使用。

選 BelleGroup/school_math_0.25M 當主資料集，理由：
  * 簡體中文，與 pretrain 用的 pretrain_t2t_mini 語言分布一致（不會把
    「英文能力不足」誤判成「推理能力不足」）
  * 24.8 萬筆帶完整 CoT 的數學應用題
  * 中位數 131 tokens，99.6% 落在 512 以內

Loss 遮罩：prompt 段與 padding 段都設為 -100。
（`dataset/data_reasoning.py` 只遮 prompt，padding 位置保留 pad_token_id=0 當作
 預測目標，等於訓練模型去輸出 pad —— 這裡不沿用那個做法。）

val 段額外保留原始文字，供生成式的 exact-match 評測使用。
"""
import os
import sys
import json
import argparse

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

import torch
from datasets import load_dataset
from transformers import AutoTokenizer

HERE = os.path.dirname(os.path.abspath(__file__))
SEQ_LEN = 512


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", type=int, default=100_000)
    ap.add_argument("--val", type=int, default=2_000)
    ap.add_argument("--out", default=os.path.join(HERE, "reasoning_tokens.pt"))
    args = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    pad = tok.pad_token_id
    print(f"tokenizer pad={pad} bos={tok.bos_token_id} eos={tok.eos_token_id}")

    ds = load_dataset("BelleGroup/school_math_0.25M", split="train")
    print(f"Belle 原始樣本數 {len(ds)}")

    need = args.train + args.val
    ids_all, plen_all, tlen_all, raw = [], [], [], []
    skipped = 0

    for item in ds:
        prompt = item["instruction"].strip()
        if item.get("input"):
            prompt += "\n" + item["input"].strip()
        response = item["output"].strip()
        if not prompt or not response:
            continue

        p_ids = tok(tok.bos_token + prompt, add_special_tokens=False)["input_ids"]
        r_ids = tok(response + tok.eos_token, add_special_tokens=False)["input_ids"]
        total = len(p_ids) + len(r_ids)
        if total > SEQ_LEN:          # 硬過濾，確保推理鏈完整而非被截斷
            skipped += 1
            continue

        ids_all.append(p_ids + r_ids + [pad] * (SEQ_LEN - total))
        plen_all.append(len(p_ids))
        tlen_all.append(total)
        raw.append({"prompt": prompt, "response": response})

        if len(ids_all) >= need:
            break
        if len(ids_all) % 20000 == 0:
            print(f"  已處理 {len(ids_all)}/{need}", flush=True)

    if len(ids_all) < need:
        raise RuntimeError(f"樣本不足：{len(ids_all)} < {need}")

    ids = torch.tensor(ids_all, dtype=torch.int16)
    plen = torch.tensor(plen_all, dtype=torch.int16)
    tlen = torch.tensor(tlen_all, dtype=torch.int16)

    n_t = args.train
    torch.save({
        "train_ids": ids[:n_t], "train_plen": plen[:n_t], "train_tlen": tlen[:n_t],
        "val_ids": ids[n_t:], "val_plen": plen[n_t:], "val_tlen": tlen[n_t:],
        "seq_len": SEQ_LEN, "pad_token_id": pad,
    }, args.out)

    # val 的原始文字：exact-match 評測要用來生成與比對
    val_raw = os.path.join(HERE, "reasoning_val_raw.json")
    with open(val_raw, "w", encoding="utf-8") as f:
        json.dump(raw[n_t:], f, ensure_ascii=False, indent=1)

    resp_tokens = (tlen[:n_t].long() - plen[:n_t].long()).sum().item()
    print(f"\n✅ 已存至 {args.out} ({os.path.getsize(args.out)/1e6:.0f} MB)")
    print(f"   train={n_t}  val={args.val}  (因超長捨棄 {skipped} 筆)")
    print(f"   train 的 response token 數 = {resp_tokens/1e6:.2f}M（只有這些會算 loss）")
    print(f"   val 原始文字 → {val_raw}")


if __name__ == "__main__":
    main()
