import contextlib
import time
from typing import Dict, List, Optional, Tuple, Set, Union

import numpy as np
import torch
import torch.nn as nn

from vllm.config import (DeviceConfig, ModelConfig, ParallelConfig,
                         SchedulerConfig)
from vllm.logger import init_logger
from vllm.model_executor import get_model, InputMetadata, SamplingMetadata
from vllm.sampling_params import SamplingParams, SamplingType
from vllm.sequence import SamplerOutput, SequenceData, SequenceGroupMetadata
from vllm.lora.layers import LoRAMapping
from vllm.lora.request import LoRARequest
from vllm.utils import pin_memory_available

logger = init_logger(__name__)

KVCache = Tuple[torch.Tensor, torch.Tensor]


class NeuronModelRunner:

    def __init__(
        self,
        model_config: ModelConfig,
        parallel_config: ParallelConfig,
        scheduler_config: SchedulerConfig,
        device_config: DeviceConfig,
    ):
        self.model_config = model_config
        self.parallel_config = parallel_config
        self.scheduler_config = scheduler_config

        # model_config can be None in tests/samplers/test_sampler.py.
        # FIXME(woosuk): This is a hack to make the tests work. Refactor this.
        self.sliding_window = (model_config.get_sliding_window()
                               if model_config is not None else None)
        self.device_config = (device_config
                              if device_config is not None else DeviceConfig())
        self.device = self.device_config.device

        self.model = None
        self.lora_manager = None

        self.pin_memory = pin_memory_available()

    def load_model(self) -> None:
        self.model = get_model(self.model_config,
                               self.device_config,
                               parallel_config=self.parallel_config,
                               scheduler_config=self.scheduler_config)

    def _prepare_prompt(
        self,
        seq_group_metadata_list: List[SequenceGroupMetadata],
    ) -> Tuple[torch.Tensor, torch.Tensor, InputMetadata, List[int], List[int],
               List[int], List[int], Set[LoRARequest]]:
        assert len(seq_group_metadata_list) > 0
        input_tokens: List[List[int]] = []
        input_positions: List[List[int]] = []
        input_block_ids: List[int] = []

        prompt_lens: List[int] = []
        context_lens: List[int] = []
        subquery_lens: List[int] = []
        for seq_group_metadata in seq_group_metadata_list:
            assert seq_group_metadata.is_prompt
            seq_ids = list(seq_group_metadata.seq_data.keys())
            assert len(seq_ids) == 1
            seq_id = seq_ids[0]

            seq_data = seq_group_metadata.seq_data[seq_id]
            prompt_tokens = seq_data.get_token_ids()
            prompt_len = len(prompt_tokens)
            prompt_lens.append(prompt_len)
            computed_len = 0
            # actual prompt lens
            context_lens.append(computed_len)
            subquery_lens.append(prompt_len - computed_len)

            input_tokens.append(prompt_tokens)
            # NOTE(woosuk): Here we assume that the first token in the prompt
            # is always the first token in the sequence.
            input_positions.append(
                list(range(computed_len, computed_len + len(prompt_tokens))))

            assert seq_group_metadata.block_tables is not None
            block_table = seq_group_metadata.block_tables[seq_id]
            assert len(block_table) == 1
            input_block_ids.append(block_table[0])

        max_prompt_len = max(subquery_lens)
        assert max_prompt_len > 0
        input_tokens = _make_tensor_with_pad(input_tokens,
                                             max_prompt_len,
                                             pad=0,
                                             dtype=torch.long,
                                             device=self.device)
        input_positions = _make_tensor_with_pad(input_positions,
                                                max_prompt_len,
                                                pad=0,
                                                dtype=torch.long,
                                                device=self.device)
        input_block_ids = torch.tensor(input_block_ids,
                                       dtype=torch.long,
                                       device=self.device)

        return (input_tokens, input_positions, input_block_ids, prompt_lens,
                subquery_lens)

    def _prepare_decode(
        self,
        seq_group_metadata_list: List[SequenceGroupMetadata],
    ) -> Tuple[torch.Tensor, torch.Tensor, InputMetadata, List[int], List[int],
               Set[LoRARequest]]:
        assert len(seq_group_metadata_list) > 0
        input_tokens: List[List[int]] = []
        input_positions: List[List[int]] = []
        input_block_ids: List[int] = []
        context_lens: List[int] = []

        for seq_group_metadata in seq_group_metadata_list:
            assert not seq_group_metadata.is_prompt

            seq_ids = list(seq_group_metadata.seq_data.keys())

            for seq_id in seq_ids:
                seq_data = seq_group_metadata.seq_data[seq_id]
                generation_token = seq_data.get_last_token_id()
                input_tokens.append([generation_token])

                seq_len = seq_data.get_len()
                position = seq_len - 1
                input_positions.append([position])

                context_len = seq_len if self.sliding_window is None else min(
                    seq_len, self.sliding_window)
                context_lens.append(context_len)

                assert seq_group_metadata.block_tables is not None
                block_table = seq_group_metadata.block_tables[seq_id]
                assert len(block_table) == 1
                input_block_ids.append(block_table[0])

        input_tokens = _make_tensor_with_pad(input_tokens,
                                             max_len=1,
                                             pad=0,
                                             dtype=torch.long,
                                             device=self.device)
        input_positions = _make_tensor_with_pad(input_positions,
                                                max_len=1,
                                                pad=0,
                                                dtype=torch.long,
                                                device=self.device)
        context_lens = torch.tensor(context_lens,
                                    dtype=torch.int,
                                    device=self.device)
        input_block_ids = torch.tensor(input_block_ids,
                                        dtype=torch.long,
                                        device=self.device)

        return (input_tokens, input_positions, input_block_ids)

    def _prepare_sample(
        self,
        seq_group_metadata_list: List[SequenceGroupMetadata],
        prompt_lens: List[int],
        subquery_lens: Optional[List[int]],
    ) -> SamplingMetadata:
        seq_groups: List[Tuple[List[int], SamplingParams]] = []
        selected_token_indices: List[int] = []
        generators: List[torch.Generator] = []
        selected_token_start_idx = 0
        categorized_sample_indices = {t: [] for t in SamplingType}
        categorized_sample_indices_start_idx = 0

        max_subquery_len = max(subquery_lens) if subquery_lens else 1
        for i, seq_group_metadata in enumerate(seq_group_metadata_list):
            seq_ids = list(seq_group_metadata.seq_data.keys())
            sampling_params = seq_group_metadata.sampling_params
            seq_groups.append((seq_ids, sampling_params))

            if seq_group_metadata.is_prompt:
                assert len(seq_ids) == 1
                assert subquery_lens is not None
                subquery_len = subquery_lens[i]
                if sampling_params.prompt_logprobs is not None:
                    # NOTE: prompt token positions do not need sample, skip
                    categorized_sample_indices_start_idx += subquery_len - 1

                categorized_sample_indices[
                    sampling_params.sampling_type].append(
                        categorized_sample_indices_start_idx)
                categorized_sample_indices_start_idx += 1

                if sampling_params.prompt_logprobs is not None:
                    selected_token_indices.extend(
                        range(selected_token_start_idx,
                              selected_token_start_idx + subquery_len - 1))
                selected_token_indices.append(selected_token_start_idx +
                                              subquery_len - 1)
                selected_token_start_idx += max_subquery_len

                if sampling_params.seed is not None:
                    seq_group_metadata.state.generator = torch.Generator(
                        device="cuda").manual_seed(sampling_params.seed)
            else:
                num_seqs = len(seq_ids)
                selected_token_indices.extend(
                    range(selected_token_start_idx,
                          selected_token_start_idx + num_seqs))
                selected_token_start_idx += num_seqs

                categorized_sample_indices[
                    sampling_params.sampling_type].extend(
                        range(categorized_sample_indices_start_idx,
                              categorized_sample_indices_start_idx + num_seqs))
                categorized_sample_indices_start_idx += num_seqs

            if sampling_params.seed is not None:
                generators.append(seq_group_metadata.state.generator)

        selected_token_indices = _async_h2d(selected_token_indices,
                                            dtype=torch.long,
                                            target_device=self.device,
                                            pin_memory=self.pin_memory)
        categorized_sample_indices = {
            t: _async_h2d(seq_ids,
                          dtype=torch.int,
                          target_device=self.device,
                          pin_memory=self.pin_memory)
            for t, seq_ids in categorized_sample_indices.items()
        }

        seq_data: Dict[int, SequenceData] = {}
        for seq_group_metadata in seq_group_metadata_list:
            seq_data.update(seq_group_metadata.seq_data)

        sampling_metadata = SamplingMetadata(
            seq_groups=seq_groups,
            seq_data=seq_data,
            prompt_lens=prompt_lens,
            selected_token_indices=selected_token_indices,
            categorized_sample_indices=categorized_sample_indices,
            generators=generators,
        )
        return sampling_metadata

    def prepare_input_tensors(
        self,
        seq_group_metadata_list: Optional[List[SequenceGroupMetadata]],
    ) -> Tuple[torch.Tensor, torch.Tensor, InputMetadata, SamplingMetadata,
               Set[int], LoRAMapping]:
        # NOTE: We assume that all sequences in the group are all prompts or
        # all decodes.
        is_prompt = seq_group_metadata_list[0].is_prompt
        # Prepare input tensors.
        if is_prompt:
            (input_tokens, input_positions, input_block_ids, prompt_lens,
             subquery_lens) = self._prepare_prompt(seq_group_metadata_list)
        else:
            (input_tokens, input_positions,
             input_block_ids) = self._prepare_decode(seq_group_metadata_list)
            prompt_lens = []
            subquery_lens = None
        sampling_metadata = self._prepare_sample(seq_group_metadata_list,
                                                 prompt_lens, subquery_lens)

        return (input_tokens, input_positions, input_block_ids,
                sampling_metadata)

    @torch.inference_mode()
    def execute_model(
        self,
        seq_group_metadata_list: Optional[List[SequenceGroupMetadata]],
    ) -> Optional[SamplerOutput]:
        (input_tokens, input_positions, input_block_ids, sampling_metadata
         ) = self.prepare_input_tensors(seq_group_metadata_list)

        hidden_states = self.model(
            input_ids=input_tokens,
            positions=input_positions,
            input_block_ids=input_block_ids,
        )

        # Sample the next token.
        output = self.model.sample(
            hidden_states=hidden_states,
            sampling_metadata=sampling_metadata,
        )
        return output

    @property
    def vocab_size(self) -> int:
        return self.model_config.get_vocab_size()


def _pad_to_max(x: List[int], max_len: int, pad: int) -> List[int]:
    assert len(x) <= max_len
    return x + [pad] * (max_len - len(x))


def _make_tensor_with_pad(
    x: List[List[int]],
    max_len: int,
    pad: int,
    dtype: torch.dtype,
    device: Optional[Union[str, torch.device]],
) -> torch.Tensor:
    padded_x = [_pad_to_max(x_i, max_len, pad) for x_i in x]
    return torch.tensor(padded_x, dtype=dtype, device=device)


def _async_h2d(
    data: list,
    dtype: torch.dtype,
    target_device: Union[str, torch.device],
    pin_memory: bool,
) -> torch.Tensor:
    t = torch.tensor(data, dtype=dtype, pin_memory=pin_memory, device="cpu")
    return t.to(device=target_device, non_blocking=True)
