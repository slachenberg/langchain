"""Run evaluator wrapper for string evaluators."""
from __future__ import annotations

from abc import abstractmethod
from typing import Any, Dict, List, Optional, Union

from langchainplus_sdk import EvaluationResult, RunEvaluator
from langchainplus_sdk.schemas import Example, Run

from langchain.base_language import BaseLanguageModel
from langchain.callbacks.manager import (
    AsyncCallbackManagerForChainRun,
    CallbackManagerForChainRun,
)
from langchain.chains.base import Chain
from langchain.evaluation.schema import StringEvaluator
from langchain.load.dump import dumps
from langchain.load.load import loads
from langchain.load.serializable import Serializable
from langchain.schema import RUN_KEY, messages_from_dict
from langchain.schema.messages import BaseMessage, get_buffer_string
from langchain.tools.base import Tool


def _get_messages_from_run_dict(messages: List[dict]) -> List[BaseMessage]:
    if not messages:
        return []
    first_message = messages[0]
    if "lc" in first_message:
        return [loads(dumps(message)) for message in messages]
    else:
        return messages_from_dict(messages)


class StringRunMapper(Serializable):
    """Extract items to evaluate from the run object."""

    @property
    def output_keys(self) -> List[str]:
        """The keys to extract from the run."""
        return ["prediction", "input"]

    @abstractmethod
    def map(self, run: Run) -> Dict[str, str]:
        """Maps the Run to a dictionary."""

    def __call__(self, run: Run) -> Dict[str, str]:
        """Maps the Run to a dictionary."""
        if not run.outputs:
            raise ValueError(f"Run {run.id} has no outputs to evaluate.")
        return self.map(run)


class LLMStringRunMapper(StringRunMapper):
    """Extract items to evaluate from the run object."""

    def serialize_chat_messages(self, messages: List[Dict]) -> str:
        """Extract the input messages from the run."""
        if isinstance(messages, list) and messages:
            if isinstance(messages[0], dict):
                chat_messages = _get_messages_from_run_dict(messages)
            elif isinstance(messages[0], list):
                # Runs from Tracer have messages as a list of lists of dicts
                chat_messages = _get_messages_from_run_dict(messages[0])
            else:
                raise ValueError(f"Could not extract messages to evaluate {messages}")
            return get_buffer_string(chat_messages)
        raise ValueError(f"Could not extract messages to evaluate {messages}")

    def serialize_inputs(self, inputs: Dict) -> str:
        if "prompts" in inputs:  # Should we even accept this?
            input_ = "\n\n".join(inputs["prompts"])
        elif "prompt" in inputs:
            input_ = inputs["prompt"]
        elif "messages" in inputs:
            input_ = self.serialize_chat_messages(inputs["messages"])
        else:
            raise ValueError("LLM Run must have either messages or prompts as inputs.")
        return input_

    def serialize_outputs(self, outputs: Dict) -> str:
        if not outputs.get("generations"):
            raise ValueError("Cannot evaluate LLM Run without generations.")
        generations: List[Dict] = outputs["generations"]
        if not generations:
            raise ValueError("Cannot evaluate LLM run with empty generations.")
        first_generation: Dict = generations[0]
        if isinstance(first_generation, list):
            # Runs from Tracer have generations as a list of lists of dicts
            # Whereas Runs from the API have a list of dicts
            first_generation = first_generation[0]
        if "message" in first_generation:
            output_ = self.serialize_chat_messages([first_generation["message"]])
        else:
            output_ = first_generation["text"]
        return output_

    def map(self, run: Run) -> Dict[str, str]:
        """Maps the Run to a dictionary."""
        if run.run_type != "llm":
            raise ValueError("LLM RunMapper only supports LLM runs.")
        elif not run.outputs:
            if run.error:
                raise ValueError(
                    f"Cannot evaluate errored LLM run {run.id}: {run.error}"
                )
            else:
                raise ValueError(
                    f"Run {run.id} has no outputs. Cannot evaluate this run."
                )
        else:
            try:
                inputs = self.serialize_inputs(run.inputs)
            except Exception as e:
                raise ValueError(
                    f"Could not parse LM input from run inputs {run.inputs}"
                ) from e
            try:
                output_ = self.serialize_outputs(run.outputs)
            except Exception as e:
                raise ValueError(
                    f"Could not parse LM prediction from run outputs {run.outputs}"
                ) from e
            return {"input": inputs, "prediction": output_}


