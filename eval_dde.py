import time
import argparse
import random
import warnings
import json
import torch
import math
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

# --- 標準指標計算工具 ---
def compute_f1(prediction, ground_truth):
    prediction_tokens = prediction.lower().split()
    ground_truth_tokens = ground_truth.lower().split()
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
    
    pred_tokens = prediction.lower().split()
    gt_tokens = ground_truth.lower().split()
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
    
    # 幾何平均
    score = 0
    for w, p in zip(weights, p_ns):
        if p > 0: score += w * math.log(p)
    
    # 長度懲罰 (Brevity Penalty)
    bp = math.exp(min(0, 1 - len(gt_tokens) / len(pred_tokens)))
    return bp * math.exp(score)
# ----------------------

def evaluate_dde(model, tokenizer, args):
    print(f"🚀 Starting Advanced Quantitative Evaluation on {args.data_path}...")
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
        
        scenario = sample.get('scenario', 'Unknown')
        field = sample.get('field', 'General')
        
        context_prompt = format_msgs(context_messages)
        context_tokens = tokenizer(context_prompt, add_special_tokens=False).input_ids
        split_idx = len(context_tokens)
        
        full_msgs = context_messages + query_messages
        prompt = format_msgs(full_msgs) + f"<|im_start|>assistant\n"
        
        inputs = tokenizer(prompt, return_tensors="pt", add_special_tokens=False).to(args.device)
        
        output_ids = model.generate(
            input_ids=inputs["input_ids"],
            attention_mask=inputs["attention_mask"],
            max_new_tokens=args.max_new_tokens,
            do_sample=False, 
            split_idx=split_idx,
            dde_temp=args.dde_temp,
            eos_token_id=tokenizer.eos_token_id,
            pad_token_id=tokenizer.pad_token_id
        )
        
        response = tokenizer.decode(output_ids[0][len(inputs["input_ids"][0]):], skip_special_tokens=True).strip()
        
        # 指標計算
        f1 = compute_f1(response, gt_answer)
        bleu = compute_bleu(response, gt_answer)
        is_correct = gt_answer.lower() in response.lower() or response.lower() in gt_answer.lower()
        
        metrics["f1"].append(f1)
        metrics["bleu"].append(bleu)
        metrics["acc"].append(1.0 if is_correct else 0.0)
        
        print(f"[{i+1}/{total}] [{field}/{scenario}] | F1: {f1:.2f} | BLEU: {bleu:.2f} | {'PASS' if is_correct else 'FAIL'}")
        if not is_correct:
            print(f"  Query: {query_messages[0]['content'][:50]}...")
            print(f"  GT: {gt_answer[:50]}...")
            print(f"  Model: {response[:50]}...")
        
    avg_f1 = sum(metrics["f1"]) / total
    avg_bleu = sum(metrics["bleu"]) / total
    avg_acc = sum(metrics["acc"]) / total
    
    print(f"\n" + "="*50)
    print(f"📊 Final Report (Top-{test_range} Samples)")
    print(f"  - Average Accuracy: {avg_acc*100:.2f}%")
    print(f"  - Average F1 Score: {avg_f1*100:.2f}")
    print(f"  - Average BLEU-4  : {avg_bleu*100:.2f}")
    print("="*50)

def chat_mode(model, tokenizer, args):
    streamer = TextStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True)
    conversation = []
    
    print("Welcome to MiniMind DDE-v1.7 Chat (Type 'exit' to quit)")
    while True:
        prompt = input('💬: ')
        if prompt.lower() in ['exit', 'quit']: break
        
        conversation.append({"role": "user", "content": prompt})
        
        if len(conversation) > 1:
            context_prompt = format_msgs(conversation[:-1])
            split_idx = len(tokenizer(context_prompt, add_special_tokens=False).input_ids)
        else:
            split_idx = 0
            
        full_prompt = format_msgs(conversation) + f"<|im_start|>assistant\n"
        inputs = tokenizer(full_prompt, return_tensors="pt", add_special_tokens=False).to(args.device)

        print('🧠: ', end='')
        generated_ids = model.generate(
            input_ids=inputs["input_ids"], attention_mask=inputs["attention_mask"],
            max_new_tokens=args.max_new_tokens, do_sample=True, streamer=streamer,
            pad_token_id=tokenizer.pad_token_id, eos_token_id=tokenizer.eos_token_id,
            top_p=args.top_p, temperature=args.temperature,
            split_idx=split_idx, dde_temp=args.dde_temp
        )
        response = tokenizer.decode(generated_ids[0][len(inputs["input_ids"][0]):], skip_special_tokens=True)
        conversation.append({"role": "assistant", "content": response})
        print('\n')

def main():
    parser = argparse.ArgumentParser(description="MiniMind DDE Evaluation v1.7")
    parser.add_argument('--mode', default='eval', choices=['eval', 'chat'], help="測試模式")
    parser.add_argument('--data_path', default='../dataset/dde-v1.jsonl', type=str, help="數據路徑")
    parser.add_argument('--test_count', default=50, type=int, help="測試樣本數量")
    parser.add_argument('--load_from', default='model', type=str)
    parser.add_argument('--save_dir', default='out', type=str)
    parser.add_argument('--weight', default='dde_v1', type=str)
    parser.add_argument('--lora_weight', default='None', type=str)
    parser.add_argument('--hidden_size', default=768, type=int)
    parser.add_argument('--num_hidden_layers', default=8, type=int)
    parser.add_argument('--use_moe', default=0, type=int)
    parser.add_argument('--use_engram', default=0, type=int)
    parser.add_argument('--inference_rope_scaling', default=False, action='store_true')
    parser.add_argument('--max_new_tokens', default=128, type=int)
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
    else:
        chat_mode(model, tokenizer, args)

if __name__ == "__main__":
    main()
