
import logging
# basics
import os
import time
from abc import ABC, abstractmethod
from typing import Dict, Tuple, List

import monai
import nibabel as nib
import numpy as np
# dl
import torch
from monai.data import list_data_collate
from monai.inferers import SlidingWindowInferer
from monai.networks.nets import BasicUNet
from monai.transforms import (Compose, EnsureChannelFirstd, Lambdad,
                              LoadImageD, RandGaussianNoised,
                              ScaleIntensityRangePercentilesd, ToTensord)
from path import Path
from torch.utils.data import DataLoader
from tqdm import tqdm

from brainles_aurora.aux import turbo_path
from brainles_aurora.download import download_model_weights
from brainles_aurora.constants import ModalityMode, ModelSelection, FILES_TO_MODE_DICT


LIB_ABSPATH: str = os.path.dirname(os.path.abspath(__file__))

MODEL_WEIGHTS_DIR = os.path.join(LIB_ABSPATH, "model_weights")
if not os.path.exists(MODEL_WEIGHTS_DIR):
    download_model_weights(target_folder=LIB_ABSPATH)


class AuroraInferer(ABC):

    def __init__(self,
                 segmentation_file: str | np.ndarray,
                 t1_file: str | np.ndarray | None = None,
                 t1c_file: str | np.ndarray | None = None,
                 t2_file: str | np.ndarray | None = None,
                 fla_file: str | np.ndarray | None = None,
                 tta: bool = True,
                 sliding_window_batch_size: int = 1,
                 workers: int = 0,
                 threshold: float = 0.5,
                 sliding_window_overlap: float = 0.5,
                 crop_size: Tuple[int, int, int] = (192, 192, 32),
                 model_selection: ModelSelection = ModelSelection.BEST,
                 whole_network_outputs_file: str | None = None,
                 metastasis_network_outputs_file: str | None = None,
                 log_level: int | str = logging.INFO,
                 ) -> None:
        self.segmentation_file = segmentation_file
        self.t1_file = t1_file
        self.t1c_file = t1c_file
        self.t2_file = t2_file
        self.fla_file = fla_file
        self.tta = tta
        self.sliding_window_batch_size = sliding_window_batch_size
        self.workers = workers
        self.threshold = threshold
        self.sliding_window_overlap = sliding_window_overlap
        self.crop_size = crop_size
        self.model_selection = model_selection
        self.whole_network_outputs_file = whole_network_outputs_file
        self.metastasis_network_outputs_file = metastasis_network_outputs_file
        self.log_level = log_level

    def _setup_logger(self):
        logging.basicConfig(

            format='%(asctime)s %(levelname)s: %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
            level=self.log_level,
            encoding='utf-8',
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler('aurora_inferer.log')
            ]
        )

    def _check_files(self):
        # transform inputs to paths
        if self.t1_file is not None:
            self.t1_file = turbo_path(self.t1_file)

        if self.t1c_file is not None:
            self.t1c_file = turbo_path(self.t1c_file)

        if self.t2_file is not None:
            self.t2_file = turbo_path(self.t2_file)

        if self.fla_file is not None:
            self.fla_file = turbo_path(self.fla_file)

        # return  mode based on files
        return self._determine_mode()

    def _determine_mode(self) -> ModalityMode:

        # check which files are given and if they exist
        def _present(file: Path | None) -> bool:
            if file is None:
                return False
            if not os.path.exists(file):
                raise FileNotFoundError(f"File {file} not found")
            return True

        t1, t1c, t2, fla = map(
            _present, [self.t1_file, self.t1c_file, self.t2_file, self.fla_file])

        logging.info(
            f"Received files: t1: {t1} t1c: {t1c} t2: {t2} flair: {fla}"
        )

        # check if files are given in a valid combination that has an existing model implementation
        mode = FILES_TO_MODE_DICT.get((t1, t1c, t2, fla), None)

        if mode is None:
            raise NotImplementedError(
                "No model implemented for this combination of files")

        logging.info(f"Inference mode based on passed files: {mode}")
        return mode

    def _get_data_loader(self) -> torch.utils.data.DataLoader:
        # init transforms
        transforms = [
            LoadImageD(keys=["images"]),
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

        # Add EnsureChannelFirstd for single modality modes
        if self.mode in [ModalityMode.T1_O, ModalityMode.T1C_O, ModalityMode.FLA_O]:
            transforms.insert(1, EnsureChannelFirstd(keys="images"))
        inference_transforms = Compose(transforms)

        # init data dictionary
        data = {}
        if self.t1_file is not None:
            data['t1'] = self.t1_file
        if self.t1c_file is not None:
            data['t1c'] = self.t1c_file
        if self.t2_file is not None:
            data['t2'] = self.t2_file
        if self.fla_file is not None:
            data['fla'] = self.fla_file
        # method returns files in standard order T1 T1C T2 FLAIR
        data['images'] = self._get_not_none_files()

        #  instantiate dataset and dataloader
        infererence_ds = monai.data.Dataset(
            data=[data],
            transform=inference_transforms,
        )

        data_loader = DataLoader(
            infererence_ds,
            batch_size=1,
            num_workers=self.workers,
            collate_fn=list_data_collate,
            shuffle=False,
        )
        return data_loader

    def _get_model(self) -> torch.nn.Module:
        # init model
        model = BasicUNet(
            spatial_dims=3,
            in_channels=len(self._get_not_none_files()),
            out_channels=2,
            features=(32, 32, 64, 128, 256, 32),
            dropout=0.1,
            act="mish",
        )

        model = torch.nn.DataParallel(model)
        model = model.to(self.device)

        # load weights
        weights = os.path.join(
            MODEL_WEIGHTS_DIR,
            self.mode,
            f"{self.mode}_{self.model_selection}.tar",
        )

        if not os.path.exists(weights):
            raise NotImplementedError(
                f"No weights found for model {mode} and selection {model_selection}")

        checkpoint = torch.load(weights, map_location=self.device)
        model.load_state_dict(checkpoint["model_state"])

        return model

    def _get_not_none_files(self) -> List[str | np.ndarray | Path]:
        # returns not None files in standard order T1 T1C T2 FLAIR
        images = [self.t1_file, self.t1c_file, self.t2_file, self.fla_file]
        return list(filter(None, images))

    def _create_nifti_seg(self,
                          reference_file: str | Path,
                          onehot_model_outputs_CHWD):
        # generate segmentation nifti
        activated_outputs = (
            (onehot_model_outputs_CHWD[0][:, :, :,
                                          :].sigmoid()).detach().cpu().numpy()
        )

        binarized_outputs = activated_outputs >= threshold

        binarized_outputs = binarized_outputs.astype(np.uint8)

        whole_metastasis = binarized_outputs[0]
        enhancing_metastasis = binarized_outputs[1]

        final_seg = whole_metastasis.copy()
        final_seg[whole_metastasis == 1] = 1  # edema
        final_seg[enhancing_metastasis == 1] = 2  # enhancing

        # get header and affine from reference
        ref = nib.load(reference_file)

        segmentation_image = nib.Nifti1Image(final_seg, ref.affine, ref.header)
        nib.save(segmentation_image, output_file)

        if whole_network_output_file:
            whole_network_output_file = Path(
                os.path.abspath(whole_network_output_file))

            whole_out = binarized_outputs[0]

            whole_out_image = nib.Nifti1Image(
                whole_out, ref.affine, ref.header)
            nib.save(whole_out_image, whole_network_output_file)

        if enhancing_network_output_file:
            enhancing_network_output_file = Path(
                os.path.abspath(enhancing_network_output_file)
            )

            enhancing_out = binarized_outputs[1]

            enhancing_out_image = nib.Nifti1Image(
                enhancing_out, ref.affine, ref.header)
            nib.save(enhancing_out_image, enhancing_network_output_file)

    def infer(self):
        logging.info("Loading data and model")
        self.data_loader = self._get_data_loader()
        self.model = self._get_model()

        logging.info(f"Running inference on {self.device}")
        return self._infer()

    @abstractmethod
    def _configure_device(self) -> torch.device:
        pass

    @abstractmethod
    def _infer(self):
        pass


class GPUInferer(AuroraInferer):

    def __init__(self,
                 segmentation_file: str | np.ndarray,
                 t1_file: str | np.ndarray | None = None,
                 t1c_file: str | np.ndarray | None = None,
                 t2_file: str | np.ndarray | None = None,
                 fla_file: str | np.ndarray | None = None,
                 cuda_devices: str = "0",
                 tta: bool = True,
                 sliding_window_batch_size: int = 1,
                 workers: int = 0,
                 threshold: float = 0.5,
                 sliding_window_overlap: float = 0.5,
                 crop_size: Tuple[int, int, int] = (192, 192, 32),
                 model_selection: ModelSelection = ModelSelection.BEST,
                 whole_network_outputs_file: str | None = None,
                 metastasis_network_outputs_file: str | None = None,
                 log_level: int | str = logging.INFO,
                 ) -> None:
        super().__init__(
            segmentation_file=segmentation_file,
            t1_file=t1_file,
            t1c_file=t1c_file,
            t2_file=t2_file,
            fla_file=fla_file,
            tta=tta,
            sliding_window_batch_size=sliding_window_batch_size,
            workers=workers,
            threshold=threshold,
            sliding_window_overlap=sliding_window_overlap,
            crop_size=crop_size,
            model_selection=model_selection,
            whole_network_outputs_file=whole_network_outputs_file,
            metastasis_network_outputs_file=metastasis_network_outputs_file,
            log_level=log_level,
        )
        # GPUInferer specific variables
        self.cuda_devices = cuda_devices

        # setup
        self._setup_logger()
        self.mode = self._check_files()
        self.device = self._configure_device()

    def _infer(self):
        inferer = SlidingWindowInferer(
            roi_size=self.crop_size,  # = patch_size
            sw_batch_size=self.sliding_window_batch_size,
            sw_device=self.device,
            device=self.device,
            overlap=self.sliding_window_overlap,
            mode="gaussian",
            padding_mode="replicate",
        )

        with torch.no_grad():
            self.model.eval()
            # loop through batches
            for data in tqdm(self.data_loader, 0):
                inputs = data["images"]

                outputs = inferer(inputs, self.model)
                if self.tta:
                    outputs = _apply_test_time_augmentations(
                        data, inferer, self.model
                    )

                # generate segmentation nifti
                try:
                    reference_file = data["t1c"][0]
                except:
                    try:
                        reference_file = data["fla"][0]
                    except:
                        reference_file = data["t1"][0]
                    else:
                        FileNotFoundError("no reference file found!")

                self._create_nifti_seg(
                    reference_file=reference_file,
                    onehot_model_outputs_CHWD=outputs,
                )

    def _configure_device(self) -> torch.device:

        os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        os.environ["CUDA_VISIBLE_DEVICES"] = self.cuda_devices

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        assert device == "cuda", "No cuda device available while using GPUInferer"

        logging.info(f"Using device: {device}")

        # clean memory
        torch.cuda.empty_cache()
        return device


class CPUInferer(AuroraInferer):

    def __init__(self,
                 segmentation_file: str | np.ndarray,
                 t1_file: str | np.ndarray | None = None,
                 t1c_file: str | np.ndarray | None = None,
                 t2_file: str | np.ndarray | None = None,
                 fla_file: str | np.ndarray | None = None,
                 tta: bool = True,
                 sliding_window_batch_size: int = 1,
                 workers: int = 0,
                 threshold: float = 0.5,
                 sliding_window_overlap: float = 0.5,
                 crop_size: Tuple[int, int, int] = (192, 192, 32),
                 model_selection: ModelSelection = ModelSelection.BEST,
                 whole_network_outputs_file: str | None = None,
                 metastasis_network_outputs_file: str | None = None,
                 log_level: int | str = logging.INFO,
                 ) -> None:
        super().__init__(
            segmentation_file=segmentation_file,
            t1_file=t1_file,
            t1c_file=t1c_file,
            t2_file=t2_file,
            fla_file=fla_file,
            tta=tta,
            sliding_window_batch_size=sliding_window_batch_size,
            workers=workers,
            threshold=threshold,
            sliding_window_overlap=sliding_window_overlap,
            crop_size=crop_size,
            model_selection=model_selection,
            whole_network_outputs_file=whole_network_outputs_file,
            metastasis_network_outputs_file=metastasis_network_outputs_file,
            log_level=log_level,
        )

        # setup
        self._setup_logger()
        self.mode = self._check_files()
        self.device = self._configure_device()

    def _infer(self):
        inferer = SlidingWindowInferer(
            roi_size=self.crop_size,  # = patch_size
            sw_batch_size=self.sliding_window_batch_size,
            sw_device=self.device,
            device=self.device,
            overlap=self.sliding_window_overlap,
            mode="gaussian",
            padding_mode="replicate",
        )

        with torch.no_grad():
            self.model.eval()
            # loop through batches
            for data in tqdm(self.data_loader, 0):
                inputs = data["images"]

                outputs = inferer(inputs, self.model)
                if self.tta:
                    outputs = _apply_test_time_augmentations(
                        data, inferer, self.model
                    )

                # generate segmentation nifti
                try:
                    reference_file = data["t1c"][0]
                except:
                    try:
                        reference_file = data["fla"][0]
                    except:
                        reference_file = data["t1"][0]
                    else:
                        FileNotFoundError("no reference file found!")

                self._create_nifti_seg(
                    reference_file=reference_file,
                    onehot_model_outputs_CHWD=outputs,
                )

    def _configure_device(self) -> torch.device:
        device = torch.device("cpu")
        logging.info(f"Using device: {device}")

        return device
