import json
from textwrap import dedent
from typing import Optional, List, Iterator, Dict, Any


from phi.llm.base import LLM
from phi.llm.message import Message
from phi.tools.function import FunctionCall
from phi.utils.log import logger
from phi.utils.timer import Timer
from phi.utils.tools import (
    get_function_call_for_tool_call,
    extract_tool_from_xml,
    remove_function_calls_from_string,
)

try:
    import anthropic
except ImportError:
    logger.error("`anthropic` not installed")
    raise


class Claude(LLM):
    name: str = "claude"
    model: str = "claude-3-opus-20240229"
    # -*- Request parameters
    max_tokens: Optional[int] = 1024
    temperature: Optional[float] = None
    stop_sequences: Optional[List[str]] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    request_params: Optional[Dict[str, Any]] = None
    # -*- Client parameters
    api_key: Optional[str] = None
    client_params: Optional[Dict[str, Any]] = None
    # -*- Provide the client manually
    claude_client: Optional[anthropic.Anthropic] = None

    @property
    def client(self) -> anthropic.Anthropic:
        if self.claude_client:
            return self.claude_client

        _client_params: Dict[str, Any] = {}
        if self.api_key:
            _client_params["api_key"] = self.api_key
        return anthropic.Anthropic(**_client_params)

    @property
    def api_kwargs(self) -> Dict[str, Any]:
        _request_params: Dict[str, Any] = {}
        if self.max_tokens:
            _request_params["max_tokens"] = self.max_tokens
        if self.temperature:
            _request_params["temperature"] = self.temperature
        if self.stop_sequences:
            _request_params["stop_sequences"] = self.stop_sequences
        if self.top_p:
            _request_params["top_p"] = self.top_p
        if self.top_k:
            _request_params["top_k"] = self.top_k
        if self.request_params:
            _request_params.update(self.request_params)
        return _request_params

    # def to_dict(self) -> Dict[str, Any]:
    #     _dict = super().to_dict
    #     # Unsure about what to add here
    #     return _dict

    def invoke(self, messages: List[Message]) -> Dict[str, Any]:
        system_message: Message
        user_assistant_message: List[Message] = []

        for message in messages:
            if message.role == "system":
                system_message = message
            else:
                user_assistant_message.append(message)

        return self.client.messages.create(
            messages=[
                {"role": dump.get("role"), "content": dump.get("content")}
                for m in user_assistant_message
                for dump in [m.model_dump()]
            ],
            system=system_message.content,
            model=self.model,
            max_tokens=self.max_tokens,
            stop_sequences=["</function_calls>"],
        )

    def invoke_stream(self, messages: List[Message]) -> Any:
        system_message: Message
        user_assistant_message: List[Message] = []

        for message in messages:
            if message.role == "system":
                system_message = message
            else:
                user_assistant_message.append(message)

        return self.client.messages.stream(
            max_tokens=self.max_tokens,
            model=self.model,
            messages=[
                {"role": dump.get("role"), "content": dump.get("content")}
                for m in user_assistant_message
                for dump in [m.model_dump()]
            ],
            system=system_message.content,
            stop_sequences=["</function_calls>"],
        )

    def response(self, messages: List[Message]) -> str:
        logger.debug("---------- Claude Response Start ----------")
        # -*- Log messages for debugging
        for m in messages:
            m.log()

        response_timer = Timer()
        response_timer.start()
        response: Dict[str, Any] = self.invoke(messages=messages)
        response_timer.stop()
        logger.debug(f"Time to generate response: {response_timer.elapsed:.4f}s")

        response_content = response.content[0].text  # type: ignore

        # -*- Create assistant message
        assistant_message = Message(
            role=response.role or "assistant",  # type: ignore
            content=response_content,
        )

        # Check if the response contains a tool call
        try:
            if response_content is not None:
                if "<function_calls>" in response_content:
                    # List of tool calls added to the assistant message
                    tool_calls: List[Dict[str, Any]] = []

                    # Add function call closing tag to the assistant message
                    assistant_message.content += "</function_calls>"  # type: ignore

                    # If the assistant is calling multiple functions, the response will contain multiple <invoke> tags
                    response_content = response_content.split("</invoke>")
                    for tool_call_response in response_content:
                        if "<invoke>" in tool_call_response:
                            # Extract tool call string from response
                            tool_call_dict = extract_tool_from_xml(tool_call_response)

                            tool_call_name = tool_call_dict.get("tool_name")
                            tool_call_args = tool_call_dict.get("parameters")
                            function_def = {"name": tool_call_name}
                            if tool_call_args is not None:
                                function_def["arguments"] = json.dumps(tool_call_args)
                            tool_calls.append(
                                {
                                    "type": "function",
                                    "function": function_def,
                                }
                            )
                            logger.debug(f"Tool Calls: {tool_calls}")

                    if len(tool_calls) > 0:
                        assistant_message.tool_calls = tool_calls
        except Exception as e:
            logger.warning(e)
            pass

        # -*- Update usage metrics
        # Add response time to metrics
        assistant_message.metrics["time"] = response_timer.elapsed
        if "response_times" not in self.metrics:
            self.metrics["response_times"] = []
        self.metrics["response_times"].append(response_timer.elapsed)

        # -*- Add assistant message to messages
        messages.append(assistant_message)
        assistant_message.log()

        # -*- Parse and run function call
        if assistant_message.tool_calls is not None and self.run_tools:
            # Remove the tool call from the response content
            final_response = remove_function_calls_from_string(assistant_message.content)  # type: ignore
            function_calls_to_run: List[FunctionCall] = []
            for tool_call in assistant_message.tool_calls:
                _function_call = get_function_call_for_tool_call(tool_call, self.functions)
                if _function_call is None:
                    messages.append(Message(role="user", content="Could not find function to call."))
                    continue
                if _function_call.error is not None:
                    messages.append(Message(role="user", content=_function_call.error))
                    continue
                function_calls_to_run.append(_function_call)

            if self.show_tool_calls:
                if len(function_calls_to_run) == 1:
                    final_response += f" - Running: {function_calls_to_run[0].get_call_str()}\n\n"
                elif len(function_calls_to_run) > 1:
                    final_response += "Running:"
                    for _f in function_calls_to_run:
                        final_response += f"\n - {_f.get_call_str()}"
                    final_response += "\n\n"

            function_call_results = self.run_function_calls(function_calls_to_run, role="user")
            if len(function_call_results) > 0:
                fc_responses = "<function_results>"

                for _fc_message in function_call_results:
                    fc_responses += "<result>"
                    fc_responses += "<tool_name>" + _fc_message.tool_call_name + "</tool_name>"  # type: ignore
                    fc_responses += "<stdout>" + _fc_message.content + "</stdout>"  # type: ignore
                    fc_responses += "</result>"
                fc_responses += "</function_results>"

                messages.append(Message(role="user", content=fc_responses))

            # -*- Yield new response using results of tool calls
            final_response += self.response(messages=messages)
            return final_response
        logger.debug("---------- Claude Response End ----------")
        # -*- Return content if no function calls are present
        if assistant_message.content is not None:
            return assistant_message.get_content_string()
        return "Something went wrong, please try again."

    def response_stream(self, messages: List[Message]) -> Iterator[str]:
        logger.debug("---------- Claude Response Start ----------")
        # -*- Log messages for debugging
        for m in messages:
            m.log()

        response_timer = Timer()
        response_timer.start()

        assistant_message_content = ""
        response_is_function_call = False
        tool_calls_in_response = 0
        is_closing_tool_call = False
        function_call_flag = False

        response = self.invoke_stream(messages=messages)

        with response as stream:
            for stream_delta in stream.text_stream:
                # logger.debug(f"Stream Delta: {stream_delta}")

                # Add response content to assistant message
                if stream_delta is not None:
                    assistant_message_content += stream_delta

                if stream_delta == "<function":
                    function_call_flag = True

                if stream_delta == ">":
                    function_call_flag = False

                if not function_call_flag:
                    # If the response is a tool call, it will start with a "<function" token
                    # If response == "<invoke", set response_is_function_call to True
                    if "<invoke" in stream_delta:
                        if assistant_message_content.count("<invoke") > assistant_message_content.count("</invoke>"):
                            response_is_function_call = True
                            tool_calls_in_response += 1

                    if response_is_function_call:
                        if assistant_message_content.count("<invoke") == assistant_message_content.count("</invoke>"):
                            response_is_function_call = False
                            is_closing_tool_call = True

                    if not response_is_function_call:
                        if is_closing_tool_call and stream_delta.strip().endswith(">"):
                            is_closing_tool_call = False

                        yield stream_delta

        response_timer.stop()
        logger.debug(f"Time to generate response: {response_timer.elapsed:.4f}s")

        # Add function call closing tag to the assistant message
        if assistant_message_content.count("<function_calls>") == 1:
            assistant_message_content += "</function_calls>"

        # -*- Create assistant message
        assistant_message = Message(
            role="assistant",
            content=assistant_message_content,
        )

        # Check if the response is a tool call
        try:
            if tool_calls_in_response > 0:
                if "<invoke>" in assistant_message_content and "</invoke>" in assistant_message_content:
                    # List of tool calls added to the assistant message
                    tool_calls: List[Dict[str, Any]] = []
                    # Break the response into tool calls
                    tool_call_responses = assistant_message_content.split("</invoke>")
                    for tool_call_response in tool_call_responses:
                        # Add back the closing tag if this is not the last tool call
                        if tool_call_response != tool_call_responses[-1]:
                            tool_call_response += "</invoke>"

                        if "<invoke>" in tool_call_response and "</invoke>" in tool_call_response:
                            # Extract tool call string from response
                            tool_call_dict = extract_tool_from_xml(tool_call_response)

                            tool_call_name = tool_call_dict.get("tool_name")
                            tool_call_args = tool_call_dict.get("parameters")
                            function_def = {"name": tool_call_name}
                            if tool_call_args is not None:
                                function_def["arguments"] = json.dumps(tool_call_args)
                            tool_calls.append(
                                {
                                    "type": "function",
                                    "function": function_def,
                                }
                            )
                            logger.debug(f"Tool Calls: {tool_calls}")

                    # If tool call parsing is successful, add tool calls to the assistant message
                    if len(tool_calls) > 0:
                        assistant_message.tool_calls = tool_calls
        except Exception:
            logger.warning(f"Could not parse tool calls from response: {assistant_message_content}")
            pass

        # -*- Update usage metrics
        # Add response time to metrics
        assistant_message.metrics["time"] = response_timer.elapsed
        if "response_times" not in self.metrics:
            self.metrics["response_times"] = []
        self.metrics["response_times"].append(response_timer.elapsed)

        # -*- Add assistant message to messages
        messages.append(assistant_message)
        assistant_message.log()

        # -*- Parse and run function call
        if assistant_message.tool_calls is not None and self.run_tools:
            function_calls_to_run: List[FunctionCall] = []
            for tool_call in assistant_message.tool_calls:
                _function_call = get_function_call_for_tool_call(tool_call, self.functions)
                if _function_call is None:
                    messages.append(Message(role="user", content="Could not find function to call."))
                    continue
                if _function_call.error is not None:
                    messages.append(Message(role="user", content=_function_call.error))
                    continue
                function_calls_to_run.append(_function_call)

            if self.show_tool_calls:
                if len(function_calls_to_run) == 1:
                    yield f"- Running: {function_calls_to_run[0].get_call_str()}\n\n"
                elif len(function_calls_to_run) > 1:
                    yield "Running:"
                    for _f in function_calls_to_run:
                        yield f"\n - {_f.get_call_str()}"
                    yield "\n\n"

            function_call_results = self.run_function_calls(function_calls_to_run, role="user")
            # Add results of the function calls to the messages
            if len(function_call_results) > 0:
                fc_responses = "<function_results>"

                for _fc_message in function_call_results:
                    fc_responses += "<result>"
                    fc_responses += "<tool_name>" + _fc_message.tool_call_name + "</tool_name>"  # type: ignore
                    fc_responses += "<stdout>" + _fc_message.content + "</stdout>"  # type: ignore
                    fc_responses += "</result>"
                fc_responses += "</function_results>"

                messages.append(Message(role="user", content=fc_responses))

            # -*- Yield new response using results of tool calls
            yield from self.response_stream(messages=messages)
        logger.debug("---------- Claude Response End ----------")

    # def response_stream(self, messages: List[Message]) -> Iterator[str]:
    #     logger.debug("---------- Claude Response Start ----------")
    #     # -*- Log messages for debugging
    #     for m in messages:
    #         m.log()

    #     response_timer = Timer()
    #     response_timer.start()

    #     assistant_message_content = ""
    #     response_is_function_call = False
    #     tool_calls_in_response = 0
    #     is_closing_tool_call = False

    #     response = self.invoke_stream(messages=messages)

    #     with response as stream:
    #         for stream_delta in stream.text_stream:
    #             # logger.debug(f"Stream Delta: {stream_delta}")

    #             # Add response content to assistant message
    #             if stream_delta is not None:
    #                 assistant_message_content += stream_delta

    #             # If the response is a tool call, it will start with a "<function" token
    #             # If response == "<function", set response_is_function_call to True

    #             if "<function" in stream_delta:
    #                 if assistant_message_content.count("<function") > assistant_message_content.count("</function>"):
    #                     response_is_function_call = True
    #                     tool_calls_in_response += 1

    #             if response_is_function_call:
    #                 if assistant_message_content.count("<function") == assistant_message_content.count("</function>"):
    #                     response_is_function_call = False
    #                     is_closing_tool_call = True

    #             if not response_is_function_call:
    #                 if is_closing_tool_call and stream_delta.strip().endswith(">"):
    #                     is_closing_tool_call = False

    #                 yield stream_delta

    #     response_timer.stop()
    #     logger.debug(f"Time to generate response: {response_timer.elapsed:.4f}s")

    #     # -*- Create assistant message
    #     assistant_message = Message(
    #         role="assistant",
    #         content=assistant_message_content,
    #     )

    #     logger.debug(f"tool calls in response: {tool_calls_in_response}")

    #     logger.debug(f"Assistant Message: {assistant_message}")

    #     # Check if the response is a tool call
    #     try:
    #         if tool_calls_in_response > 0:
    #             if "<function_calls>" in assistant_message_content and "</function_calls>" in assistant_message_content:
    #                 # List of tool calls added to the assistant message
    #                 tool_calls: List[Dict[str, Any]] = []
    #                 # Break the response into tool calls
    #                 tool_call_responses = assistant_message_content.split("</function_calls>")
    #                 for tool_call_response in tool_call_responses:
    #                     # Add back the closing tag if this is not the last tool call
    #                     if tool_call_response != tool_call_responses[-1]:
    #                         tool_call_response += "</function_calls>"

    #                     if "<function_calls>" in tool_call_response and "</function_calls>" in tool_call_response:
    #                         # Extract tool call string from response
    #                         tool_call_dict = extract_tool_from_xml(tool_call_response)
    #                         logger.debug(f"Tool Call Dict: {tool_call_dict}")

    #                         tool_call_name = tool_call_dict.get("tool_name")
    #                         tool_call_args = tool_call_dict.get("parameters")
    #                         function_def = {"name": tool_call_name}
    #                         if tool_call_args is not None:
    #                             function_def["arguments"] = json.dumps(tool_call_args)
    #                         tool_calls.append(
    #                             {
    #                                 "type": "function",
    #                                 "function": function_def,
    #                             }
    #                         )
    #                         logger.debug(f"Tool Calls: {tool_calls}")

    #                 # If tool call parsing is successful, add tool calls to the assistant message
    #                 if len(tool_calls) > 0:
    #                     assistant_message.tool_calls = tool_calls
    #     except Exception:
    #         logger.warning(f"Could not parse tool calls from response: {assistant_message_content}")
    #         pass

    #     # -*- Update usage metrics
    #     # Add response time to metrics
    #     assistant_message.metrics["time"] = response_timer.elapsed
    #     if "response_times" not in self.metrics:
    #         self.metrics["response_times"] = []
    #     self.metrics["response_times"].append(response_timer.elapsed)

    #     # -*- Add assistant message to messages
    #     messages.append(assistant_message)
    #     assistant_message.log()

    #     # -*- Parse and run function call
    #     if assistant_message.tool_calls is not None and self.run_tools:
    #         function_calls_to_run: List[FunctionCall] = []
    #         for tool_call in assistant_message.tool_calls:
    #             _function_call = get_function_call_for_tool_call(tool_call, self.functions)
    #             if _function_call is None:
    #                 messages.append(Message(role="user", content="Could not find function to call."))
    #                 continue
    #             if _function_call.error is not None:
    #                 messages.append(Message(role="user", content=_function_call.error))
    #                 continue
    #             function_calls_to_run.append(_function_call)

    #         if self.show_tool_calls:
    #             if len(function_calls_to_run) == 1:
    #                 yield f"- Running: {function_calls_to_run[0].get_call_str()}\n\n"
    #             elif len(function_calls_to_run) > 1:
    #                 yield "Running:"
    #                 for _f in function_calls_to_run:
    #                     yield f"\n - {_f.get_call_str()}"
    #                 yield "\n\n"

    #         function_call_results = self.run_function_calls(function_calls_to_run, role="user")
    #         # Add results of the function calls to the messages
    #         if len(function_call_results) > 0:
    #             fc_responses = "<function_results>"

    #             for _fc_message in function_call_results:
    #                 fc_responses += "<result>"
    #                 fc_responses += "<tool_name>" + _fc_message.tool_call_name + "</tool_name>"
    #                 fc_responses += "<stdout>" + _fc_message.content + "</stdout>"
    #                 fc_responses += "</result>"
    #             fc_responses += "</function_results>"

    #             messages.append(Message(role="user", content=fc_responses))

    #         # -*- Yield new response using results of tool calls
    #         yield from self.response_stream(messages=messages)
    #     logger.debug("---------- Claude Response End ----------")

    def get_instructions_to_generate_tool_calls(self) -> List[str]:
        return []

    def get_tool_call_prompt(self) -> Optional[str]:
        if self.functions is not None and len(self.functions) > 0:
            tool_call_prompt = dedent("""\
            In this environment you have access to a set of tools you can use to answer the user's question.
            Do not show the user the function calls you are making. Only show the results of the function calls.
            You may call them like this:
            <function_calls>
            <invoke>
            <tool_name>$TOOL_NAME</tool_name>
            <parameters>
            <$PARAMETER_NAME>$PARAMETER_VALUE</$PARAMETER_NAME>
            ...
            </parameters>
            </invoke>
            </function_calls>             
            """)
            tool_call_prompt += "Here are the tools available:"
            tool_call_prompt += "\n<tools>"
            for _f_name, _function in self.functions.items():
                _function_def = _function.get_definition_for_prompt_dict()
                if _function_def:
                    tool_call_prompt += "\n<tool_description>\n"
                    tool_call_prompt += f"<tool_name>{_function_def.get('name')}</tool_name>\n"
                    tool_call_prompt += f"<description>{_function_def.get('description')}</description>\n"
                    arugments = _function_def.get("arguments")
                    tool_call_prompt += "\n<parameters>"
                    if arugments:
                        for arg in arugments:
                            tool_call_prompt += "\n<parameter>"
                            tool_call_prompt += f"<name>{arg}</name>\n"
                            if isinstance(arugments.get(arg).get("type"), str):
                                tool_call_prompt += f"<type>{arugments.get(arg).get('type')}</type>\n"
                            else:
                                tool_call_prompt += f"<type>{arugments.get(arg).get('type')[0]}</type>\n"
                            tool_call_prompt += "</parameter>"
                    tool_call_prompt += "</parameters>"
                    tool_call_prompt += "</tool_description>"
            tool_call_prompt += "\n</tools>\n\n"
            return tool_call_prompt
        return None

    def get_system_prompt_from_llm(self) -> Optional[str]:
        return self.get_tool_call_prompt()

    def get_instructions_from_llm(self) -> Optional[List[str]]:
        return self.get_instructions_to_generate_tool_calls()
