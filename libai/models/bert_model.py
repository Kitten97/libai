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
from libai.layers import (
    ActivationCheckpointing,
    build_activation,
    VocabEmbedding,
    Embedding,
    LayerNorm,
    Linear,
    TransformerLayer,
    ParallelCrossEntropyLossWithMask,
)
from libai.utils import distributed as dist
from oneflow import nn
from libai.config import configurable

from .graph_base import GraphBase
from .utils import init_method_normal, scaled_init_method_normal


class BertExtendedAttnMask(nn.Module):
    def forward(self, attention_mask):
        # We create a 3D attention mask from a 2D tensor mask.
        # [b, 1, s]
        attention_mask_b1s = attention_mask.unsqueeze(1)
        # [b, s, 1]
        attention_mask_bs1 = attention_mask.unsqueeze(2)
        # [b, s, s]
        attention_mask_bss = attention_mask_b1s * attention_mask_bs1
        # [b, 1, s, s]
        extended_attention_mask = attention_mask_bss.unsqueeze(1)

        # Convert attention mask to binary.
        extended_attention_mask = flow.le(extended_attention_mask, 0.5)
        # NOTE(Lxy): '<' is not work!
        # extended_attention_mask = (extended_attention_mask < 0.5)

        return extended_attention_mask


class BertEmbeddings(nn.Module):
    def __init__(
        self,
        vocab_size,
        hidden_size,
        max_sequence_length,
        embedding_dropout_prob,
        num_tokentypes=0,
        init_method=nn.init.xavier_normal_,
    ):
        super().__init__()
        self.vocab_embeddings = VocabEmbedding(
            vocab_size, hidden_size, init_method=init_method
        )
        self.position_embeddings = Embedding(
            max_sequence_length, hidden_size, init_method=init_method
        )

        # NOTE(l1aoxingyu): Set position_ids sbp sign to [B, B] initially, because position_ids is a
        # 1D-tensor from 0 to seq_length, if set to [S(0), B] at first, then position_ids
        # will split at the first dim of hierarchy.
        self.position_ids = flow.arange(
            max_sequence_length,
            dtype=flow.long,
            sbp=dist.get_nd_sbp([flow.sbp.broadcast, flow.sbp.broadcast]),
            placement=dist.get_layer_placement(0),
        ).unsqueeze(0)

        if num_tokentypes > 0:
            self.tokentype_embeddings = Embedding(
                num_tokentypes, hidden_size, init_method=init_method
            )
            self.tokentype_ids = flow.zeros(
                self.position_ids.size(),
                dtype=flow.long,
                sbp=self.position_ids.sbp,
                placement=self.position_ids.placement,
            )
        else:
            self.tokentype_embeddings = None

        self.embedding_dropout = nn.Dropout(embedding_dropout_prob)

    def forward(self, input_ids, tokentype_ids=None, position_ids=None):
        seq_length = input_ids.size()[1]

        word_embeddings = self.vocab_embeddings(input_ids)
        if position_ids is None:
            # Change position_ids sbp sign: [B, B] -> [S(0), B]
            position_ids = (
                self.position_ids[:, :seq_length]
                .expand_as(input_ids)
                .to_consistent(sbp=input_ids.sbp)
            )
        position_embeddings = self.position_embeddings(position_ids)
        embeddings = word_embeddings + position_embeddings

        if self.tokentype_embeddings is not None:
            if tokentype_ids is None:
                tokentype_ids = (
                    self.tokentype_ids[:, :seq_length]
                    .expand_as(input_ids)
                    .to_consistent(sbp=input_ids.sbp)
                )
            embeddings = embeddings + self.tokentype_embeddings(tokentype_ids)

        embeddings = self.embedding_dropout(embeddings)
        return embeddings

    def word_embeddings(self):
        return self.vocab_embeddings.weight


class BertLMPredictionHead(nn.Module):
    def __init__(self, hidden_size, init_method, bias_gelu_fusion=True):
        super().__init__()
        self.bias_gelu_fusion = bias_gelu_fusion
        self.dense = Linear(
            hidden_size,
            hidden_size,
            bias=True,
            parallel="col",
            init_method=init_method,
            skip_bias_add=True,
            layer_idx=-1,
        )
        if not bias_gelu_fusion:
            self.activation_func = build_activation("gelu")

        self.layernorm = LayerNorm((hidden_size,), layer_idx=-1)

    def forward(self, hidden_states):
        hidden_states = self.dense(hidden_states)
        if self.bias_gelu_fusion:
            hidden_states, bias = hidden_states
            hidden_states = flow._C.fused_bias_add_gelu(
                hidden_states, bias, axis=hidden_states.ndim - 1
            )
        else:
            hidden_states = self.activation_func(hidden_states)

        # NOTE(l1aoxingyu): hidden_states shape is [B, S, H] whose sbp sign: [S(0), S(2)]
        # Change from [S(0), S(2)] -> [S(0), B] because layernorm cannot get inputs with sbp S(2)
        hidden_states = hidden_states.to_consistent(
            sbp=dist.get_nd_sbp([flow.sbp.split(0), flow.sbp.broadcast])
        )
        hidden_states = self.layernorm(hidden_states)
        return hidden_states


