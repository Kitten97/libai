# coding=utf-8
# Copyright 2021 The OneFlow Authors. All rights reserved.
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

import oneflow as flow
from oneflow import nn

from libai.config import configurable
from libai.layers import (
    VocabEmbedding,
    Embedding,
    LayerNorm,
    Linear,
    TransformerLayer,
    ParallelCrossEntropyLoss,
    LMLogits,
    CasualMask,
)
from libai.utils import distributed as dist

from .build import MODEL_ARCH_REGISTRY
from .utils import init_method_normal, scaled_init_method_normal


class GPTEmbedding(nn.Module):
    def __init__(
        self, 
        vocab_size, 
        hidden_size, 
        max_seq_length, 
        embedding_dropout_prob=0.,
        init_method=nn.init.xavier_normal_, 
    ):
        super().__init__()
        self.token_embeddings = VocabEmbedding(vocab_size, hidden_size, init_method=init_method)
        self.position_embeddings = Embedding(max_seq_length, hidden_size, init_method=init_method)
        self.dropout = nn.Dropout(embedding_dropout_prob)

        self.position_ids = flow.arange(
            max_seq_length,
            dtype=flow.long,
            sbp=dist.get_nd_sbp([flow.sbp.broadcast, flow.sbp.broadcast]),
            placement=dist.get_layer_placement(0),
        ).unsqueeze(0)
    
    def forward(self, input_ids, past_length=0):
        bsz, seq_length = input_ids.size()

        position_ids = self.position_ids[:, past_length: past_length + seq_length]
        position_ids = position_ids.expand_as(input_ids).to_global(sbp=input_ids.sbp)

        token_embeds = self.token_embeddings(input_ids)
        position_embeds = self.position_embeddings(position_ids)
        input_embeds = token_embeds + position_embeds
        input_embeds = self.dropout(input_embeds)
        return input_embeds
    
    def word_embeddings(self):
        return self.token_embeddings.weight


class Transformer(nn.Module):
    def __init__(
        self, 
        num_layers, 
        hidden_size, 
        intermediate_size, 
        num_attention_heads, 
        attention_dropout_prob=0., 
        output_dropout_prob=0., 
        layernorm_epsilon=1e-5, 
        init_method=nn.init.xavier_normal_,
        output_layer_init_method=None,
        bias_gelu_fusion=False,
        bias_dropout_fusion=False,
        scale_mask_softmax_fusion=False,
        apply_query_key_layer_scaling=False
    ):
        super().__init__()
        self.num_layers = num_layers
     
        def build_layer(layer_number):
            return TransformerLayer(
                hidden_size,
                intermediate_size,
                num_attention_heads,
                attention_dropout_prob=attention_dropout_prob,
                output_dropout_prob=output_dropout_prob,
                layernorm_epsilon=layernorm_epsilon,
                init_method=init_method,
                output_layer_init_method=output_layer_init_method,
                bias_gelu_fusion=bias_gelu_fusion,
                bias_dropout_fusion=bias_dropout_fusion,
                scale_mask_softmax_fusion=scale_mask_softmax_fusion,
                apply_query_key_layer_scaling=apply_query_key_layer_scaling,
                layer_idx=layer_number,
            )

        self.layers = nn.ModuleList(
            [build_layer(i) for i in range(self.num_layers)]
        )
        self.layernorm_f = LayerNorm(hidden_size, eps=layernorm_epsilon, layer_idx=-1)


    def forward(self, hidden_states, attention_mask, past_key_values=None, use_cache=False):
        # hidden_states shape: (batch_size, seq_length, hidden_size)
        # sbp: [S(0), B]
        presents = []
        if past_key_values is None:
            past_key_values = tuple([None] * self.num_layers)

        for i, (layer, past) in enumerate(zip(self.layers, past_key_values)):
            if self.training:
                hidden_states = layer(
                    hidden_states, 
                    attention_mask=attention_mask
                )     
            else:
                hidden_states = layer(
                    hidden_states, 
                    attention_mask=attention_mask, 
                    past_key_value=past, 
                    use_cache=use_cache
                )
                if use_cache:
                    hidden_states, present = hidden_states
                    presents.append(present)

        output = self.layernorm_f(hidden_states)    
        if use_cache:
            output = (output, presents)
        
        return output


