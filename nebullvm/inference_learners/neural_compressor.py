import logging
import pickle
from abc import ABC
from pathlib import Path
from typing import Union, Tuple, Dict, Type

from nebullvm.base import DeepLearningFramework, ModelParams
from nebullvm.inference_learners.base import (
    BaseInferenceLearner,
    LearnerMetadata,
    PytorchBaseInferenceLearner,
)
from nebullvm.optional_modules.neural_compressor import (
    cfgs_to_fx_cfgs,
    cfg_to_qconfig,
)
from nebullvm.optional_modules.torch import (
    torch,
    prepare_fx,
    convert_fx,
    Module,
    GraphModule,
)
from nebullvm.transformations.base import MultiStageTransformation
from nebullvm.utils.torch import save_with_torch_fx, load_with_torch_fx

logger = logging.getLogger("nebullvm_logger")


class NeuralCompressorInferenceLearner(BaseInferenceLearner, ABC):
    """Model optimized on CPU using IntelNeuralCompressor.

    Attributes:
        network_parameters (ModelParams): The model parameters as batch
                size, input and output sizes.
        model (torch.fx.GraphModule): Torch fx graph model.
    """

    def __init__(
        self,
        model: Union[Module, GraphModule],
        model_quant: GraphModule,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.model = model
        self.model_quant = model_quant

    def get_size(self):
        return len(pickle.dumps(self.model_quant, -1)) + len(
            pickle.dumps(self.model, -1)
        )

    def save(self, path: Union[str, Path], **kwargs):
        """Save the model.

        Args:
            path (Path or str): Path to the directory where the model will
                be stored.
            kwargs (Dict): Dictionary of key-value pairs that will be saved in
                the model metadata file.
        """
        metadata = LearnerMetadata.from_model(self, **kwargs)
        metadata.save(path)

        path_orig_model = Path(path) / Path("model_orig")
        path_quant_model = Path(path) / Path("model_quant")

        save_with_torch_fx(self.model, path_orig_model)
        self.model_quant.save(str(path_quant_model))

    @classmethod
    def load(cls, path: Union[Path, str], **kwargs):
        """Load the model.

        Args:
            path (Path or str): Path to the directory where the model is
                stored.
            kwargs (Dict): Dictionary of additional arguments for consistency
                with other Learners.

        Returns:
            DeepSparseInferenceLearner: The optimized model.
        """
        if len(kwargs) > 0:
            logger.warning(
                f"No extra keywords expected for the load method. "
                f"Got {kwargs}."
            )

        path_orig_model = Path(path) / Path("model_orig")
        path_quant_model = Path(path) / Path("model_quant") / "best_model.pt"

        model = load_with_torch_fx(
            Path(path_orig_model), "state_dict.pt"
        ).eval()
        state_dict = torch.load(path_quant_model)

        tune_cfg = state_dict.pop("best_configure")
        op_cfgs = cfg_to_qconfig(tune_cfg, tune_cfg["approach"])
        fx_op_cfgs = cfgs_to_fx_cfgs(op_cfgs, tune_cfg["approach"])
        prepare_custom_config_dict = None
        convert_custom_config_dict = None

        q_model = prepare_fx(
            model,
            fx_op_cfgs,
            prepare_custom_config_dict=prepare_custom_config_dict,
        )

        q_model = convert_fx(
            q_model, convert_custom_config_dict=convert_custom_config_dict
        )

        q_model.load_state_dict(state_dict)

        metadata = LearnerMetadata.read(path)
        input_tfms = metadata.input_tfms
        if input_tfms is not None:
            input_tfms = MultiStageTransformation.from_dict(
                metadata.input_tfms
            )
        return cls(
            input_tfms=input_tfms,
            network_parameters=ModelParams(**metadata.network_parameters),
            model=model,
            model_quant=q_model,
        )


class PytorchNeuralCompressorInferenceLearner(
    NeuralCompressorInferenceLearner, PytorchBaseInferenceLearner
):
    """Model optimized on CPU using IntelNeuralCompressor.

    Attributes:
        network_parameters (ModelParams): The model parameters as batch
                size, input and output sizes.
        model (torch.fx.GraphModule): Torch fx graph model.
    """

    def run(self, *input_tensors: torch.Tensor) -> Tuple[torch.Tensor]:
        """Predict on the input tensors.

        Note that the input tensors must be on the same batch. If a sequence
        of tensors is given when the model is expecting a single input tensor
        (with batch size >= 1) an error is raised.

        Args:
            input_tensors (Tuple[Tensor]): Input tensors belonging to the same
                batch. The tensors are expected having dimensions
                (batch_size, dim1, dim2, ...).

        Returns:
            Tuple[Tensor]: Output tensors. Note that the output tensors does
                not correspond to the prediction on the input tensors with a
                1 to 1 mapping. In fact the output tensors are produced as the
                multiple-output of the model given a (multi-) tensor input.
        """
        inputs = (t.cpu() for t in input_tensors)
        outputs = self.model_quant(*inputs)
        return outputs


NEURAL_COMPRESSOR_INFERENCE_LEARNERS: Dict[
    DeepLearningFramework, Type[NeuralCompressorInferenceLearner]
] = {DeepLearningFramework.PYTORCH: PytorchNeuralCompressorInferenceLearner}
