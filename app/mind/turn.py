"""Provider-neutral tool-call contract. Model output proposes; Core authorizes."""
from typing import Literal, Union, Annotated
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Arguments(BaseModel):
    model_config = ConfigDict(extra='forbid')


class StartArguments(Arguments):
    goal: str = Field(min_length=3, max_length=8000)


class TargetArguments(Arguments):
    task_id: str = Field(pattern=r'^task_[a-f0-9]{32}$')


class ChangeArguments(TargetArguments):
    goal_revision: int = Field(ge=0)
    change: str = Field(min_length=3, max_length=8000)


class AnswerArguments(Arguments):
    question_id: str = Field(pattern=r'^question_[a-f0-9]{32}$')
    answer: str = Field(min_length=1, max_length=8000)


class StartCall(Arguments):
    name: Literal['start_task']
    arguments: StartArguments


class ReviseCall(Arguments):
    name: Literal['revise_task']
    arguments: ChangeArguments


class StatusCall(Arguments):
    name: Literal['get_task_status']
    arguments: TargetArguments


class CancelCall(Arguments):
    name: Literal['cancel_task']
    arguments: TargetArguments


class AnswerCall(Arguments):
    name: Literal['answer_question']
    arguments: AnswerArguments


class HealthCall(Arguments):
    name: Literal['get_system_status']
    arguments: Arguments


ToolCall = Annotated[Union[StartCall, ReviseCall, StatusCall, CancelCall, AnswerCall, HealthCall],
                     Field(discriminator='name')]


class AssistantTurn(Arguments):
    reply: str = Field(default='', max_length=16000)
    tool_calls: list[ToolCall] = Field(default_factory=list, max_length=1)

    @model_validator(mode='after')
    def one_outcome(self):
        if bool(self.reply.strip()) == bool(self.tool_calls):
            raise ValueError('El turno debe contener una respuesta o una operación concreta')
        return self

    def intent(self):
        if not self.tool_calls:
            return {'action': 'GENERAL_CHAT', 'reply': self.reply}
        call = self.tool_calls[0]
        args = call.arguments.model_dump()
        if call.name == 'start_task':
            return {'action': 'ACTION_REQUIRED', 'extracted_goal': args['goal'], 'reply': '',
                    'tool_call': call.model_dump()}
        return {'action': 'TOOL_CALL', 'reply': '', 'tool_call': call.model_dump()}
