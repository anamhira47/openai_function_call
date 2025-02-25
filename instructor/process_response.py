# type: ignore[all]

from collections.abc import Iterable
from textwrap import dedent
from instructor.dsl.iterable import IterableBase, IterableModel
from instructor.dsl.parallel import ParallelBase, ParallelModel, handle_parallel_model
from instructor.dsl.partial import PartialBase
from instructor.dsl.simple_type import AdapterBase, ModelAdapter, is_simple_type
from instructor.function_calls import OpenAISchema, openai_schema, openai_schema_from_json

from openai.types.chat import ChatCompletion
from pydantic import BaseModel


import inspect
import logging
from typing import (
    Generator,
    Optional,
    Type,
    Tuple,
    get_args,
    get_origin,
    TypeVar,
    ParamSpec,
    Any,
    Dict,
)

from instructor.mode import Mode


logger = logging.getLogger("instructor")

T_Model = TypeVar("T_Model", bound=BaseModel)
T_Retval = TypeVar("T_Retval")
T_ParamSpec = ParamSpec("T_ParamSpec")
T = TypeVar("T")


async def process_response_async(
    response: ChatCompletion,
    *,
    response_model: Type[T_Model | OpenAISchema | BaseModel],
    stream: bool = False,
    validation_context: Optional[dict] = None,
    strict: Optional[bool] = None,
    mode: Mode = Mode.TOOLS,
) -> T_Model | ChatCompletion:
    """Processes a OpenAI response with the response model, if available.
    It can use `validation_context` and `strict` to validate the response
    via the pydantic model

    Args:
        response (ChatCompletion): The response from OpenAI's API
        response_model (BaseModel): The response model to use for parsing the response
        stream (bool): Whether the response is a stream
        validation_context (dict, optional): The validation context to use for validating the response. Defaults to None.
        strict (bool, optional): Whether to use strict json parsing. Defaults to None.
    """

    logger.debug(
        f"Instructor Raw Response: {response}",
    )
    if response_model is None:
        return response

    if (
        inspect.isclass(response_model)
        and issubclass(response_model, (IterableBase, PartialBase))
        and stream
    ):
        model = await response_model.from_streaming_response_async(
            response,
            mode=mode,
        )
        return model

    model = response_model.from_response(
        response,
        validation_context=validation_context,
        strict=strict,
        mode=mode,
    )

    # ? This really hints at the fact that we need a better way of
    # ? attaching usage data and the raw response to the model we return.
    if isinstance(model, IterableBase):
        logger.debug(f"Returning takes from IterableBase")
        return [task for task in model.tasks]

    if isinstance(response_model, ParallelBase):
        logger.debug(f"Returning model from ParallelBase")
        return model

    if isinstance(model, AdapterBase):
        logger.debug(f"Returning model from AdapterBase")
        return model.content

    model._raw_response = response
    return model


def process_response(
    response: T_Model,
    *,
    response_model: Type[OpenAISchema | BaseModel],
    stream: bool,
    validation_context: Optional[dict] = None,
    strict=None,
    mode: Mode = Mode.TOOLS,
) -> T_Model | Generator[T_Model, None, None] | ChatCompletion:
    """Processes a OpenAI response with the response model, if available.

    Args:
        response (T): The response from OpenAI's API
        response_model (Type[T_Model]): The response model to use for parsing the response
        stream (bool): Whether the response is a stream
        validation_context (dict, optional): The validation context to use for validating the response. Defaults to None.
        strict (_type_, optional): Whether to use strict json parsing. Defaults to None.
        mode (Mode, optional): The openai completion mode. Defaults to Mode.FUNCTIONS.

    Returns:
        Union[T_Model, T]: The parsed response, if a response model is available, otherwise the response as is from the SDK
    """

    logger.debug(
        f"Instructor Raw Response: {response}",
    )

    if response_model is None:
        logger.debug("No response model, returning response as is")
        return response

    if (
        inspect.isclass(response_model)
        and issubclass(response_model, (IterableBase, PartialBase))
        and stream
    ):
        model = response_model.from_streaming_response(
            response,
            mode=mode,
        )
        return model

    model = response_model.from_response(
        response,
        validation_context=validation_context,
        strict=strict,
        mode=mode,
    )

    # ? This really hints at the fact that we need a better way of
    # ? attaching usage data and the raw response to the model we return.
    if isinstance(model, IterableBase):
        logger.debug(f"Returning takes from IterableBase")
        return [task for task in model.tasks]

    if isinstance(response_model, ParallelBase):
        logger.debug(f"Returning model from ParallelBase")
        return model

    if isinstance(model, AdapterBase):
        logger.debug(f"Returning model from AdapterBase")
        return model.content
    if isinstance(model, str):
        return model

    model._raw_response = response
    return model


