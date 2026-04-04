import time
import argparse
import random
import warnings
import json
import torch
import math
import re
from collections import Counter
from transformers import AutoTokenizer, AutoModelForCausalLM, TextStreamer
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM
from model.model_lora import *
from trainer.trainer_utils import setup_seed, get_model_params, get_model_paths
warnings.filterwarnings('ignore')

def init_model(args):
    tokenizer = AutoTokenizer.from_pretrained(args.load_from)
    if 'model' in args.load_from:
        model = MiniMindForCausalLM(MiniMindConfig(
            hidden_size=args.hidden_size,
            num_hidden_layers=args.num_hidden_layers,
            use_moe=bool(args.use_moe),
            use_engram=bool(args.use_engram),
            use_dde=bool(args.use_dde),
            dde_layer=args.dde_layer,
            inference_rope_scaling=args.inference_rope_scaling
        ))
        ckp_path, _ = get_model_paths(args.save_dir, args.weight, model.config)
        model.load_state_dict(torch.load(ckp_path, map_location=args.device), strict=False)
        if args.lora_weight != 'None':
            apply_lora(model)
            load_lora(model, f'./{args.save_dir}/{args.lora_weight}_{args.hidden_size}.pth')
    else:
        model = AutoModelForCausalLM.from_pretrained(args.load_from, trust_remote_code=True)
    get_model_params(model, model.config)
    return model.half().eval().to(args.device), tokenizer

def format_msgs(msgs):
    prompt = ""
    for msg in msgs:
        role = msg['role']
        content = msg['content']
        prompt += f"<|im_start|>{role}\n{content}<|im_end|>\n"
    return prompt

def tokenize(text):
    if re.search(r'[\u4e00-\u9fff]', text):
        return list(text.lower().replace(" ", ""))
    return text.lower().split()

def compute_f1(prediction, ground_truth):
    prediction_tokens = tokenize(prediction)
    ground_truth_tokens = tokenize(ground_truth)
    if not prediction_tokens or not ground_truth_tokens: return 0.0
    common = Counter(prediction_tokens) & Counter(ground_truth_tokens)
    num_same = sum(common.values())
    if num_same == 0: return 0.0
    precision = 1.0 * num_same / len(prediction_tokens)
    recall = 1.0 * num_same / len(ground_truth_tokens)
    f1 = (2 * precision * recall) / (precision + recall)
    return f1

def compute_bleu(prediction, ground_truth):
    def ngrams(tokens, n):
        return [tuple(tokens[i:i+n]) for i in range(len(tokens)-n+1)]
    pred_tokens = tokenize(prediction)
    gt_tokens = tokenize(ground_truth)
    if not pred_tokens or not gt_tokens: return 0.0
    weights = [0.25, 0.25, 0.25, 0.25]
    p_ns = []
    for n in range(1, 5):
        p_ngrams = ngrams(pred_tokens, n)
        g_ngrams = ngrams(gt_tokens, n)
        if not p_ngrams:
            p_ns.append(0.0)
            continue
        common = Counter(p_ngrams) & Counter(g_ngrams)
        p_ns.append(sum(common.values()) / len(p_ngrams))
    if p_ns[0] == 0: return 0.0
    score = 0
    for w, p in zip(weights, p_ns):
        if p > 0: score += w * math.log(p)
    bp = math.exp(min(0, 1 - len(gt_tokens) / len(pred_tokens)))
    return bp * math.exp(score)

# --- 核心推理邏輯：雙階段 DDE 生成 ---
def generate_dde_secure(model, tokenizer, prompt, split_idx, args):
    inputs = tokenizer(prompt, return_tensors="pt", add_special_tokens=False).to(args.device)
    input_ids = inputs["input_ids"]
    
    with torch.no_grad():
        # Phase 1: Prefill (產生記憶)
        # 呼叫 MiniMindForCausalLM 的 forward (底層的 MiniMindModel 會回傳 dde_memory)
        outputs = model(input_ids=input_ids, split_idx=split_idx, dde_temp=args.dde_temp)
        memory_state = outputs.dde_memory
        
        # Phase 2: Decode (鎖定記憶，逐字生成)
        generated_ids = input_ids
        for _ in range(args.max_new_tokens):
            # 傳入 past_memory，模型會自動進入鎖定模式
            out = model(generated_ids, past_memory=memory_state, dde_temp=args.dde_temp)
            logits = out.logits
            
            # 數值檢查 (Sanity Check)
            if torch.isnan(logits).any() or torch.isinf(logits).any():
                print("⚠️ [WARNING] NaN or Inf detected in Logits! Inference collapsed.")
                break
                
            next_token_id = torch.argmax(logits[:, -1, :], dim=-1).unsqueeze(-1)
            
            if next_token_id.item() == tokenizer.eos_token_id:
                break
            
            generated_ids = torch.cat([generated_ids, next_token_id], dim=-1)
            
    return generated_ids

