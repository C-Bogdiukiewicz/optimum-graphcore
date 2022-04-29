import torch
from torch.nn import BCEWithLogitsLoss, CrossEntropyLoss, MSELoss
import torch.nn as nn
from torch import Tensor
from transformers.models.convnext.modeling_convnext import ConvNextLayer, ConvNextDropPath, ConvNextLayerNorm
from transformers.activations import ACT2FN
import torch.nn.functional as F
import poptorch
import transformers
from transformers.models.convnext import ConvNextModel
from transformers.modeling_outputs import (
    ImageClassifierOutputWithNoAttention,
)
from optimum.utils import logging
from ...modeling_utils import PipelineMixin, get_layer_ipu, recomputation_checkpoint, register
from timm.loss import LabelSmoothingCrossEntropy, SoftTargetCrossEntropy
from .fb_to_hf_map_util import fb_to_hf_name

logger = logging.get_logger(__name__)

def extend_hf_convnext_init(self, config):
    # call transformers.ConvNextForImageClassification.__init__()
    transformers.ConvNextForImageClassification.original_init(self, config)

    if hasattr(config, "head_init_scale") and config.num_labels > 0:
        self.classifier.weight.data.mul_(config.head_init_scale)
        self.classifier.bias.data.mul_(config.head_init_scale)

    if hasattr(config, "pretrained_weights_path") and config.pretrained_weights_path:
        load_weights_from_fb_model(self, config.pretrained_weights_path)

transformers.ConvNextForImageClassification.original_init = transformers.ConvNextForImageClassification.__init__
transformers.ConvNextForImageClassification.__init__ = extend_hf_convnext_init

def load_weights_from_fb_model(model, fb_model_path, load_classifier=False):
    fb_state_dict = torch.load(fb_model_path)["model"]

    current_state_dict = model.state_dict()
    new_state_dict = {}

    for fb_tensor_name in fb_state_dict.keys():
        hf_tensor_name = fb_to_hf_name(fb_tensor_name)

        if hf_tensor_name and ("head" not in fb_tensor_name or load_classifier):
            print(f"setting {hf_tensor_name} with fb {fb_tensor_name}")
            new_state_dict[hf_tensor_name] = fb_state_dict[fb_tensor_name]
        else:
            print(f"setting {hf_tensor_name} with current {hf_tensor_name}")
            new_state_dict[hf_tensor_name] = current_state_dict[hf_tensor_name]

    model.load_state_dict(new_state_dict)

class SerializedLinear(nn.Linear):
    def __init__(self, in_features: int, out_features: int, bias: bool = True,
                 device=None, dtype=None):
        super().__init__(in_features, out_features, bias, device, dtype)
        # import pdb; pdb.set_trace()
        print(f"LINEAR {in_features} {out_features}")
        # largest are final blocks:
        # LINEAR 384 1536
        # LINEAR 1536 384
        # BLOCK - 384
        # LINEAR 384 1536
        # LINEAR 1536 384
        # BLOCK - 768
        # LINEAR 768 3072
        # LINEAR 3072 768
        # BLOCK - 768
        # LINEAR 768 3072
        # LINEAR 3072 768
        # BLOCK - 768
        # LINEAR 768 3072
        # LINEAR 3072 768
        if in_features > 2000 or out_features > 2000:
            self.ser_mode = poptorch.MatMulSerializationMode.ReducingDim
        else:
            self.ser_mode = poptorch.MatMulSerializationMode.Disabled

    def forward(self, input: Tensor) -> Tensor:
        return poptorch.serializedMatMul(input, self.weight.t(), mode=self.ser_mode, factor=2) + self.bias

def replace_block_init(self, config, dim, drop_path=0):
    super(ConvNextLayer, self).__init__()
    print(f"BLOCK - {dim}")
    self.dwconv = nn.Conv2d(dim, dim, kernel_size=7, padding=3, groups=dim)  # depthwise conv
    self.layernorm = ConvNextLayerNorm(dim, eps=1e-6)
    self.pwconv1 = SerializedLinear(dim, 4 * dim)  # pointwise/1x1 convs, implemented with linear layers
    self.act = ACT2FN[config.hidden_act]
    self.pwconv2 = SerializedLinear(4 * dim, dim)
    self.layer_scale_parameter = (
        nn.Parameter(config.layer_scale_init_value * torch.ones((dim)), requires_grad=True)
        if config.layer_scale_init_value > 0
        else None
    )
    self.drop_path = ConvNextDropPath(drop_path) if drop_path > 0.0 else nn.Identity()

