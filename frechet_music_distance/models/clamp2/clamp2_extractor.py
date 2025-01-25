from __future__ import annotations
from pathlib import Path
from typing import Any, Iterable

import torch
from accelerate import Accelerator
from numpy.typing import NDArray
from transformers import AutoTokenizer, BertConfig

from ...utils import download_file
from ..feature_extractor import FeatureExtractor
from . import config
from .clamp2_model import CLaMP2Model
from .m3_patchilizer import M3Patchilizer
from ...dataloaders import DataLoader, MIDIasMTFLoader, ABCLoader
from ...dataloaders.utils import get_dataset_ext


class CLaMP2Extractor(FeatureExtractor):

    def __init__(self, verbose: bool = True) -> None:
        super().__init__(verbose)
        self.accelerator = Accelerator()
        self.device = self.accelerator.device
        self.midi_dataloader = MIDIasMTFLoader(verbose=verbose)
        self.abc_dataloader = ABCLoader(verbose=verbose)

        m3_config = BertConfig(
            vocab_size=1,
            hidden_size=config.M3_HIDDEN_SIZE,
            num_hidden_layers=config.PATCH_NUM_LAYERS,
            num_attention_heads=config.M3_HIDDEN_SIZE//64,
            intermediate_size=config.M3_HIDDEN_SIZE*4,
            max_position_embeddings=config.PATCH_LENGTH
        )
        self.model = CLaMP2Model(m3_config, text_model_name=config.TEXT_MODEL_NAME, hidden_size=config.CLAMP2_HIDDEN_SIZE)
        self.model = self.model.to(self.device)
        self.tokenizer = AutoTokenizer.from_pretrained(config.TEXT_MODEL_NAME)
        self.patchilizer = M3Patchilizer()

        self.model.eval()

        try:
            self.checkpoint = torch.load(config.CLAMP2_WEIGHTS_PATH, map_location="cpu", weights_only=True)
        except Exception:
            self._download_checkpoint()
            self.checkpoint = torch.load(config.CLAMP2_WEIGHTS_PATH, map_location="cpu", weights_only=True)

        self.model.load_state_dict(self.checkpoint["model"])

    def _download_checkpoint(self) -> None:
        print(f"Downloading CLaMP2 weights from: {config.CLAMP2_WEIGHTS_URL} into {config.CLAMP2_WEIGHTS_PATH}")
        download_file(config.CLAMP2_WEIGHTS_URL, config.CLAMP2_WEIGHTS_PATH, verbose=self.verbose)

    @torch.no_grad()
    def extract_feature(self, data: str) -> NDArray:

        input_data = self.patchilizer.encode(data, add_special_patches=True)
        input_data = torch.tensor(input_data)
        max_input_length = config.PATCH_LENGTH

        segment_list = []
        for i in range(0, len(input_data), max_input_length):
            segment_list.append(input_data[i:i+max_input_length])
        segment_list[-1] = input_data[-max_input_length:]

        last_hidden_states_list = []

        for input_segment in segment_list:
            input_masks = torch.tensor([1]*input_segment.size(0))
            pad_indices = torch.ones((config.PATCH_LENGTH - input_segment.size(0), config.PATCH_SIZE)).long() * self.patchilizer.pad_token_id
            input_masks = torch.cat((input_masks, torch.zeros(max_input_length - input_segment.size(0))), 0)
            input_segment = torch.cat((input_segment, pad_indices), 0)
            last_hidden_states = self.model.get_music_features(music_inputs=input_segment.unsqueeze(0).to(self.device),
                                                               music_masks=input_masks.unsqueeze(0).to(self.device))
            last_hidden_states_list.append(last_hidden_states)

        full_chunk_cnt = len(input_data) // max_input_length
        remain_chunk_len = len(input_data) % max_input_length
        if remain_chunk_len == 0:
            feature_weights = torch.tensor([max_input_length] * full_chunk_cnt, device=self.device).view(-1, 1)
        else:
            feature_weights = torch.tensor([max_input_length] * full_chunk_cnt + [remain_chunk_len], device=self.device).view(-1, 1)

        last_hidden_states_list = torch.concat(last_hidden_states_list, 0)
        last_hidden_states_list = last_hidden_states_list * feature_weights
        last_hidden_states_list = last_hidden_states_list.sum(dim=0) / feature_weights.sum()

        return last_hidden_states_list.unsqueeze(0).detach().cpu().numpy()

    def extract_features_from_path(self, dataset_path: str | Path) -> NDArray:
        extension = get_dataset_ext(dataset_path)

        if extension in (".mid", ".midi"):
            data = self.midi_dataloader.load_dataset_async(dataset_path)
        elif extension == ".abc":
            data = self.abc_dataloader.load_dataset_async(dataset_path)
        else:
            msg = f"CLAmP 2 supports .mid, .midi and .abc files but got {extension}"
            raise ValueError(msg)

        return super().extract_features(data)

    def extract_feature_from_path(self, filepath: str | Path) -> NDArray:
        extension = Path(filepath).suffix

        if extension in (".mid", ".midi"):
            data = self.midi_dataloader.load_file(filepath)
        elif extension == ".abc":
            data = self.abc_dataloader.load_file(filepath)
        else:
            msg = f"CLAmP 2 supports .mid, .midi and .abc files but got {extension}"
            raise ValueError(msg)

        return super().extract_feature(data)