class ChainStringRunMapper(StringRunMapper):
    """Extract items to evaluate from the run object from a chain."""

    input_key: str
    """The key from the model Run's inputs to use as the eval input."""
    prediction_key: str
    """The key from the model Run's outputs to use as the eval prediction."""

    @classmethod
    def from_chain(
        cls,
        model: Chain,
        input_key: Optional[str] = None,
        prediction_key: Optional[str] = None,
    ) -> ChainStringRunMapper:
        """Create a RunMapper from a chain."""
        error_messages = []
        if input_key is None:
            if len(model.input_keys) > 1:
                error_messages.append(
                    f"Chain {model.lc_namespace} has multiple input"
                    " keys. Please specify 'input_key' when loading."
                )
            else:
                input_key = model.input_keys[0]
        elif input_key not in model.input_keys:
            error_messages.append(
                f"Chain {model.lc_namespace} does not have specified"
                f" input key {input_key}."
            )
        if prediction_key is None:
            if len(model.output_keys) > 1:
                error_messages.append(
                    f"Chain {model.lc_namespace} has multiple"
                    " output keys. Please specify 'prediction_key' when loading."
                )
            else:
                prediction_key = model.output_keys[0]
        elif prediction_key not in model.output_keys:
            error_messages.append(
                f"Chain {model.lc_namespace} does not have specified"
                f" prediction_key {prediction_key}."
            )
        if error_messages:
            raise ValueError("\n".join(error_messages))
        if input_key is None or prediction_key is None:
            # This should never happen, but mypy doesn't know that.
            raise ValueError(f"Chain {model.lc_namespace} has no input or output keys.")
        return cls(input_key=input_key, prediction_key=prediction_key)

    def map(self, run: Run) -> Dict[str, str]:
        """Maps the Run to a dictionary."""
        if not run.outputs:
            raise ValueError(f"Run {run.id} has no outputs to evaluate.")
        if run.run_type != "chain":
            raise ValueError("Chain RunMapper only supports Chain runs.")
        if self.input_key not in run.inputs:
            raise ValueError(f"Run {run.id} does not have input key {self.input_key}.")
        elif self.prediction_key not in run.outputs:
            raise ValueError(
                f"Run {run.id} does not have prediction key {self.prediction_key}."
            )
        else:
            return {
                "input": run.inputs[self.input_key],
                "prediction": run.outputs[self.prediction_key],
            }


class ToolStringRunMapper(StringRunMapper):
    """Map an input to the tool."""

    def map(self, run: Run) -> Dict[str, str]:
        if not run.outputs:
            raise ValueError(f"Run {run.id} has no outputs to evaluate.")
        return {"input": run.inputs["input"], "prediction": run.outputs["output"]}


class StringExampleMapper(Serializable):
    """Map an example, or row in the dataset, to the inputs of an evaluation."""

    reference_key: Optional[str] = None

    @property
    def output_keys(self) -> List[str]:
        """The keys to extract from the run."""
        return ["reference"]

    def serialize_chat_messages(self, messages: List[Dict]) -> str:
        """Extract the input messages from the run."""
        chat_messages = _get_messages_from_run_dict(messages)
        return get_buffer_string(chat_messages)

    def map(self, example: Example) -> Dict[str, str]:
        """Maps the Example, or dataset row to a dictionary."""
        if not example.outputs:
            raise ValueError(
                f"Example {example.id} has no outputs to use as a reference."
            )
        if self.reference_key is None:
            if len(example.outputs) > 1:
                raise ValueError(
                    f"Example {example.id} has multiple outputs, so you must"
                    " specify a reference_key."
                )
            else:
                output = list(example.outputs.values())[0]
                return {
                    "reference": self.serialize_chat_messages([output])
                    if isinstance(output, dict)
                    and output.get("type")
                    and output.get("data")
                    else output
                }
        elif self.reference_key not in example.outputs:
            raise ValueError(
                f"Example {example.id} does not have reference key"
                f" {self.reference_key}."
            )
        return {"reference": example.outputs[self.reference_key]}

    def __call__(self, example: Example) -> Dict[str, str]:
        """Maps the Run and Example to a dictionary."""
        if not example.outputs:
            raise ValueError(
                f"Example {example.id} has no outputs to use as areference label."
            )
        return self.map(example)


