import logging
from logging import Logger
import os
from abc import ABC, abstractmethod
from pathlib import Path
import sys
from typing import Dict, List
from typing import IO

from datetime import datetime
import monai
import nibabel as nib
import numpy as np
import torch
from monai.data import list_data_collate
from monai.inferers import SlidingWindowInferer
from monai.networks.nets import BasicUNet
from monai.transforms import (
    Compose,
    EnsureChannelFirstd,
    Lambdad,
    LoadImageD,
    RandGaussianNoised,
    ScaleIntensityRangePercentilesd,
    ToTensord,
)
from torch.utils.data import DataLoader
import uuid

from brainles_aurora.inferer.constants import (
    IMGS_TO_MODE_DICT,
    DataMode,
    InferenceMode,
    Output,
)
from brainles_aurora.aux import turbo_path
from brainles_aurora.inferer.dataclasses import AuroraInfererConfig, BaseConfig
from brainles_aurora.inferer.download import download_model_weights


class DualStdErrOutput:
    def __init__(self, stderr: IO, file_handler_stream: IO = None):
        self.stderr = stderr
        self.file_handler_stream = file_handler_stream

    def set_file_handler_stream(self, file_handler_stream: IO):
        self.file_handler_stream = file_handler_stream

    def write(self, text):
        self.stderr.write(text)
        if self.file_handler_stream:
            self.file_handler_stream.write(text)

    def flush(self):
        self.stderr.flush()
        if self.file_handler_stream:
            self.file_handler_stream.flush()


class AbstractInferer(ABC):
    """
    Abstract base class for inference.

    Attributes:
        config (BaseConfig): The configuration for the inferer.
        output_folder (Path): The output folder for the inferer. Follows the schema {config.output_folder}/{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}
    """

    def __init__(self, config: BaseConfig) -> None:
        """Initialize the abstract inferer

        Args:
            config (BaseConfig): Configuration for the inferer.
        """
        self.config = config

        # setup logger
        self.dual_stderr_output = DualStdErrOutput(sys.stderr)
        sys.stderr = self.dual_stderr_output
        self.log = self._setup_logger(log_file=None)

        # download weights if not present
        self.lib_path: str = os.path.dirname(os.path.abspath(__file__))

        self.model_weights_folder = Path(self.lib_path).parent / "model_weights"
        if not self.model_weights_folder.exists():
            download_model_weights(target_folder=self.lib_path)

    def _setup_logger(self, log_file: str | Path | None) -> Logger:
        # Create a logger for each inference run with a unique log file

        default_formatter = logging.Formatter(
            "%(asctime)s %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S"
        )
        logger = logging.getLogger(f"Inferer_{uuid.uuid4()}")
        logger.setLevel(self.config.log_level)  # Set the desired logging level
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(default_formatter)
        logger.addHandler(stream_handler)

        if log_file:
            # Create a file handler. We dont add it to the logger directly, but to the dual_stderr_output
            # This way als console output includign excpetions will be redirceted to the log file

            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            file_handler = logging.FileHandler(log_file)

            self.dual_stderr_output.set_file_handler_stream(file_handler.stream)
            logger.info(f"Logging to: {log_file}")

        return logger

    @abstractmethod
    def infer(self):
        pass


