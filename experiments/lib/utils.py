import asyncio
import black
from collections import deque
import time
from openai import AsyncOpenAI
from openai.types.chat.chat_completion_token_logprob import ChatCompletionTokenLogprob
import torch
from typing import (
    Any,
    Callable,
    Coroutine,
    Optional,
    ParamSpec,
    Sequence,
    TypeVar,
    Union,
)


def black_print(
    value: object,
) -> None:
    """
    Prints the value with black formatting.

    Args:
        value (object): The value to print.

    Note:
        The string representation of the value must be valid Python code.
    """
    print(
        black.format_str(str(value), mode=black.Mode()).strip(),
    )


def get_semaphore(client: AsyncOpenAI, default_value: int = 100) -> asyncio.Semaphore:
    if hasattr(client, "_semaphore"):
        return getattr(client, "_semaphore")
    transport = client._client._transport
    semaphore = asyncio.Semaphore(
        value=(
            getattr(
                getattr(transport, "_pool"), "_max_keepalive_connections", default_value
            )
            if hasattr(transport, "_pool")
            else default_value
        )
    )
    setattr(client, "_semaphore", semaphore)
    return semaphore


def get_token(token_logprob: ChatCompletionTokenLogprob) -> str:
    if token_logprob.token.startswith("token_id:"):
        return bytes(token_logprob.bytes or []).decode()
    return token_logprob.token


def get_token_id(token_logprob: ChatCompletionTokenLogprob) -> int:
    assert token_logprob.token.startswith("token_id:"), "Unable to recover token ID"
    return int(token_logprob.token.removeprefix("token_id:"))


def read_last_n_lines(filename: str, n: int) -> str:
    """Read the last n lines of a file efficiently.

    Args:
        filename: Path to the file to read
        n: Number of lines to read from end

    Returns:
        String containing the last n lines
    """

    # Use deque with maxlen to efficiently store only n lines
    lines = deque(maxlen=n)

    # Read file in chunks from end
    with open(filename, "rb") as f:
        # Seek to end of file
        f.seek(0, 2)
        file_size = f.tell()

        # Start from end, read in 8KB chunks
        chunk_size = 8192
        position = file_size

        # Read chunks until we have n lines or reach start
        while position > 0 and len(lines) < n:
            # Move back one chunk
            chunk_size = min(chunk_size, position)
            position -= chunk_size
            f.seek(position)
            chunk = f.read(chunk_size).decode()

            # Split into lines and add to deque
            lines.extendleft(chunk.splitlines())

        # If we're not at file start, we may have a partial first line
        if position > 0:
            # Read one more chunk to get complete first line
            position -= 1
            f.seek(position)
            chunk = f.read(chunk_size).decode()
            lines[0] = chunk[chunk.rindex("\n") + 1 :] + lines[0]

    return "\n".join(lines)


P = ParamSpec("P")
T = TypeVar("T")


def return_exception(callable: Callable[P, T]) -> Callable[P, Union[T, BaseException]]:
    """Decorator to return exception instead of raising it.

    Args:
        callable: Function to decorate

    Returns:
        Decorated function that returns exception instead of raising it
    """

    def wrapper(*args: P.args, **kwargs: P.kwargs) -> Union[T, BaseException]:
        try:
            return callable(*args, **kwargs)
        except BaseException as exception:
            return exception

    return wrapper


def async_return_exception(
    callable: Callable[P, Coroutine[Any, Any, T]]
) -> Callable[P, Coroutine[Any, Any, Union[T, BaseException]]]:
    """Decorator to return exception instead of raising it for async functions.

    Args:
        callable: Async function to decorate

    Returns:
        Decorated async function that returns exception instead of raising it
    """

    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> Union[T, BaseException]:
        try:
            return await callable(*args, **kwargs)
        except BaseException as exception:
            return exception

    return wrapper


def retry(
    max_attempts: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Decorator that retries a function on failure with exponential backoff.

    Args:
        max_attempts: Maximum number of retry attempts before giving up
        delay: Initial delay between retries in seconds
        backoff: Multiplicative factor to increase delay between retries
        exceptions: Tuple of exception types to catch and retry on

    Returns:
        Decorated function that will retry on specified exceptions
    """

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            current_delay = delay
            last_exception: Optional[Exception] = None

            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt == max_attempts - 1:
                        raise
                    time.sleep(current_delay)
                    current_delay *= backoff

            assert last_exception is not None  # for type checker
            raise last_exception

        return wrapper

    return decorator


class Timer:
    def __init__(self, description: str):
        self.description = description
        self.start_time = 0.0

    def __enter__(self):
        self.start_time = time.time()
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[Any],
    ) -> None:
        if not exc_type:
            seconds = time.time() - self.start_time
            print(f"{self.description} in {seconds:.2f}s ✓")


def truncate_pad(
    input: torch.Tensor,
    shape: Sequence[int],
    mode: str = "constant",
    value: Optional[float] = None,
) -> torch.Tensor:
    """Truncates or pads a tensor to match the target shape.

    For each dimension i, if shape[i] is:
    - -1: Leave that dimension unchanged
    - < input.shape[i]: Truncate to first shape[i] elements
    - > input.shape[i]: Pad with value to reach shape[i] elements

    Args:
        input: Input tensor to reshape
        shape: Target shape, with -1 indicating unchanged dimensions
        mode: Padding mode to pass to torch.nn.functional.pad
        value: Pad value to pass to torch.nn.functional.pad

    Returns:
        Tensor with dimensions matching shape (except where -1)
    """
    result = input
    for i in range(len(shape)):
        if shape[i] == -1:
            continue
        if shape[i] < input.shape[i]:
            # Truncate on this dimension
            slicing = [slice(None)] * len(input.shape)
            slicing[i] = slice(0, shape[i])
            result = result[tuple(slicing)]
        elif shape[i] > input.shape[i]:
            # Start of Selection
            padding = [0] * (2 * len(input.shape))
            padding[2 * (len(input.shape) - i - 1) + 1] = shape[i] - input.shape[i]
            result = torch.nn.functional.pad(result, padding, mode=mode, value=value)
    return result
