import os
import sys
import unittest
import torch

__package__ = "test"
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from model.model_minimind import MiniMindConfig, MiniMindModel, EngramManager

class TestEngramV2(unittest.TestCase):
    def setUp(self):
        self.config = MiniMindConfig(
            hidden_size=256,
            num_hidden_layers=4,
            engram_layers=[1, 2],
            engram_vocab_size=1000,
            n_head_per_ngram=2,
            max_ngram_size=3,
            engram_offload_cpu=True
        )
        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        self.model = MiniMindModel(self.config).to(self.device)
        self.engram = self.model.engram_system

    def test_cpu_offloading_integrity(self):
        """Ensure the embedding table stays on CPU even if the model is moved to GPU."""
        if self.device == 'cuda':
            self.assertEqual(self.engram.embedding_table.embedding.weight.device.type, 'cpu')
            # Verify other components are on GPU
            self.assertEqual(self.engram.fusions['1']['value_proj'].weight.device.type, 'cuda')

    def test_hashing_consistency(self):
        """Verify that hashing is deterministic and handles history correctly."""
        input_ids = torch.tensor([[10, 20, 30, 40]], device=self.device)
        
        # 1. Full sequence hash
        hashes_full = self.engram.get_hashes(input_ids, L_curr=4)
        
        # 2. Incremental hash for the last token
        # When we are at the 4th token (40), the full context is [10, 20, 30, 40]
        hashes_incremental = self.engram.get_hashes(input_ids, L_curr=1)
        
        # The last position of the full hash should match the incremental hash
        self.assertTrue(torch.equal(hashes_full[:, -1:, :], hashes_incremental))
        print("✅ Hash consistency verified (Full vs Incremental)")

    def test_stage1_gather_device(self):
        """Verify Stage 1 gathers from CPU and returns to GPU."""
        input_ids = torch.tensor([[1, 2, 3, 4, 5]], device=self.device)
        features = self.engram.stage1_gather(input_ids, L_curr=5)
        
        self.assertEqual(features.device.type, self.device.split(':')[0])
        self.assertEqual(features.shape, (1, 5, self.config.n_embed_per_ngram * (self.config.max_ngram_size - 1)))

    def test_end_to_end_equivalence(self):
        """
        Critical Test: Ensure that engram features for a token are identical 
        whether processed in a batch or incrementally (like in generation).
        """
        input_ids = torch.tensor([[101, 102, 103, 104, 105]], device=self.device)
        
        # Step A: Process whole sequence
        with torch.no_grad():
            features_full = self.engram.stage1_gather(input_ids, L_curr=5)
            # Take the features for the 4th token (index 3)
            feat_idx3_full = features_full[:, 3:4, :]

        # Step B: Process incrementally (simulating generation at step 4)
        # current input is just [104], but full_input_ids is [101, 102, 103, 104]
        curr_input = input_ids[:, 3:4]
        full_input = input_ids[:, :4]
        
        with torch.no_grad():
            feat_idx3_inc = self.engram.stage1_gather(full_input, L_curr=1)

        # They must be identical
        torch.testing.assert_close(feat_idx3_full, feat_idx3_inc)
        print("✅ End-to-end equivalence verified (Batch vs Incremental)")

    def test_differentiability(self):
        """Ensure gradients can flow back to the CPU table (if not frozen)."""
        input_ids = torch.tensor([[1, 2, 3]], device=self.device)
        hidden_states = torch.randn(1, 3, self.config.hidden_size, device=self.device, requires_grad=True)
        
        engram_features = self.engram.stage1_gather(input_ids, L_curr=3)
        output = self.engram.stage2_fusion(1, hidden_states, engram_features)
        
        loss = output.sum()
        loss.backward()
        
        # Check if gradients exist in the CPU table
        self.assertIsNotNone(self.engram.embedding_table.embedding.weight.grad)
        print("✅ Gradient flow to CPU verified")

if __name__ == '__main__':
    unittest.main()
