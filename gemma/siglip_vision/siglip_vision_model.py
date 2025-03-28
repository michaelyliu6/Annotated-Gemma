# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Siglip vision model for gemma 3 and paligemma."""

import torch
from torch import nn
import torch.nn.functional as F

from . import config as siglip_vision_config
SiglipVisionModelConfig = siglip_vision_config.SiglipVisionModelConfig

class AveragePool2D(nn.Module):
  """Applies 4x4 average pooling and reshaping."""
  def __init__(self, config):
    super().__init__()
    self.config = config

  def forward(self, x):
      """Applies 4x4 average pooling and reshaping."""
      batch_size, seq_len, channels = x.shape
      width = int(seq_len**0.5)
      if width * width != seq_len:
          raise ValueError(
              f"Sequence length {seq_len} is not a perfect square. Cannot reshape to a square image."
          )
      # Bx(64^2)x1152 -> Bx1152x(64^2) -> Bx1152x64x64
      x = x.transpose(1, 2).reshape(batch_size, channels, width, width)
      # Bx1152x64x64-> Bx1152x16x16
      x = F.avg_pool2d(x, kernel_size=4, stride=4)
      # Bx1152x64x64-> Bx1152x256 -> Bx256x1152
      x = x.flatten(2).transpose(1, 2)
      return x

# Siglip Attention
class SiglipAttention(nn.Module):
  """Siglip attention module."""

  def __init__(self, dim, num_heads, head_dim):
    super().__init__()
    self.dim = dim
    self.num_heads = num_heads
    self.head_dim = head_dim

    # Key, Query, Value projections
    self.k_proj = nn.Linear(dim, num_heads * head_dim, bias=True)
    self.q_proj = nn.Linear(dim, num_heads * head_dim, bias=True)
    self.v_proj = nn.Linear(dim, num_heads * head_dim, bias=True)

    # Output projection
    self.o_proj = nn.Linear(num_heads * head_dim, dim, bias=True)

  def forward(self, x):
    batch_size, seq_len, _ = x.size()

    # Project inputs to key, query, value
    k = self.k_proj(x).view(batch_size, seq_len, self.num_heads, self.head_dim)
    q = self.q_proj(x).view(batch_size, seq_len, self.num_heads, self.head_dim)
    v = self.v_proj(x).view(batch_size, seq_len, self.num_heads, self.head_dim)

    # Transpose for multi-head attention
    k = k.transpose(1, 2)  # (batch_size, num_heads, seq_len, head_dim)
    q = q.transpose(1, 2)  # (batch_size, num_heads, seq_len, head_dim)
    v = v.transpose(1, 2)  # (batch_size, num_heads, seq_len, head_dim)

    # Scaled dot-product attention
    scores = torch.matmul(q, k.transpose(-2, -1)) / (self.head_dim**0.5)
    attn_weights = F.softmax(scores, dim=-1)
    attn_output = torch.matmul(attn_weights, v)

    # Transpose back to (batch_size, seq_len, num_heads, head_dim)
    attn_output = attn_output.transpose(1, 2).contiguous()
    attn_output = attn_output.view(
        batch_size, seq_len, self.num_heads * self.head_dim
    )

    # Apply output projection
    output = self.o_proj(attn_output)

    return output


class SiglipMLP(nn.Module):
  """
  Siglip MLP module.
  
  
  The standard GELU is defined as:
  \text{GELU}(x) = x \cdot \Phi(x)

  The GELU approximation is defined as:
  \Phi(x) \approx \frac{1}{2} \left[ 1 + \tanh \left( \sqrt{\frac{2}{\pi}} \left( x + 0.044715 x^3 \right) \right) \right]
  """
  def __init__(self, hidden_size, intermediate_size):
    super().__init__()
    self.fc1 = nn.Linear(hidden_size, intermediate_size)
    self.fc2 = nn.Linear(intermediate_size, hidden_size)

  def gelu_tanh(self, x):
    # (--- Apply the GELU approximation function ---)  https://arxiv.org/pdf/1606.08415
    return (
        0.5
        * x
        * (
            1
            + torch.tanh(
                torch.sqrt(torch.tensor(2.0 / torch.pi, device=x.device))
                * (x + 0.044715 * torch.pow(x, 3))
            )
        )
    )

  def forward(self, x):
    x = self.fc1(x)
    x = self.gelu_tanh(x)
    x = self.fc2(x)
    return x