def handle_response_model(
    response_model: T, mode: Mode = Mode.TOOLS, **kwargs
) -> Tuple[Type[OpenAISchema], Dict[str, Any]]:
    """Prepare the response model type hint, and returns the response_model
    along with the new modified kwargs needed to be able to use the response_model
    parameter with the patch function.


    Args:
        response_model (T): The response model to use for parsing the response
        mode (Mode, optional): The openai completion mode. Defaults to Mode.TOOLS.

    Raises:
        NotImplementedError: When using stream=True with a non-iterable response_model
        ValueError: When using an invalid patch mode

    Returns:
        Union[Type[OpenAISchema], dict]: The response model to use for parsing the response
    """
    new_kwargs = kwargs.copy()
    if response_model is not None:
        # Handles the case where the response_model is a simple type
        # Literal, Annotated, Union, str, int, float, bool, Enum
        # We wrap the response_model in a ModelAdapter that sets 'content' as the response
        if is_simple_type(response_model):
            response_model = ModelAdapter[response_model]

        # This a special case for parallel tools
        if mode == Mode.PARALLEL_TOOLS:
            assert (
                new_kwargs.get("stream", False) is False
            ), "stream=True is not supported when using PARALLEL_TOOLS mode"
            new_kwargs["tools"] = handle_parallel_model(response_model)
            new_kwargs["tool_choice"] = "auto"

            # This is a special case for parallel models
            response_model = ParallelModel(typehint=response_model)
            return response_model, new_kwargs

        # This is for all other single model cases
        if get_origin(response_model) is Iterable:
            iterable_element_class = get_args(response_model)[0]
            response_model = IterableModel(iterable_element_class)
        if isinstance(response_model, dict):
            logger.info("Response model is a dict, creating OpenAISchema from it")
            response_model = openai_schema_from_json(response_model)
        elif not issubclass(response_model, OpenAISchema):
            response_model = openai_schema(response_model)  # type: ignore
        
        if new_kwargs.get("stream", False) and not issubclass(
            response_model, (IterableBase, PartialBase)
        ):
            raise NotImplementedError(
                "stream=True is not supported when using response_model parameter for non-iterables"
            )

        if mode == Mode.FUNCTIONS:
            new_kwargs["functions"] = [response_model.openai_schema]  # type: ignore
            new_kwargs["function_call"] = {"name": response_model.openai_schema["name"]}  # type: ignore
        elif mode in {Mode.TOOLS, Mode.MISTRAL_TOOLS}:
            new_kwargs["tools"] = [
                {
                    "type": "function",
                    "function": response_model.openai_schema,
                }
            ]
            if mode == Mode.MISTRAL_TOOLS:
                new_kwargs["tool_choice"] = "any"
            else:
                new_kwargs["tool_choice"] = {
                    "type": "function",
                    "function": {"name": response_model.openai_schema["name"]},
                }
        elif mode in {Mode.JSON, Mode.MD_JSON, Mode.JSON_SCHEMA}:
            # If its a JSON Mode we need to massage the prompt a bit
            # in order to get the response we want in a json format
            message = dedent(
                f"""
                As a genius expert, your task is to understand the content and provide
                the parsed objects in json that match the following json_schema:\n

                {response_model.model_json_schema()}

                Make sure to return an instance of the JSON, not the schema itself
                """
            )

            if mode == Mode.JSON:
                new_kwargs["response_format"] = {"type": "json_object"}

            elif mode == Mode.JSON_SCHEMA:
                new_kwargs["response_format"] = {
                    "type": "json_object",
                    "schema": response_model.model_json_schema(),
                }

            elif mode == Mode.MD_JSON:
                new_kwargs["messages"].append(
                    {
                        "role": "user",
                        "content": "Return the correct JSON response within a ```json codeblock. not the JSON_SCHEMA",
                    },
                )
            # check that the first message is a system message
            # if it is not, add a system message to the beginning
            if new_kwargs["messages"][0]["role"] != "system":
                new_kwargs["messages"].insert(
                    0,
                    {
                        "role": "system",
                        "content": message,
                    },
                )
            # if it is, system append the schema to the end
            else:
                new_kwargs["messages"][0]["content"] += f"\n\n{message}"
        else:
            raise ValueError(f"Invalid patch mode: {mode}")

    logger.debug(
        f"Instructor Request: {mode.value=}, {response_model=}, {new_kwargs=}",
        extra={
            "mode": mode.value,
            "response_model": getattr(response_model, '__name__', getattr(response_model, 'name', None)) if response_model is not None else None,
            "new_kwargs": new_kwargs,
        },
    )
    return response_model, new_kwargs
