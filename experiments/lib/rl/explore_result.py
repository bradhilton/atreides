import asyncio
from collections import Counter
from dataclasses import dataclass, field
import numpy as np
import random
import time
import torch
from tqdm import tqdm
from typing import Callable, Optional

from .completion import Completion
from .episode import Episode
from .pack import DiskPackedTensors, get_mask, PackedTensors, packed_tensors_from_dir
from ..tokenizer import Tokenizer
from ..utils import truncate_pad


@dataclass
class ExploreResult:
    pbar: tqdm
    max_mask_sequence_batch_size: int
    model: str
    sample_probability_power: float
    sequence_length: int
    tensor_dir: str
    tokenizer: Tokenizer
    trajectories_per_episode: Optional[int]
    completion_tensors: dict[Completion, dict[str, torch.Tensor]] = field(
        default_factory=dict
    )
    episodes: list[Episode] = field(default_factory=list)
    exceptions: list[BaseException] = field(default_factory=list)
    sequence: Counter[Completion] = field(default_factory=Counter)
    sequences: list[Counter[Completion]] = field(default_factory=list)
    start: float = field(default_factory=lambda: time.time())

    def add_exception(self, exception: BaseException) -> None:
        self.exceptions.append(exception)
        self._update_pbar_postfix()

    def done_callback(self, task: asyncio.Task[Episode]) -> None:
        try:
            self._pack_episode(task.result())
        except BaseException as exception:
            self.add_exception(exception)

    def completed(self) -> "ExploreResult":
        self.pbar.n = len(self.episodes)
        self.pbar.close()
        packed_tensors = self._write_sequence(force_write_mask=True)
        if packed_tensors is not None:
            self._write_weights(packed_tensors)
            self._normalize_advantages(packed_tensors)
        return self

    def tensors(self) -> PackedTensors:
        return packed_tensors_from_dir(**self.disk_packed_tensors())

    def disk_packed_tensors(self) -> DiskPackedTensors:
        return DiskPackedTensors(
            dir=self.tensor_dir,
            num_sequences=len(self.sequences),
            sequence_length=self.sequence_length,
        )

    def _update_pbar_postfix(self) -> None:
        self.pbar.set_postfix(
            completed=len(self.episodes),
            exceptions=len(self.exceptions),
        )

    def _pack_episode(self, episode: Episode) -> None:
        self.episodes.append(episode)
        self._update_pbar_postfix()
        termini: list[Completion] = []
        possible_termini = episode.completion.leaves(model=self.model)
        if self.trajectories_per_episode is not None:
            possible_termini = list(possible_termini)
            possible_termini = random.choices(
                possible_termini,
                weights=[
                    leaf.sample_weight(
                        cache=True,
                        model=self.model,
                        power=self.sample_probability_power,
                    )
                    for leaf in possible_termini
                ],
                k=self.trajectories_per_episode,
            )
        for terminus in possible_termini:
            for terminus in terminus.ancestors(including_self=True):
                if (
                    terminus.advantage(cache=True, model=self.model) != 0
                    and len(terminus.tokens(self.tokenizer, cache=True))
                    <= self.sequence_length
                ):
                    break
            else:
                continue
            termini.append(terminus)
        for terminus in termini:
            while True:
                for completion in terminus.ancestors(including_self=True, reverse=True):
                    self.sequence[completion] += 1
                    if (
                        sum(
                            c.token_count(self.tokenizer, cache=True)
                            for c in self.sequence
                        )
                        > self.sequence_length
                    ):
                        for c in completion.ancestors(including_self=True):
                            self.sequence[c] -= 1
                        self._write_sequence()
                        break
                else:
                    break

    def _write_sequence(
        self, force_write_mask: bool = False
    ) -> Optional[PackedTensors]:
        if not self.sequence:
            return
        for completion, count in list(self.sequence.items()):
            if count == 0:
                del self.sequence[completion]
        for completion in self.sequence:
            if completion not in self.completion_tensors:
                self.completion_tensors[completion] = self._get_completion_tensors(
                    completion,
                    tokenizer=self.tokenizer,
                )
        num_sequences = 2 ** (
            int(np.log2(max(round(2**20 / self.sequence_length), len(self.sequences))))
            + 1
        )
        packed_tensors = packed_tensors_from_dir(
            dir=self.tensor_dir,
            num_sequences=num_sequences,
            sequence_length=self.sequence_length,
        )
        for key, pad_value in {
            "tokens": self.tokenizer.get_pad_token_id() or 0,
            "advantages": torch.nan,
            "logprobs": torch.nan,
            "input_pos": 0,
        }.items():
            packed_tensors[key][len(self.sequences)] = self._sequence_to_tensor(
                sequence=self.sequence,
                pad_value=pad_value,
                map=lambda completion: self.completion_tensors[completion][key],
            )
        self.sequences.append(self.sequence)
        self.sequence = Counter()
        if (
            force_write_mask
            or len(self.sequences) % self.max_mask_sequence_batch_size == 0
        ):
            self._write_mask(packed_tensors)
        return packed_tensors

    def _write_mask(self, packed_tensors: PackedTensors) -> None:
        start = (
            (len(self.sequences) - 1) // self.max_mask_sequence_batch_size
        ) * self.max_mask_sequence_batch_size
        stop = min(start + self.max_mask_sequence_batch_size, len(self.sequences))
        completions = {
            completion
            for sequence in self.sequences[start:stop]
            for completion in sequence
        }
        max_ancestors = (
            max(
                root.max_depth(self.model)
                for root in {completion.root() for completion in completions}
            )
            + 1
        )
        ancestor_ids: dict[Completion, list[int]] = {}
        for completion in completions:
            ids = [
                id(ancestor) for ancestor in completion.ancestors(including_self=True)
            ]
            ids += [ids[-1]] * (max_ancestors - len(ids))
            ancestor_ids[completion] = ids
        packed_tensors["mask"][start:stop] = get_mask(
            ids=self._sequences_to_tensor(
                sequences=self.sequences[start:stop],
                pad_value=0,
                map=lambda completion: self.completion_tensors[completion]["ids"],
            ),
            ancestor_ids=self._sequences_to_tensor(
                sequences=self.sequences[start:stop],
                pad_value=0,
                map=lambda completion: torch.tensor(
                    [
                        ancestor_ids[completion]
                        for _ in range(
                            self.completion_tensors[completion]["ids"].shape[0]
                        )
                    ]
                ),
            ),
        )

    def _get_completion_tensors(
        self,
        completion: Completion,
        tokenizer: Tokenizer,
    ) -> dict[str, torch.Tensor]:
        tokens = completion.tokens(tokenizer, cache=True)
        # replacement_token, replacement_token_id = get_replacement_token(tokens, tokenizer)
        # Hard coding this for now
        replacement_token, replacement_token_id = (
            "<|reserved_special_token_250|>",
            128255,
        )
        replaced_tokens = completion.tokens(
            tokenizer, replacement_token=replacement_token
        )
        mask = replaced_tokens == replacement_token_id
        if tokens.shape != replaced_tokens.shape:
            tokens = replaced_tokens.clone()
            tokens[mask] = torch.tensor(
                tokenizer.llm.get_tokenizer()(
                    [
                        token_logprob.token
                        for token_logprobs in completion._token_logprob_sequences()
                        for token_logprob in token_logprobs
                    ],
                    add_special_tokens=False,
                    is_split_into_words=True,  # type: ignore
                )["input_ids"]
            )
        advantages = torch.full_like(mask, fill_value=torch.nan, dtype=torch.float32)
        advantages[mask] = torch.tensor(
            [advantage for advantage in completion.token_advantages(cache=True)]
        )
        logprobs = torch.full_like(mask, fill_value=torch.nan, dtype=torch.float32)
        logprobs[mask] = torch.tensor([logprob for logprob in completion.logprobs()])
        prev_completion = next(reversed(self.completion_tensors), None)
        if prev_completion is not completion.parent:
            advantages[0] = logprobs[0] = torch.nan
        start_pos_id = (
            completion.parent.all_token_count(tokenizer, cache=True)
            if completion.parent
            else 0
        )
        return {
            "tokens": tokens,
            "advantages": advantages,
            "logprobs": logprobs,
            "ids": torch.tensor([id(completion) for _ in range(tokens.shape[0])]),
            "input_pos": torch.tensor(
                [i for i in range(start_pos_id, tokens.shape[0] + start_pos_id)]
            ),
        }

    def _write_weights(self, packed_tensors: PackedTensors) -> None:
        total_occurances = sum(self.sequences, Counter())
        sequence_occurences = Counter(
            completion for completions in self.sequences for completion in completions
        )
        weights: dict[Completion, float] = {
            completion: (
                (
                    (
                        total_occurances[completion]
                        if self.trajectories_per_episode is not None
                        else completion.sample_weight(
                            cache=True,
                            model=self.model,
                            power=self.sample_probability_power,
                        )
                    )
                    / sequence_occurences[completion]
                )
                if completion.advantage(cache=True, model=self.model) != 0
                else 0
            )
            for completion in total_occurances
        }
        average_weight = sum(weights.values()) / len(weights)
        weights = {
            completion: weight / average_weight
            for completion, weight in weights.items()
        }
        packed_tensors["weights"][: len(self.sequences)] = self._sequences_to_tensor(
            self.sequences,
            pad_value=0.0,
            map=lambda completion: torch.full(
                self.completion_tensors[completion]["tokens"].shape,
                fill_value=weights[completion],
            ),
        )

    def _normalize_advantages(self, packed_tensors: PackedTensors) -> None:
        advantages = packed_tensors["advantages"][: len(self.sequences)]
        advantages -= torch.nanmean(advantages)
        advantages /= torch.std(advantages[~torch.isnan(advantages)]) or 1.0

    def _sequences_to_tensor(
        self,
        sequences: list[Counter[Completion]],
        pad_value: float,
        map: Callable[[Completion], torch.Tensor],
    ) -> torch.Tensor:
        return torch.stack(
            [
                self._sequence_to_tensor(
                    sequence=sequence,
                    pad_value=pad_value,
                    map=map,
                )
                for sequence in sequences
            ]
        )

    def _sequence_to_tensor(
        self,
        sequence: Counter[Completion],
        pad_value: float,
        map: Callable[[Completion], torch.Tensor],
    ) -> torch.Tensor:
        return truncate_pad(
            torch.cat([map(completion) for completion in sequence]),
            [self.sequence_length],
            mode="constant",
            value=pad_value,
        )