class SiglipEncoderBlock(nn.Module):
  """Encoder block (Transformer layer) for siglip vision model."""

  def __init__(self, config: SiglipVisionModelConfig):
    super().__init__()
    self.self_attn = SiglipAttention(
        config.embedding_dim, config.num_attention_heads, config.head_dim
    )
    # SigLiPFromPatches_0/siglip_encoder/Transformer/encoderblock_0/LayerNorm_0
    self.layer_norm1 = nn.LayerNorm(config.embedding_dim, eps=config.layer_norm_eps)
    self.mlp = SiglipMLP(config.embedding_dim, config.intermediate_size)
    # SigLiPFromPatches_0/siglip_encoder/Transformer/encoderblock_0/LayerNorm_1
    self.layer_norm2 = nn.LayerNorm(config.embedding_dim, eps=config.layer_norm_eps)

  def forward(self, x):
    # Pre-LN
    residual = x
    # SigLiPFromPatches_0/siglip_encoder/Transformer/encoderblock_0/LayerNorm_0
    x = self.layer_norm1(x)
    x = self.self_attn(x)
    x = x + residual  # Residual connection *after* LayerNorm

    residual = x
    # SigLiPFromPatches_0/siglip_encoder/Transformer/encoderblock_0/LayerNorm_1
    x = self.layer_norm2(x)
    x = self.mlp(x)
    x = x + residual  # Residual connection *after* LayerNorm
    return x


# https://developers.googleblog.com/en/gemma-explained-paligemma-architecture/
class SiglipVisionModel(nn.Module):
  """Signlip vision model for gemma 3 and paligemma."""

  def __init__(self, config: SiglipVisionModelConfig):
    super().__init__()

    # SigLiPFromPatches_0/siglip_encoder/embedding
    self.patch_embedding = nn.Conv2d(
        in_channels=config.input_channels, # 3
        out_channels=config.embedding_dim, # 1152
        kernel_size=config.conv2d_patch_size, # 14
        stride=config.conv2d_patch_size, # 14
        padding=0,
        bias=config.embedding_use_bias, # True
    )
    self.num_patches = (config.image_size // config.conv2d_patch_size) ** 2 # (896//14)**2 = 4096
    self.num_positions = self.num_patches
    # SigLiPFromPatches_0/siglip_encoder
    self.position_embedding = nn.Embedding(
        self.num_positions, config.embedding_dim # 4096, 1152
    )

    # position_ids = einops.repeat(torch.arange(self.num_positions), 'num_positions -> 1 num_positions') # (1, 4096)
    self.register_buffer("position_ids", torch.arange(self.num_positions).expand((1, -1)), persistent=False)

    # SigLiPFromPatches_0/siglip_encoder/Transformer/encoderblock_i
    self.encoder_blocks = nn.ModuleList(
        SiglipEncoderBlock(config=config)
        for _ in range(config.num_hidden_layers)
    )
    # SigLiPFromPatches_0/siglip_encoder/Transformer/encoder_norm
    self.final_norm = nn.LayerNorm(config.embedding_dim, config.layer_norm_eps)
    self.avg_pool = AveragePool2D(config)
    self.config = config

  # pixel_values=Bxconfig.input_channels x config.image_size x config.image_size
  @torch.inference_mode
  def forward(
      self,
      pixel_values: torch.Tensor, # (B, C, H, W)
  ) -> torch.Tensor:
    # (--- 1. Embed the image according to SiplipVisionEmbeddings ---)
    # (batch_size, channels, height, width) -> (batch_size, embedding_dim, height//14, width//14)
    # (B, 3, 896, 896) -> (B, 1152, 64, 64)
    x = self.patch_embedding(pixel_values)

    # (--- 2. Flatten the image and transpose the dimensions ---)
    # (batch_size, embedding_dim, height//14, width//14) -> (batch_size, embedding_dim, num_patches)
    # (B, 1152, 64, 64) -> (B, 4096, 1152)
    x = x.flatten(2).transpose(1, 2)

    # (--- 3. Add the position embeddings to the image embeddings ---)
    position_ids = self.position_ids.to(pixel_values.device) # Move to the same device as input `pixel_values`
    # (1, num_patches) -> (1, num_patches, embedding_dim)
    # (1, 4096) -> (1, 4096, 1152)
    x = x + self.position_embedding(position_ids) # [batch_size, 4096, 1152] + [1, 4096, 1152] (broadcasting) -> [batch_size, 4096, 1152]

    # (--- 4. Pass the image embeddings to the SiglipEncoderBlocks ---)
    # (batch_size, num_patches, embedding_dim) -> (batch_size, num_patches, embedding_dim)
    # (B, 4096, 1152) -> (B, 4096, 1152)
    for block in self.encoder_blocks:
      x = block(x)

    # (--- 5. Apply the final normalization ---)
    # (batch_size, num_patches, embedding_dim) -> (batch_size, num_patches, embedding_dim)
    # (B, 4096, 1152) -> (B, 4096, 1152)
    x = self.final_norm(x)

    # (--- 6. Apply the average pooling ---)
    # (batch_size, num_patches, embedding_dim) -> (batch_size, num_patches//16, embedding_dim)
    # (B, 4096, 1152) -> (B, 256, 1152)
    return self.avg_pool(x)