def evaluate_dde(model, tokenizer, args):
    print(f"🚀 Starting DDE-v1.7 Secure Evaluation on {args.data_path}...")
    with open(args.data_path, 'r', encoding='utf-8') as f:
        all_samples = json.load(f)
    
    test_range = args.test_count if args.test_count > 0 else len(all_samples)
    samples = all_samples[-test_range:]
    
    metrics = {"f1": [], "bleu": [], "acc": []}
    total = len(samples)
    
    for i, sample in enumerate(samples):
        messages = sample['dialogue']
        context_messages = messages[:-2]
        query_messages = messages[-2:-1]
        gt_answer = messages[-1]['content'].strip()
        
        context_prompt = format_msgs(context_messages)
        context_tokens = tokenizer(context_prompt, add_special_tokens=False).input_ids
        split_idx = len(context_tokens)
        
        full_msgs = context_messages + query_messages
        prompt = format_msgs(full_msgs) + f"<|im_start|>assistant\n"
        
        # 使用雙階段生成
        output_ids = generate_dde_secure(model, tokenizer, prompt, split_idx, args)
        
        # [關鍵修正] 清理輸出中的 <think> 與 assistant 標籤
        response = tokenizer.decode(output_ids[0][len(tokenizer(prompt, add_special_tokens=False).input_ids):], skip_special_tokens=True).strip()
        response = re.sub(r'<think>.*?</think>', '', response, flags=re.DOTALL).strip()
        response = response.replace('assistant\n', '').strip()
        
        f1 = compute_f1(response, gt_answer)
        bleu = compute_bleu(response, gt_answer)
        
        # [科學判定] 只要模型包含 GT 事實或 F1 分數夠高 (代表核心語意對了)，即視為 PASS
        is_correct = False
        if response:
            is_correct = (gt_answer.lower() in response.lower()) or (response.lower() in gt_answer.lower()) or (f1 > 0.5)
        
        metrics["f1"].append(f1)
        metrics["bleu"].append(bleu)
        metrics["acc"].append(1.0 if is_correct else 0.0)
        
        print(f"[{i+1}/{total}] | F1: {f1:.2f} | BLEU: {bleu:.2f} | {'✅ PASS' if is_correct else '❌ FAIL'}")
        if not is_correct:
            print(f"  GT: {gt_answer[:60]}...")
            print(f"  Model: {response[:60] if response else '(EMPTY)'}")
        
    avg_f1 = sum(metrics["f1"]) / total
    avg_bleu = sum(metrics["bleu"]) / total
    avg_acc = sum(metrics["acc"]) / total
    
    print(f"\n📊 Final Report: Accuracy={avg_acc*100:.2f}% | F1={avg_f1*100:.2f} | BLEU={avg_bleu*100:.2f}")

def main():
    parser = argparse.ArgumentParser(description="MiniMind DDE Evaluation v1.7")
    parser.add_argument('--mode', default='eval', choices=['eval', 'chat'])
    parser.add_argument('--data_path', default='../dataset/dde-v1.jsonl', type=str)
    parser.add_argument('--test_count', default=50, type=int)
    parser.add_argument('--load_from', default='model', type=str)
    parser.add_argument('--save_dir', default='out', type=str)
    parser.add_argument('--weight', default='dde_v1', type=str)
    parser.add_argument('--lora_weight', default='None', type=str)
    parser.add_argument('--hidden_size', default=768, type=int)
    parser.add_argument('--num_hidden_layers', default=8, type=int)
    parser.add_argument('--use_moe', default=0, type=int)
    parser.add_argument('--use_engram', default=0, type=int)
    parser.add_argument('--inference_rope_scaling', default=False, action='store_true')
    parser.add_argument('--max_new_tokens', default=64, type=int)
    parser.add_argument('--temperature', default=0.7, type=float)
    parser.add_argument('--top_p', default=0.9, type=float)
    parser.add_argument('--device', default='cuda' if torch.cuda.is_available() else 'cpu', type=str)
    parser.add_argument('--use_dde', default=1, type=int)
    parser.add_argument('--dde_layer', default=4, type=int)
    parser.add_argument('--dde_temp', default=0.2, type=float)

    args = parser.parse_args()
    model, tokenizer = init_model(args)
    if args.mode == 'eval':
        evaluate_dde(model, tokenizer, args)

if __name__ == "__main__":
    main()