class BertPooler(nn.Module):
    """Pooler layer.
    
    Pool hidden states of a specific token (for example start of the
    sequence) and add a linear transformation followed by a tanh.

    Args:
        hidden_size: hidden state feature dimension
    """

    def __init__(self, hidden_size, init_method):
        super().__init__()
        self.dense = Linear(
            hidden_size,
            hidden_size,
            bias=True,
            parallel="col",
            init_method=init_method,
            layer_idx=-1,
        )
        self.activation_func = build_activation("tanh")

    def forward(self, hidden_states, sequence_index=0):
        """Just "pool" the model by simply taking the [CLS] token corresponding to the first token.
        """
        # hidden_states: [bsz, seq_len, hidden_size]
        select_token_tensor = hidden_states[:, sequence_index]
        pooled_output = self.dense(select_token_tensor)
        pooled_output = self.activation_func(pooled_output)
        return pooled_output


class BertPreTrainingHeads(nn.Module):
    def __init__(self, hidden_size, init_method, bias_gelu_fusion=True):
        super().__init__()
        self.predictions = BertLMPredictionHead(
            hidden_size, init_method, bias_gelu_fusion
        )
        self.seq_relationship = Linear(
            hidden_size,
            2,
            bias=True,
            parallel="row",
            init_method=init_method,
            layer_idx=-1,
        )

    def forward(self, sequence_output, pooled_output):
        prediction_scores = self.predictions(sequence_output)
        seq_relationship_score = self.seq_relationship(pooled_output)
        return prediction_scores, seq_relationship_score


class LMLogits(nn.Module):
    def __init__(self, vocab_size):
        super().__init__()

        self.bias = flow.nn.Parameter(
            flow.empty(
                (vocab_size,),
                dtype=flow.float32,
                placement=dist.get_layer_placement(-1),
                sbp=dist.get_nd_sbp([flow.sbp.broadcast, flow.sbp.split(0)]),
            )
        )
        nn.init.zeros_(self.bias)

    def forward(self, input, word_embeddings):
        """LM logits using word embedding weights """
        # input with sbp sign [S(0), B] and word_embeddings with sbp sign [S(0), B]

        # NOTE(l1aoxingyu): This is for pipeline parallelism
        # change word embedding placement from stage(0) to stage(-1)
        w = word_embeddings.to_consistent(placement=input.placement)

        # NOTE(l1aoxingyu): input x embed^T = logits with sbp sign
        # [S(0), B] x [B, S(1)] --> [S(0), S(1)]
        #     ↑          ↑               ↑
        #   input      embed^T         logits
        # Backward pass input.grad = logits.grad x embed with sbp sign
        # [S(0), S(1)] x [B, S(0)] --> [S(0), P]
        #     ↑             ↑               ↑
        #  logits.grad    embed        input.grad
        # When use input.grad as head node for backward pass, need to convert
        # its sbp sign fromm [S(0), P] --> [S(0), B]
        input = input.to_consistent(grad_sbp=input.sbp)

        logits = flow._C.matmul(input, w, transpose_b=True) + self.bias
        return logits


class BertLoss(nn.Module):
    def __init__(self):
        super().__init__()
        self.masked_lm_loss = ParallelCrossEntropyLossWithMask()

    def forward(self, lm_output, lm_labels, loss_mask, binary_logits, ns_labels):
        masked_lm_loss = self.masked_lm_loss(lm_output, lm_labels, loss_mask)
        sop_loss = flow._C.cross_entropy(
            binary_logits, ns_labels, ignore_index=-1, reduction="none"
        ).mean()
        # NOTE(l1aoxingyu): Change lm loss sbp sign [P, P] -> [P, B] to add with sop loss
        # whose sbp sign: [P, B]
        masked_lm_loss = masked_lm_loss.to_consistent(
            sbp=dist.get_nd_sbp([flow.sbp.partial_sum, flow.sbp.broadcast])
        )
        losses = masked_lm_loss + sop_loss
        return losses


