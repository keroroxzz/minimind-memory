import os
import argparse
import time
import torch
from transformers import AutoTokenizer
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM
from transformers import AutoTokenizer, AutoModelForCausalLM, TextStreamer
from model.model_lora import *
from trainer.trainer_utils import get_model_params, get_model_paths

def init_model(args):
    tokenizer = AutoTokenizer.from_pretrained(args.load_from)
    if 'model' in args.load_from:
        model = MiniMindForCausalLM(MiniMindConfig(
            hidden_size=args.hidden_size,
            num_hidden_layers=args.num_hidden_layers,
            use_moe=bool(args.use_moe),
            use_engram=bool(args.use_engram),
            use_dense_attention=bool(args.use_dense_attention),
            use_latent_attention=bool(args.use_latent_attention),
            inference_rope_scaling=args.inference_rope_scaling
        ))
        
        if os.path.exists(args.weight):
            ckp_path = args.weight
        else:
            ckp_path, _ = get_model_paths(args.save_dir, args.weight, model.config)
            
        # 支援 .safetensors 格式
        if ckp_path.endswith('.safetensors'):
            from safetensors.torch import load_file
            state_dict = load_file(ckp_path, device=args.device)
        else:
            state_dict = torch.load(ckp_path, map_location=args.device, weights_only=False)
            
        model.load_state_dict(state_dict, strict=False)
        
        if args.lora_weight != 'None':
            apply_lora(model)
            load_lora(model, f'./{args.save_dir}/{args.lora_weight}_{args.hidden_size}.pth')
    else:
        model = AutoModelForCausalLM.from_pretrained(args.load_from, trust_remote_code=True)
    get_model_params(model, model.config)
    return model.half().eval().to(args.device), tokenizer

def eval_reasoning(args):
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    model, tokenizer = init_model(args)

    test_queries = [
        "你有什么特长？", # legacy question to check if model can still answer basic questions
        "如果我有3个苹果，你又给了我5个，然後我吃了2个，我还剩下几个苹果？",
        "请问 12 乘以 15 等于多少？",
        "老师有24个气球，平均分给6个小朋友，每个小朋友分到几个？",
    ]
    
    print(f"\n--- 正在評估模型: {os.path.basename(args.weight)} (Dense Attention: {args.use_dense_attention}) ---")
    
    streamer = TextStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True)

    for query in test_queries:
        inputs = tokenizer.bos_token + query
        inputs = tokenizer(inputs, return_tensors="pt", truncation=True).to(args.device)
        input_ids = inputs["input_ids"]
        
        print(f"\n問題: {query}\n回答：", end="", flush=True)
        
        st = time.time()
        with torch.no_grad():
            outputs = model.generate(
                input_ids=input_ids,
                attention_mask=inputs["attention_mask"],
                max_new_tokens=args.max_new_tokens,
                pad_token_id=tokenizer.pad_token_id,
                eos_token_id=tokenizer.eos_token_id,
                temperature=args.temperature,
                do_sample=True,
                top_p=args.top_p,
                repetition_penalty=1,
                streamer=streamer
            )[0]

        gen_tokens = len(outputs) - len(input_ids[0])
        print(f'\n[Speed]: {gen_tokens / (time.time() - st):.2f} tokens/s\n\n') if args.show_speed else print('\n\n')

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MiniMind模型推理与对话")
    parser.add_argument('--load_from', default='model', type=str, help="模型加载路径（model=原生torch权重，其他路径=transformers格式）")
    parser.add_argument('--weight', default='full_sft', type=str, help="权重名称前缀（pretrain, full_sft, rlhf, reason, ppo_actor, grpo, spo）")
    parser.add_argument('--lora_weight', default='None', type=str, help="LoRA权重名称（None表示不使用，可选：lora_identity, lora_medical）")
    parser.add_argument('--hidden_size', default=768, type=int, help="隐藏层维度")
    parser.add_argument('--num_hidden_layers', default=8, type=int, help="隐藏层数量")
    parser.add_argument('--use_moe', default=1, type=int, choices=[0, 1], help="是否使用MoE架构（0=否，1=是）")
    parser.add_argument('--use_engram', default=1, type=int, choices=[0, 1], help="是否使用Engram架构（0=否，1=是）")
    parser.add_argument('--use_dense_attention', default=0, type=int, choices=[0, 1], help="是否使用Dense Attention架构（0=否，1=是）")
    parser.add_argument('--use_latent_attention', default=0, type=int, choices=[0, 1], help="是否使用Latent Attention架构（0=否，1=是）")
    parser.add_argument('--inference_rope_scaling', default=False, action='store_true', help="启用RoPE位置编码外推（4倍，仅解决位置编码问题）")
    parser.add_argument('--max_new_tokens', default=8192, type=int, help="最大生成长度（注意：并非模型实际长文本能力）")
    parser.add_argument('--temperature', default=0.85, type=float, help="生成温度，控制随机性（0-1，越大越随机）")
    parser.add_argument('--top_p', default=0.95, type=float, help="nucleus采样阈值（0-1）")
    parser.add_argument('--open_thinking', default=0, type=int, help="是否开启自适应思考（0=否，1=是）")
    parser.add_argument('--historys', default=0, type=int, help="携带历史对话轮数（需为偶数，0表示不携带历史）")
    parser.add_argument('--show_speed', default=1, type=int, help="显示decode速度（tokens/s）")
    parser.add_argument('--device', default='cuda' if torch.cuda.is_available() else 'cpu', type=str, help="运行设备")
    args = parser.parse_args()

    eval_reasoning(args)
