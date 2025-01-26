from openai.types.chat.chat_completion_message_param import ChatCompletionMessageParam
from typing import Any, NotRequired, Required, TypedDict


class NotFoundError(Exception):
    pass


class Dataset: ...


class Task: ...


class PatternRewardDict(TypedDict):
    regex_pattern: str
    expected_capture_groups: list[str] | dict[str, str]
    weight: NotRequired[float]


class TaskDict(TypedDict, total=False):
    messages: Required[list[ChatCompletionMessageParam]]
    pattern_rewards: list[PatternRewardDict]
    metadata: dict[str, Any]


async def create_dataset(name: str, tasks: list[TaskDict]) -> Dataset: ...


async def get_dataset(name: str) -> Dataset: ...


async def delete_dataset(name: str) -> None: ...