class BertEncoder(nn.Module):
    def __init__(
        self,
        hidden_size,
        hidden_layers,
        num_attention_heads,
        intermediate_size,
        hidden_dropout_prob,
        attention_probs_dropout_prob,
        layernorm_eps,
        init_method,
        scaled_init_method,
        bias_gelu_fusion=True,
        bias_dropout_fusion=True,
        scale_mask_softmax_fusion=True,
        apply_query_key_layer_scaling=True,
    ):
        super().__init__()
        self.hidden_size = hidden_size
        self.hidden_layers = hidden_layers
        self.num_attention_heads = num_attention_heads
        self.attention_head_size = int(hidden_size / num_attention_heads)
        self.intermediate_size = intermediate_size
        self.hidden_dropout_prob = hidden_dropout_prob
        self.attention_probs_dropout_prob = attention_probs_dropout_prob
        self.layernorm_eps = layernorm_eps
        self.init_method = init_method
        self.scaled_init_method = scaled_init_method
        self.bias_gelu_fusion = bias_gelu_fusion
        self.bias_dropout_fusion = bias_dropout_fusion
        self.scale_mask_softmax_fusion = scale_mask_softmax_fusion
        self.apply_query_key_layer_scaling = apply_query_key_layer_scaling

        self._build_layer()

        # Final layer norm before output.
        self.final_layernorm = LayerNorm(
            (hidden_size,), eps=layernorm_eps, layer_idx=-1
        )

    def _get_layer(self, layer_idx):
        layer = getattr(self, f"layers_{layer_idx}")
        checkpoint = getattr(self, f"layers_checkpoint_{layer_idx}")
        return layer, checkpoint

    def _build_layer(self):
        for i in range(self.hidden_layers):
            setattr(
                self,
                f"layers_{i}",
                TransformerLayer(
                    self.hidden_size,
                    self.intermediate_size,
                    self.num_attention_heads,
                    attention_dropout_prob=self.hidden_dropout_prob,
                    output_dropout_prob=self.hidden_dropout_prob,
                    layernorm_epsilon=self.layernorm_eps,
                    bias_gelu_fusion=self.bias_gelu_fusion,
                    bias_dropout_fusion=self.bias_dropout_fusion,
                    scale_mask_softmax_fusion=self.scale_mask_softmax_fusion,
                    apply_query_key_layer_scaling=self.apply_query_key_layer_scaling,
                    init_method=self.init_method,
                    output_layer_init_method=self.scaled_init_method,
                    layer_idx=i,
                ),
            )
            setattr(
                self, f"layers_checkpoint_{i}", ActivationCheckpointing(layer_idx=i)
            )

    def forward(self, hidden_states, extended_attention_mask):
        for i in range(self.hidden_layers):
            layer_module, checkpoint = self._get_layer(i)
            hidden_states = layer_module(
                checkpoint(hidden_states), extended_attention_mask
            )

        output = self.final_layernorm(hidden_states)
        return output


class BertModel(nn.Module):
    """Bert language model"""

    @configurable
    def __init__(
        self,
        vocab_size,
        hidden_size,
        hidden_layers,
        num_attention_heads,
        intermediate_size,
        hidden_dropout_prob,
        attention_probs_dropout_prob,
        max_position_embeddings,
        num_tokentypes=2,
        add_pooling_layer=True,
        initializer_range=0.02,
        layernorm_eps=1e-12,
        bias_gelu_fusion=True,
        bias_dropout_fusion=True,
        scale_mask_softmax_fusion=True,
        apply_query_key_layer_scaling=True,
    ):
        super().__init__()
        init_method = init_method_normal(initializer_range)
        scaled_init_method = scaled_init_method_normal(initializer_range, hidden_layers)

        # Embeddings
        self.embeddings = BertEmbeddings(
            vocab_size,
            hidden_size,
            max_position_embeddings,
            hidden_dropout_prob,
            num_tokentypes,
            init_method,
        )

        # Mask generation
        self.extended_attn_mask = BertExtendedAttnMask()

        # Encoders
        self.encoder = BertEncoder(
            hidden_size,
            hidden_layers,
            num_attention_heads,
            intermediate_size,
            hidden_dropout_prob,
            attention_probs_dropout_prob,
            layernorm_eps,
            init_method,
            scaled_init_method,
            bias_gelu_fusion,
            bias_dropout_fusion,
            scale_mask_softmax_fusion,
            apply_query_key_layer_scaling,
        )

        self.pooler = (
            BertPooler(hidden_size, init_method) if add_pooling_layer else None
        )

    @classmethod
    def from_config(cls, cfg):
        return {
            "vocab_size": cfg.vocab_size,
            "hidden_size": cfg.hidden_size,
            "hidden_layers": cfg.hidden_layers,
            "num_attention_heads": cfg.num_attention_heads,
            "intermediate_size": cfg.intermediate_size,
            "hidden_dropout_prob": cfg.hidden_dropout_prob,
            "attention_probs_dropout_prob": cfg.attention_probs_dropout_prob,
            "max_position_embeddings": cfg.max_position_embeddings,
            "num_tokentypes": cfg.num_tokentypes,
            "add_pooling_layer": cfg.add_pooling_layer,
            "initializer_range": cfg.initializer_range,
            "layernorm_eps": cfg.layernorm_eps,
            "bias_gelu_fusion": cfg.bias_gelu_fusion,
            "bias_dropout_fusion": cfg.bias_dropout_fusion,
            "scale_mask_softmax_fusion": cfg.scale_mask_softmax_fusion,
            "apply_query_key_layer_scaling": cfg.apply_query_key_layer_scaling,
        }

    def forward(
        self, input_ids, attention_mask, tokentype_ids=None, pooling_sequence_index=0,
    ):
        extended_attention_mask = self.extended_attn_mask(attention_mask)

        embedding_output = self.embeddings(input_ids, tokentype_ids)
        encoder_output = self.encoder(embedding_output, extended_attention_mask)
        pooled_output = (
            self.pooler(encoder_output, pooling_sequence_index)
            if self.pooler is not None
            else None
        )
        return encoder_output, pooled_output

    def word_embeddings_weight(self):
        return self.embeddings.word_embeddings()