@MODEL_ARCH_REGISTRY.register()
class GPT2Model(nn.Module):
    """GPT-2 language model. The output of the forward method is logits.
    
    Arguments:
        num_layers: number of layers.
        vocab_size: size of vocabulary.
        hidden_size: size of hidden state.
        num_attention_heads: number of attention heads.
        max_seq_length: maximum size of sequence, which is used for positional embedding.
        embedding_dropout_prob: dropout probability of embedding.
        attention_dropout_prob: dropout probability of attention weights.
        output_dropout_prob: dropout probability of output.
        layernorm_epsilon: epsilon used in layernorm.
        apply_query_key_layer_scaling: if `true`, scaling the attention score by layer index.
        bias_gelu_fusion: whether fuse add bias and gelu.
        bias_dropout_fusion: whether fuse add bias and dropout.
    """

    @configurable
    def __init__(
        self, 
        num_layers, 
        vocab_size, 
        hidden_size, 
        intermediate_size, 
        num_attention_heads, 
        max_seq_length=1024, 
        embedding_dropout_prob=0., 
        attention_dropout_prob=0., 
        output_dropout_prob=0., 
        layernorm_epsilon=1e-6, 
        initializer_range=0.02,
        bias_gelu_fusion=False, 
        bias_dropout_fusion=False,
        scale_mask_softmax_fusion=False,
        apply_query_key_layer_scaling=False,
    ):
        super().__init__()
        init_method = init_method_normal(std=initializer_range)
        output_layer_init_method = scaled_init_method_normal(initializer_range, num_layers)

        self.embeddings = GPTEmbedding(
            vocab_size, 
            hidden_size, 
            max_seq_length, 
            embedding_dropout_prob=embedding_dropout_prob,
            init_method=init_method, 
        )
        
        self.casual_mask = CasualMask()

        self.transformer = Transformer(
            num_layers, 
            hidden_size, 
            intermediate_size,
            num_attention_heads, 
            attention_dropout_prob=attention_dropout_prob,
            output_dropout_prob=output_dropout_prob,
            layernorm_epsilon=layernorm_epsilon,
            init_method=init_method,
            output_layer_init_method=output_layer_init_method,
            bias_gelu_fusion=bias_gelu_fusion,
            bias_dropout_fusion=bias_dropout_fusion,
            scale_mask_softmax_fusion=scale_mask_softmax_fusion,
            apply_query_key_layer_scaling=apply_query_key_layer_scaling,
        )

        self.lm_head = LMLogits(vocab_size, bias=True)
        
        self.loss_func = ParallelCrossEntropyLoss()

    @classmethod
    def from_config(cls, cfg):
        return {
            "num_layers": cfg.num_layers,
            "vocab_size": cfg.vocab_size,
            "hidden_size": cfg.hidden_size, 
            "intermediate_size": cfg.intermediate_size,
            "num_attention_heads": cfg.num_attention_heads,
            "max_seq_length": cfg.max_position_embeddings,
            "embedding_dropout_prob": cfg.embedding_dropout_prob,
            "attention_dropout_prob": cfg.attention_probs_dropout_prob,
            "output_dropout_prob": cfg.hidden_dropout_prob,
            "layernorm_epsilon": cfg.layernorm_epsilon,
            "initializer_range": cfg.initializer_range,
            "bias_gelu_fusion": cfg.bias_gelu_fusion,
            "bias_dropout_fusion": cfg.bias_dropout_fusion,
            "scale_mask_softmax_fusion": cfg.scale_mask_softmax_fusion,
            "apply_query_key_layer_scaling": cfg.apply_query_key_layer_scaling,
        }

    def forward(self, input_ids, label_ids=None, past_key_values=None, use_cache=False):
        input_ids_shape = input_ids.size()
        past_length = past_key_values[0].size(2) if past_key_values is not None else 0

        input_embeds = self.embeddings(input_ids, past_length)
        attention_mask = self.casual_mask(input_ids, past_length=past_length)

        transformer_output = self.transformer(
            input_embeds, 
            attention_mask=attention_mask, 
            past_key_values=past_key_values, 
            use_cache=use_cache
        )

        if use_cache:
            transformer_output, presents = transformer_output
        
        output = self.lm_head(transformer_output, self.embeddings.word_embeddings)

        if use_cache:
            output = (output, presents)
        
        if label_ids is not None:
            logits = output[0]
            loss = self.loss_func(logits, label_ids)
            return {"loss": loss}

        return output


@MODEL_ARCH_REGISTRY.register()
class GPT2ForPretraining(nn.Module):
    def __init__(self, cfg) -> None:
        super().__init__()
        self.gpt_model = GPT2Model(cfg)
        self.loss_func = ParallelCrossEntropyLoss()

    def forward(
        self, 
        input_ids, 
        label_ids=None,
        past_key_values=None, 
        use_cache=False,
    ):
        outputs = self.gpt_model(input_ids, past_key_values=past_key_values, use_cache=use_cache)
        
        if label_ids is not None:
            logits = outputs[0]
            loss = self.loss_func(logits, label_ids)
            return {"loss": loss}
        
        return outputs

    @staticmethod
    def set_pipeline_stage_id(model):
        dist_utils = dist.get_dist_util()

        # Set pipeline parallelism stage_id
        for module_block in model.modules():
            # module.origin can get the original module
            if isinstance(module_block.origin, GPTEmbedding):
                module_block.config.stage_id = dist_utils.get_layer_stage_id(0)
            elif isinstance(module_block.origin, CasualMask):
                module_block.config.stage_id = dist_utils.get_layer_stage_id(0)
            elif isinstance(module_block.origin, TransformerLayer):
                module_block.config.stage_id = dist_utils.get_layer_stage_id(module_block.layer_idx)
            elif isinstance(module_block.origin, LMLogits):
                module_block.config.stage_id = dist_utils.get_layer_stage_id(-1)
            elif isinstance(module_block.origin, ParallelCrossEntropyLoss):
                module_block.config.stage_id = dist_utils.get_layer_stage_id(-1)

        model.gpt_model.transformer.layernorm_f.config.stage_id = dist_utils.get_layer_stage_id(-1)
    