class StringRunEvaluatorChain(Chain, RunEvaluator):
    """Evaluate Run and optional examples."""

    run_mapper: StringRunMapper
    """Maps the Run to a dictionary with 'input' and 'prediction' strings."""
    example_mapper: Optional[StringExampleMapper] = None
    """Maps the Example (dataset row) to a dictionary
    with a 'reference' string."""
    name: str
    """The name of the evaluation metric."""
    string_evaluator: StringEvaluator
    """The evaluation chain."""

    @property
    def input_keys(self) -> List[str]:
        return ["run", "example"]

    @property
    def output_keys(self) -> List[str]:
        return ["feedback"]

    def _prepare_input(self, inputs: Dict[str, Any]) -> Dict[str, str]:
        run: Run = inputs["run"]
        example: Optional[Example] = inputs.get("example")
        evaluate_strings_inputs = self.run_mapper(run)
        if example and self.example_mapper:
            evaluate_strings_inputs.update(self.example_mapper(example))
        elif self.string_evaluator.requires_reference:
            raise ValueError(
                f"Evaluator {self.name} requires an reference"
                " example from the dataset,"
                f" but none was provided for run {run.id}."
            )
        return evaluate_strings_inputs

    def _prepare_output(self, output: Dict[str, Any]) -> EvaluationResult:
        evaluation_result = EvaluationResult(key=self.name, **output)
        if RUN_KEY in output:
            # TODO: Not currently surfaced. Update
            evaluation_result.evaluator_info[RUN_KEY] = output[RUN_KEY]
        return evaluation_result

    def _call(
        self,
        inputs: Dict[str, str],
        run_manager: Optional[CallbackManagerForChainRun] = None,
    ) -> Dict[str, Any]:
        """Call the evaluation chain."""
        evaluate_strings_inputs = self._prepare_input(inputs)
        _run_manager = run_manager or CallbackManagerForChainRun.get_noop_manager()
        callbacks = _run_manager.get_child()
        chain_output = self.string_evaluator.evaluate_strings(
            **evaluate_strings_inputs,
            callbacks=callbacks,
        )
        evaluation_result = self._prepare_output(chain_output)
        return {"feedback": evaluation_result}

    async def _acall(
        self,
        inputs: Dict[str, str],
        run_manager: AsyncCallbackManagerForChainRun | None = None,
    ) -> Dict[str, Any]:
        """Call the evaluation chain."""
        evaluate_strings_inputs = self._prepare_input(inputs)
        _run_manager = run_manager or AsyncCallbackManagerForChainRun.get_noop_manager()
        callbacks = _run_manager.get_child()
        chain_output = await self.string_evaluator.aevaluate_strings(
            **evaluate_strings_inputs,
            callbacks=callbacks,
        )
        evaluation_result = self._prepare_output(chain_output)
        return {"feedback": evaluation_result}

    def evaluate_run(
        self, run: Run, example: Optional[Example] = None
    ) -> EvaluationResult:
        """Evaluate an example."""
        return self({"run": run, "example": example})["feedback"]

    async def aevaluate_run(
        self, run: Run, example: Optional[Example] = None
    ) -> EvaluationResult:
        """Evaluate an example."""
        result = await self.acall({"run": run, "example": example})
        return result["feedback"]

    @classmethod
    def from_model_and_evaluator(
        cls,
        model: Union[Chain, BaseLanguageModel, Tool],
        evaluator: StringEvaluator,
        input_key: Optional[str] = None,
        prediction_key: Optional[str] = None,
        reference_key: Optional[str] = None,
    ) -> StringRunEvaluatorChain:
        """Create a StringRunEvaluatorChain from a model and evaluator."""
        if isinstance(model, BaseLanguageModel):
            run_mapper: StringRunMapper = LLMStringRunMapper()
        elif isinstance(model, Chain):
            run_mapper = ChainStringRunMapper.from_chain(
                model, input_key=input_key, prediction_key=prediction_key
            )
        elif isinstance(model, Tool):
            run_mapper = ToolStringRunMapper()
        else:
            raise NotImplementedError(
                f"{cls.__name__}.from_model_and_evaluator({type(model)})"
                " not yet implemented."
                "Expected one of [BaseLanguageModel, Chain, Tool]."
            )
        if reference_key is not None or isinstance(model, BaseLanguageModel):
            example_mapper = StringExampleMapper(reference_key=reference_key)
        elif evaluator.requires_reference:
            # We could potentially auto-infer if there is only one string in the
            # example, but it's preferred to raise earlier.
            raise ValueError(
                f"Evaluator {evaluator.evaluation_name} requires a reference"
                " example from the dataset. Please specify the reference key from"
                " amongst the dataset outputs keys."
            )
        else:
            example_mapper = None
        return cls(
            name=evaluator.evaluation_name,
            run_mapper=run_mapper,
            example_mapper=example_mapper,
            string_evaluator=evaluator,
        )