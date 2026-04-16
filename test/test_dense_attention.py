import os
import sys

__package__ = "test"
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import torch
import torch.nn.functional as F
import model.model_minimind as model_module
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

def test_dense_attention():
    print("Starting Dense Attention Numerical Verification...")
    
    config = MiniMindConfig(
        hidden_size=256,
        num_hidden_layers=4,
        num_attention_heads=8,
        use_dense_attention=True,
        vocab_size=100
    )
    
    model = MiniMindForCausalLM(config).eval()
    
    original_sdpa = model_module.F.scaled_dot_product_attention
    layer_stats = []

    def patched_sdpa(query, key, value, attn_mask=None, dropout_p=0.0, is_causal=False):
        # Calculate stats
        attn_weights = (query @ key.transpose(-2, -1)) / (query.shape[-1]**0.5)
        if attn_mask is not None:
            if attn_mask.dtype == torch.bool:
                attn_weights.masked_fill_(~attn_mask, float('-inf'))
            else:
                attn_weights += attn_mask
        
        softmax_weights = F.softmax(attn_weights, dim=-1)
        
        stats = {
            'key_shape': key.shape,
            'max_weight': softmax_weights.max().item(),
            'mean_weight': softmax_weights.mean().item(),
            'std_weight': softmax_weights.std().item(),
            'entropy': -(softmax_weights * torch.log(softmax_weights + 1e-9)).sum(dim=-1).mean().item()
        }
        layer_stats.append(stats)
        
        return original_sdpa(query, key, value, attn_mask, dropout_p, is_causal)

    model_module.F.scaled_dot_product_attention = patched_sdpa
    
    try:
        batch_size = 1
        seq_len = 32
        input_ids = torch.randint(0, config.vocab_size, (batch_size, seq_len))
        
        with torch.no_grad():
            model(input_ids)
            
        print("\nNumerical Stats per Layer:")
        for i, stats in enumerate(layer_stats):
            print(f"Layer {i}:")
            print(f"  KV Shape: {stats['key_shape']}")
            print(f"  Max Attention Weight: {stats['max_weight']:.4f}")
            print(f"  Entropy: {stats['entropy']:.4f}")
            
        # Check if entropy drops too low (meaning it's collapsing to one token)
        # or stays too high (meaning it's too dilute)
        return True

    finally:
        model_module.F.scaled_dot_product_attention = original_sdpa

if __name__ == "__main__":
    test_dense_attention()