ConvNextLayer.__init__ = replace_block_init

class ConvNextPipelineMixin(PipelineMixin):
    def parallelize(self):
        """Transform the model into an IPU pipeline"""
        super().parallelize()
        logger.info("---------- Device Allocation -----------")
        # Set embedding pipeline mapping
        logger.info(f"Embedding  --> IPU 0")
        self.convnext.embeddings = poptorch.BeginBlock(self.convnext.embeddings, "Embedding", ipu_id=0)

        # Set encoder pipeline mappings
        # get the mapping of encoder layers --> IPU
        encoder_layer_ipu = get_layer_ipu(self.ipu_config.layers_per_ipu)
        global_layer_idx = 0
        for stage_nr, stage in enumerate(self.convnext.encoder.stages):
            for stage_layer_idx, layer in enumerate(stage.layers):
                # Set encoder convnext layer mapping
                ipu_id = encoder_layer_ipu[global_layer_idx]
                logger.info(f"Encoder stage {stage_nr}, convnext layer {stage_layer_idx} --> IPU {ipu_id}")
                layer = poptorch.BeginBlock(layer, f"Encoder_stage_{stage_nr}_layer_{stage_layer_idx}", ipu_id=ipu_id)
                global_layer_idx += 1

        return self


@register(transformers.ConvNextForImageClassification)
class PipelinedConvNextForImageClassification(transformers.ConvNextForImageClassification, ConvNextPipelineMixin):
    def _init_weights(self, module):
        """Initialize the weights"""
        if isinstance(module, (torch.nn.Linear, torch.nn.Conv2d)):
            # Use truncated normal init as in the paper code.
            torch.nn.init.trunc_normal_(module.weight.data, std=.02)
            if module.bias is not None:
                module.bias.data.zero_()
        elif isinstance(module, torch.nn.LayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)

    def parallelize(self):
        """Set pipeline mapping for the head (layernorm + classifier layers)"""
        super().parallelize()
        last_ipu = self.ipu_config.ipus_per_replica - 1
        logger.info(f"Head --> IPU {last_ipu}")
        self.convnext.layernorm = poptorch.BeginBlock(self.convnext.layernorm, "LayerNorm", last_ipu)
        self.classifier = poptorch.BeginBlock(self.classifier, "Classifier", last_ipu)

        return self

    @poptorch.autocast()
    def forward(self, pixel_values=None, labels=None, output_hidden_states=None, return_dict=False):
        # return super().forward(pixel_values=pixel_values, labels=labels, output_hidden_states=output_hidden_states, return_dict=False)
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        outputs = self.convnext(pixel_values, output_hidden_states=output_hidden_states, return_dict=return_dict)

        pooled_output = outputs.pooler_output if return_dict else outputs[1]

        logits = self.classifier(pooled_output)

        smoothing = 0.0
        if hasattr(self.config, "smoothing"):
            smoothing = self.config.smoothing

        loss = None
        if labels is not None:
            if self.config.problem_type is None:
                if self.num_labels == 1:
                    self.config.problem_type = "regression"
                elif self.num_labels > 1 and (labels.dtype == torch.long or labels.dtype == torch.int):
                    #
                    self.config.problem_type = "single_label_classification"
                else:
                    # Using mixup
                    self.config.problem_type = "multi_label_classification"

            if self.config.problem_type == "regression":
                loss_fct = MSELoss()
                if self.num_labels == 1:
                    loss = loss_fct(logits.squeeze(), labels.squeeze())
                else:
                    loss = loss_fct(logits, labels)
            elif self.config.problem_type == "single_label_classification":
                loss_fct = CrossEntropyLoss(label_smoothing=smoothing)
                if self.eval:
                    loss_fct = CrossEntropyLoss()
                loss = loss_fct(logits.view(-1, self.num_labels), labels.view(-1))
            elif self.config.problem_type == "multi_label_classification":
                loss_fct = SoftTargetCrossEntropy()
                loss = loss_fct(logits, labels)
                loss = poptorch.identity_loss(loss, 'none')
        if not return_dict:
            output = (logits,) + outputs[2:]
            return ((loss,) + output) if loss is not None else output

        return ImageClassifierOutputWithNoAttention(
            loss=loss,
            logits=logits,
            hidden_states=outputs.hidden_states,
        )
