import torch
from torch.nn import BCEWithLogitsLoss, CrossEntropyLoss, MSELoss
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

class ConvNextPipelineMixin(PipelineMixin):
    def parallelize(self):
        """Transform the model into an IPU pipeline"""
        super().parallelize()
        logger.info("---------- Device Allocation -----------")
        # Set embedding pipeline mapping
        logger.info(f"Embedding  --> IPU 0")
        self.convnext.embeddings = poptorch.BeginBlock(self.convnext.embeddings, "Embedding", ipu_id=0)

        #Set encoder pipeline mappings
        encoder_layer_ipu = get_layer_ipu(self.ipu_config.layers_per_ipu)
        for idx, layer in enumerate(self.convnext.encoder.stages):
            ipu_id = encoder_layer_ipu[idx]
            logger.info(f"Encoder stage {idx} --> IPU {ipu_id}")
            self.convnext.encoder.stages[idx] = poptorch.BeginBlock(layer, f"Encoder_stage_{idx}", ipu_id)

        return self


@register(transformers.ConvNextForImageClassification)
class PipelinedConvNextForImageClassification(transformers.ConvNextForImageClassification, ConvNextPipelineMixin):
    def __init__(self, config):
        super().__init__(config)

        self.num_labels = config.num_labels
        self.convnext = ConvNextModel(config)

        # Classifier head
        self.classifier = (
            torch.nn.Linear(config.hidden_sizes[-1], config.num_labels) if config.num_labels > 0 else nn.Identity()
        )

        # Initialize weights and apply final processing
        self.post_init()

        self.classifier.weight.data.mul_(config.head_init_scale)
        self.classifier.bias.data.mul_(config.head_init_scale)

        if config.pretrained_weights_path:
            self.load_weights_from_fb_model(config.pretrained_weights_path)

    def load_weights_from_fb_model(self, fb_model_path, load_classifier=False):
        fb_state_dict = torch.load(fb_model_path)["model"]

        current_state_dict = self.state_dict()
        new_state_dict = {}

        for fb_tensor_name in fb_state_dict.keys():
            hf_tensor_name = fb_to_hf_name(fb_tensor_name)

            if hf_tensor_name and ("head" not in fb_tensor_name or load_classifier):
                print(f"setting {hf_tensor_name} with fb {fb_tensor_name}")
                new_state_dict[hf_tensor_name] = fb_state_dict[fb_tensor_name]
            else:
                print(f"setting {hf_tensor_name} with current {hf_tensor_name}")
                new_state_dict[hf_tensor_name] = current_state_dict[hf_tensor_name]

        self.load_state_dict(new_state_dict)

    def _init_weights(self, module):
        """Initialize the weights"""
        if isinstance(module, (torch.nn.Linear, torch.nn.Conv2d)):
            # Use truncated normal init as in the paper code.
            # print("Init weights with truncated normal distribution")
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

    def forward(self, pixel_values=None, labels=None, output_hidden_states=None, return_dict=False):
        # return super().forward(pixel_values=pixel_values, labels=labels, output_hidden_states=output_hidden_states, return_dict=False)
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        outputs = self.convnext(pixel_values, output_hidden_states=output_hidden_states, return_dict=return_dict)

        pooled_output = outputs.pooler_output if return_dict else outputs[1]

        logits = self.classifier(pooled_output)

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
                loss_fct = CrossEntropyLoss(label_smoothing=self.config.smoothing)
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