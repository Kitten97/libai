from libai.config import LazyCall

from libai.models import T5Model, T5ForPretraining

cfg = dict(
    num_encoder_layers=12,
    num_decoder_layers=12,
    vocab_size=30522,
    hidden_size=768,
    intermediate_size=4096,
    num_attention_heads=12,
    max_position_embeddings=512,
    embedding_dropout_prob=0.1,
    hidden_dropout_prob=0.1,
    attention_dropout_prob=0.1,
    initializer_range=0.02,
    layernorm_epsilon=1e-5,
    bias_gelu_fusion=True,
    bias_dropout_fusion=True,
    scale_mask_softmax_fusion=False,
    apply_query_key_layer_scaling=True,
)

t5_model = LazyCall(T5Model)(cfg=cfg)

pretrain_model = LazyCall(T5ForPretraining)(cfg=cfg)