class BertForPreTraining(nn.Module):
    def __init__(self, cfg):
        super().__init__()
        self.bert = BertModel(cfg)
        self.cls = BertPreTrainingHeads(
            cfg.hidden_size,
            init_method_normal(cfg.initializer_range),
            cfg.bias_gelu_fusion,
        )
        self.lm_logits = LMLogits(cfg.vocab_size)
        self.loss_func = BertLoss()

    def forward(
        self,
        input_ids,
        attention_mask,
        tokentype_ids=None,
        ns_labels=None,
        lm_labels=None,
        loss_mask=None,
        pooling_sequence_index=0,
    ):
        outputs = self.bert(
            input_ids, attention_mask, tokentype_ids, pooling_sequence_index
        )
        sequence_output, pooled_output = outputs[:2]

        sequence_output, seq_relationship_score = self.cls(
            sequence_output, pooled_output
        )

        prediction_scores = self.lm_logits(
            sequence_output, self.bert.word_embeddings_weight()
        )

        if lm_labels is not None and ns_labels is not None:
            total_loss = self.loss_func(
                prediction_scores,
                lm_labels,
                loss_mask,
                seq_relationship_score,
                ns_labels,
            )
            return total_loss
        else:
            return prediction_scores, seq_relationship_score


class BertForPretrainingGraph(GraphBase):
    def build(
        self,
        tokens,
        padding_mask,
        tokentype_ids,
        ns_labels=None,
        lm_labels=None,
        loss_mask=None,
    ):

        # Forward pass through the model
        if self.is_eval:
            return self.model(tokens, padding_mask, tokentype_ids)
        else:
            losses = self.model(
                tokens, padding_mask, tokentype_ids, ns_labels, lm_labels, loss_mask
            )

            losses.backward()
            return losses

    def set_activation_checkpoint(self):
        for module_block in self.model.modules():
            if isinstance(module_block.origin, TransformerLayer):
                module_block.config.activation_checkpointing = True

    def set_pipeline_stage_id(self):
        dist_utils = dist.get_dist_util()

        # 设置模型的 stage_id
        for module_block in self.model.modules():
            # module.origin can get the original module
            if isinstance(module_block.origin, BertEmbeddings):
                module_block.config.stage_id = dist_utils.get_layer_stage_id(0)
            elif isinstance(module_block.origin, BertExtendedAttnMask):
                module_block.config.stage_id = dist_utils.get_layer_stage_id(0)
            elif isinstance(module_block.origin, TransformerLayer):
                module_block.config.stage_id = dist_utils.get_layer_stage_id(
                    module_block.layer_idx
                )
            elif isinstance(module_block.origin, BertEncoder):
                # Set the last layernorm stage id
                module_block.config.stage_id = dist_utils.get_layer_stage_id(-1)
            elif isinstance(module_block.origin, BertPreTrainingHeads):
                module_block.config.stage_id = dist_utils.get_layer_stage_id(-1)
            elif isinstance(module_block.origin, LMLogits):
                module_block.config.stage_id = dist_utils.get_layer_stage_id(-1)
            elif isinstance(module_block.origin, BertLoss):
                module_block.config.stage_id = dist_utils.get_layer_stage_id(-1)
            else:
                pass

        self.model.loss_func.config.stage_id = dist_utils.get_layer_stage_id(-1)