class AuroraInferer(AbstractInferer):
    """Inferer for the CPU Aurora models."""

    def __init__(self, config: AuroraInfererConfig) -> None:
        """Initialize the AuroraInferer.

        Args:
            config (AuroraInfererConfig): Configuration for the Aurora inferer.
        """
        super().__init__(config=config)

        self.log.info(
            f"Initialized {self.__class__.__name__} with config: {self.config}"
        )
        self.device = self._configure_device()

        self.validated_images = None
        self.inference_mode = None

    def _validate_images(
        self,
        t1: str | Path | np.ndarray | None = None,
        t1c: str | Path | np.ndarray | None = None,
        t2: str | Path | np.ndarray | None = None,
        fla: str | Path | np.ndarray | None = None,
    ) -> List[np.ndarray | None] | List[Path | None]:
        """Validate input images, sets the input mode and returns the list of validated images.

        Returns:
            List[np.ndarray | None] | List[Path | None]: List of validated images.
        """

        def _validate_image(
            data: str | Path | np.ndarray | None,
        ) -> np.ndarray | Path | None:
            if data is None:
                return None
            if isinstance(data, np.ndarray):
                self.input_mode = DataMode.NUMPY
                return data.astype(np.float32)
            if not os.path.exists(data):
                raise FileNotFoundError(f"File {data} not found")
            if not (data.endswith(".nii.gz") or data.endswith(".nii")):
                raise ValueError(
                    f"File {data} must be a nifti file with extension .nii or .nii.gz"
                )
            self.input_mode = DataMode.NIFTI_FILE
            return turbo_path(data)

        images = [
            _validate_image(img)
            for img in [
                t1,
                t1c,
                t2,
                fla,
            ]
        ]

        not_none_images = [img for img in images if img is not None]
        assert len(not_none_images) > 0, "No input images provided"
        # make sure all inputs have the same type
        unique_types = set(map(type, not_none_images))
        assert (
            len(unique_types) == 1
        ), f"All passed images must be of the same type! Received {unique_types}. Accepted Input types: {list(DataMode)}"

        self.log.info(
            f"Successfully validated input images. Input mode: {self.input_mode}"
        )
        return images

    def _determine_inference_mode(self, images) -> InferenceMode:
        """Determine the inference mode based on the provided images.

        Raises:
            NotImplementedError: If no model is implemented for the combination of input images.
        Returns:
            InferenceMode: Inference mode based on the combination of input images.
        """
        _t1, _t1c, _t2, _fla = [img is not None for img in images]
        self.log.info(
            f"Received files: T1: {_t1}, T1C: {_t1c}, T2: {_t2}, FLAIR: {_fla}"
        )

        # check if files are given in a valid combination that has an existing model implementation
        mode = IMGS_TO_MODE_DICT.get((_t1, _t1c, _t2, _fla), None)

        if mode is None:
            raise NotImplementedError(
                "No model implemented for this combination of images"
            )

        self.log.info(f"Inference mode: {mode}")
        return mode

    def _get_data_loader(self) -> torch.utils.data.DataLoader:
        """Get the data loader for inference.

        Returns:
            torch.utils.data.DataLoader: Data loader for inference.
        """
        # init transforms
        transforms = [
            LoadImageD(keys=["images"])
            if self.input_mode == DataMode.NIFTI_FILE
            else None,
            EnsureChannelFirstd(keys="images")
            if len(self._get_not_none_files()) == 1
            else None,
            Lambdad(["images"], np.nan_to_num),
            ScaleIntensityRangePercentilesd(
                keys="images",
                lower=0.5,
                upper=99.5,
                b_min=0,
                b_max=1,
                clip=True,
                relative=False,
                channel_wise=True,
            ),
            ToTensord(keys=["images"]),
        ]
        # Filter None transforms
        transforms = list(filter(None, transforms))
        inference_transforms = Compose(transforms)

        # Initialize data dictionary
        data = {
            "images": self._get_not_none_files(),
        }

        # init dataset and dataloader
        infererence_ds = monai.data.Dataset(
            data=[data],
            transform=inference_transforms,
        )

        data_loader = DataLoader(
            infererence_ds,
            batch_size=1,
            num_workers=self.config.workers,
            collate_fn=list_data_collate,
            shuffle=False,
        )
        return data_loader

    def _get_model(self) -> torch.nn.Module:
        """Get the Aurora model based on the inference mode.

        Returns:
            torch.nn.Module: Aurora model.
        """

        # init model
        model = BasicUNet(
            spatial_dims=3,
            in_channels=len(self._get_not_none_files()),
            out_channels=2,
            features=(32, 32, 64, 128, 256, 32),
            dropout=0.1,
            act="mish",
        )

        # load weights
        weights_path = os.path.join(
            self.model_weights_folder,
            self.inference_mode,
            f"{self.config.model_selection}.tar",
        )

        if not os.path.exists(weights_path):
            raise NotImplementedError(
                f"No weights found for model {self.mode} and selection {self.config.model_selection}"
            )

        model = model.to(self.device)
        checkpoint = torch.load(weights_path, map_location=self.device)

        # The models were trained using DataParallel, hence we need to remove the 'module.' prefix
        # for cpu inference to enable checkpoint loading (since DataParallel is not usable for CPU)
        if self.device == torch.device("cpu"):
            if "module." in list(checkpoint["model_state"].keys())[0]:
                checkpoint["model_state"] = {
                    k.replace("module.", ""): v
                    for k, v in checkpoint["model_state"].items()
                }
        else:
            model = torch.nn.parallel.DataParallel(model)

        model.load_state_dict(checkpoint["model_state"])

        return model

    def _apply_test_time_augmentations(
        self, outputs: torch.Tensor, data: Dict, inferer: SlidingWindowInferer
    ) -> torch.Tensor:
        """Apply test time augmentations to the model outputs.

        Args:
            outputs (torch.Tensor): Model outputs.
            data (Dict): Input data.
            inferer (SlidingWindowInferer): Sliding window inferer.

        Returns:
            torch.Tensor: Augmented model outputs.
        """
        n = 1.0
        for _ in range(4):
            # test time augmentations
            _img = RandGaussianNoised(keys="images", prob=1.0, std=0.001)(data)[
                "images"
            ]

            output = inferer(_img, self.model)
            outputs = outputs + output
            n += 1.0
            for dims in [[2], [3]]:
                flip_pred = inferer(torch.flip(_img, dims=dims), self.model)

                output = torch.flip(flip_pred, dims=dims)
                outputs = outputs + output
                n += 1.0
        outputs = outputs / n
        return outputs

    def _get_not_none_files(self) -> List[np.ndarray] | List[Path]:
        """Get the list of non-None input images in  order T1-T1C-T2-FLA.

        Returns:
            List[np.ndarray] | List[Path]: List of non-None images.
        """
        assert self.validated_images is not None, "Images not validated yet"

        return [img for img in self.validated_images if img is not None]

    def _save_as_nifti(self, postproc_data: Dict[str, np.ndarray]) -> None:
        """Save post-processed data as NIFTI files.

        Args:
            postproc_data (Dict[str, np.ndarray]): Post-processed data.
        """
        # determine affine/ header
        if self.input_mode == DataMode.NIFTI_FILE:
            reference_file = self._get_not_none_files()[0]
            ref = nib.load(reference_file)
            affine, header = ref.affine, ref.header
        else:
            self.log.warning(
                f"Writing NIFTI output after NumPy input, using default affine=np.eye(4) and header=None"
            )
            affine, header = np.eye(4), None

        # save niftis
        for key, data in postproc_data.items():
            output_file = self.output_file_mapping[key]
            if output_file:
                output_image = nib.Nifti1Image(data, affine, header)
                os.makedirs(os.path.dirname(output_file), exist_ok=True)
                nib.save(output_image, output_file)
                self.log.info(f"Saved {key} to {output_file}")

    def _post_process(
        self, onehot_model_outputs_CHWD: torch.Tensor
    ) -> Dict[str, np.ndarray]:
        """Post-process the model outputs.

        Args:
            onehot_model_outputs_CHWD (torch.Tensor): One-hot encoded model outputs.

        Returns:
            Dict[str, np.ndarray]: Post-processed data.
        """

        # create segmentations
        activated_outputs = (
            (onehot_model_outputs_CHWD[0][:, :, :, :].sigmoid()).detach().cpu().numpy()
        )
        binarized_outputs = activated_outputs >= self.config.threshold
        binarized_outputs = binarized_outputs.astype(np.uint8)

        whole_metastasis = binarized_outputs[0]
        enhancing_metastasis = binarized_outputs[1]

        final_seg = whole_metastasis.copy()
        final_seg[whole_metastasis == 1] = 1  # edema
        final_seg[enhancing_metastasis == 1] = 2  # enhancing

        whole_out = binarized_outputs[0]
        enhancing_out = binarized_outputs[1]

        # create output dict based on config
        return {
            Output.SEGMENTATION: final_seg,
            Output.WHOLE_NETWORK: whole_out,
            Output.METASTASIS_NETWORK: enhancing_out,
        }

    def _sliding_window_inference(self) -> None | Dict[str, np.ndarray]:
        """Perform sliding window inference using monai.inferers.SlidingWindowInferer.

        Returns:
            None | Dict[str, np.ndarray]: Post-processed data if output_mode is NUMPY, otherwise the data is saved as a niftis and None is returned.
        """
        inferer = SlidingWindowInferer(
            roi_size=self.config.crop_size,  # = patch_size
            sw_batch_size=self.config.sliding_window_batch_size,
            sw_device=self.device,
            device=self.device,
            overlap=self.config.sliding_window_overlap,
            mode="gaussian",
            padding_mode="replicate",
        )

        with torch.no_grad():
            self.model.eval()
            self.model = self.model.to(self.device)
            # loop through batches, only 1 batch!
            for data in self.data_loader:
                inputs = data["images"].to(self.device)

                outputs = inferer(inputs, self.model)
                if self.config.tta:
                    self.log.info("Applying test time augmentations")
                    outputs = self._apply_test_time_augmentations(
                        outputs, data, inferer
                    )

                postprocessed_data = self._post_process(
                    onehot_model_outputs_CHWD=outputs,
                )
                if self.config.output_mode == DataMode.NUMPY:
                    # rm whole/ metastasus network if not requested
                    if not self.config.include_whole_network_in_numpy_output_mode:
                        _ = postprocessed_data.pop(Output.WHOLE_NETWORK)
                    if not self.config.include_metastasis_network_in_numpy_output_mode:
                        _ = postprocessed_data.pop(Output.METASTASIS_NETWORK)
                    return postprocessed_data
                else:
                    self._save_as_nifti(postproc_data=postprocessed_data)
                    return None

    def _configure_device(self) -> torch.device:
        """Configure the device for inference.

        Returns:
            torch.device: Configured device.
        """
        device = torch.device("cpu")
        self.log.info(f"Using device: {device}")
        return device

    def infer(
        self,
        t1: str | Path | np.ndarray | None = None,
        t1c: str | Path | np.ndarray | None = None,
        t2: str | Path | np.ndarray | None = None,
        fla: str | Path | np.ndarray | None = None,
        segmentation_file: str | Path | None = None,
        whole_network_file: str | Path | None = None,
        metastasis_network_file: str | Path | None = None,
    ) -> None:
        """Run the inference process."""
        # setup logger for inference run
        log_path = (
            Path(segmentation_file).with_suffix(".log") if segmentation_file else "."
        )
        self.log = self._setup_logger(
            log_file=log_path,
        )

        self.log.info(f"Running inference on {self.device}")

        # check inputs and get mode , == prev mode => run inference, else load new model
        prev_mode = self.inference_mode
        self.validated_images = self._validate_images(t1=t1, t1c=t1c, t2=t2, fla=fla)
        self.inference_mode = self._determine_inference_mode(
            images=self.validated_images
        )

        if prev_mode != self.inference_mode:
            self.log.info("Loading Model and weights")
            self.model = self._get_model()
        else:
            self.log.info(
                f"Same inference mode {self.inference_mode}. Reusing previouse model"
            )

        self.log.info("Setting up Dataloader")
        self.data_loader = self._get_data_loader()

        # setup output file paths
        if self.config.output_mode == DataMode.NIFTI_FILE:
            # TODO add error handling to ensure file extensions present
            if not segmentation_file:
                default_segmentation_path = "segmentation.nii.gz"
                self.log.warning("No segmentation file name provided, using default")
            self.output_file_mapping = {
                Output.SEGMENTATION: segmentation_file or default_segmentation_path,
                Output.WHOLE_NETWORK: whole_network_file,
                Output.METASTASIS_NETWORK: metastasis_network_file,
            }

        ########
        return self._sliding_window_inference()


####################
# GPU Inferer
####################
class AuroraGPUInferer(AuroraInferer):
    """Inferer for the Aurora models on GPU."""

    def __init__(
        self,
        config: AuroraInfererConfig,
        cuda_devices: str = "0",
    ) -> None:
        """Initialize the AuroraGPUInferer.

        Args:
            config (AuroraInfererConfig): Configuration for the Aurora GPU inferer.
            cuda_devices (str, optional): CUDA devices to use. Defaults to "0".
        """
        self.cuda_devices = cuda_devices

        super().__init__(config=config)

    def _configure_device(self) -> torch.device:
        """Configure the GPU device for inference.

        Returns:
            torch.device: Configured GPU device.
        """
        os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        os.environ["CUDA_VISIBLE_DEVICES"] = self.cuda_devices

        assert (
            torch.cuda.is_available()
        ), "No cuda device available while using GPUInferer"

        device = torch.device("cuda")
        self.log.info(f"Using device: {device}")

        # clean memory
        torch.cuda.empty_cache()
        return device
