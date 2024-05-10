import asyncio
from typing import Optional, Type

from opendevin.controller.agent import Agent
from opendevin.controller.state.state import State
from opendevin.core.config import config
from opendevin.core.exceptions import (
    AgentMalformedActionError,
    AgentNoActionError,
    LLMOutputError,
    MaxCharsExceedError,
)
from opendevin.core.logger import opendevin_logger as logger
from opendevin.core.schema import AgentState
from opendevin.events.action import (
    Action,
    AddTaskAction,
    AgentDelegateAction,
    AgentFinishAction,
    ChangeAgentStateAction,
    MessageAction,
    ModifyTaskAction,
    NullAction,
)
from opendevin.events.event import Event
from opendevin.events.observation import (
    AgentDelegateObservation,
    AgentStateChangedObservation,
    ErrorObservation,
    NullObservation,
    Observation,
)
from opendevin.events.stream import EventSource, EventStream, EventStreamSubscriber

MAX_ITERATIONS = config.max_iterations
MAX_CHARS = config.llm.max_chars


class AgentController:
    id: str
    agent: Agent
    max_iterations: int
    event_stream: EventStream
    state: State
    agent_task: Optional[asyncio.Task] = None
    delegate: 'AgentController | None' = None
    _agent_state: AgentState = AgentState.LOADING
    _pending_action: Action | None = None

    def __init__(
        self,
        agent: Agent,
        event_stream: EventStream,
        sid: str = 'default',
        max_iterations: int = MAX_ITERATIONS,
        max_chars: int = MAX_CHARS,
        inputs: Optional[dict] = None,
    ):
        """Initializes a new instance of the AgentController class.

        Args:
            agent: The agent instance to control.
            sid: The session ID of the agent.
            max_iterations: The maximum number of iterations the agent can run.
            max_chars: The maximum number of characters the agent can output.
        """
        self.id = sid
        self.agent = agent
        self.state = State(inputs=inputs or {})
        self.event_stream = event_stream
        self.event_stream.subscribe(
            EventStreamSubscriber.AGENT_CONTROLLER, self.on_event
        )
        self.max_iterations = max_iterations
        self.max_chars = max_chars
        self.agent_task = asyncio.create_task(self._start_run_loop())

    async def close(self):
        if self.agent_task is not None:
            self.agent_task.cancel()
        self.event_stream.unsubscribe(EventStreamSubscriber.AGENT_CONTROLLER)
        await self.set_agent_state_to(AgentState.STOPPED)

    def update_state_for_step(self):
        self.state.iteration += 1

    def update_state_after_step(self):
        self.state.updated_info = []

    async def report_error(self, message: str):
        await self.event_stream.add_event(ErrorObservation(message), EventSource.AGENT)

    async def add_history(self, action: Action, observation: Observation):
        if not isinstance(action, Action):
            raise TypeError(
                f'action must be an instance of Action, got {type(action).__name__} instead'
            )
        if not isinstance(observation, Observation):
            raise TypeError(
                f'observation must be an instance of Observation, got {type(observation).__name__} instead'
            )
        self.state.history.append((action, observation))
        self.state.updated_info.append((action, observation))

    async def on_event(self, event: Event):
        if isinstance(event, ChangeAgentStateAction):
            await self.set_agent_state_to(event.agent_state)  # type: ignore
        elif isinstance(event, MessageAction) and event.source == EventSource.USER:
            await self.add_history(event, NullObservation(''))
            if self.get_agent_state() != AgentState.RUNNING:
                await self.set_agent_state_to(AgentState.RUNNING)
        elif (
            isinstance(event, MessageAction)
            and event.source == EventSource.AGENT
            and event.wait_for_response
        ):
            await self.set_agent_state_to(AgentState.AWAITING_USER_INPUT)
        elif isinstance(event, AgentDelegateAction):
            await self.start_delegate(event)
        elif isinstance(event, AddTaskAction):
            self.state.plan.add_subtask(event.parent, event.goal, event.subtasks)
        elif isinstance(event, ModifyTaskAction):
            self.state.plan.set_subtask_state(event.task_id, event.state)
        elif isinstance(event, AgentFinishAction):
            self.state.outputs = event.outputs  # type: ignore[attr-defined]
            await self.set_agent_state_to(AgentState.FINISHED)
        elif isinstance(event, Observation):
            if self._pending_action and self._pending_action.id == event.cause:
                await self.add_history(self._pending_action, event)
                self._pending_action = None
            else:
                await self.add_history(NullAction(), event)

    async def reset_task(self):
        self.agent.reset()

    async def set_agent_state_to(self, new_state: AgentState):
        logger.info(
            f'Setting agent({type(self.agent).__name__}) state from {self._agent_state} to {new_state}'
        )
        if new_state == self._agent_state:
            return

        self._agent_state = new_state
        if new_state == AgentState.STOPPED or new_state == AgentState.ERROR:
            await self.reset_task()

        await self.event_stream.add_event(
            AgentStateChangedObservation('', self._agent_state), EventSource.AGENT
        )

    def get_agent_state(self):
        """Returns the current state of the agent task."""
        return self._agent_state

    async def start_delegate(self, action: AgentDelegateAction):
        AgentCls: Type[Agent] = Agent.get_cls(action.agent)
        agent = AgentCls(llm=self.agent.llm)
        self.delegate = AgentController(
            sid=self.id + '-delegate',
            agent=agent,
            event_stream=self.event_stream,
            max_iterations=self.max_iterations,
            max_chars=self.max_chars,
            inputs=action.inputs,
        )

    async def _start_run_loop(self):
        while True:
            try:
                await self._step()
            except asyncio.CancelledError:
                logger.info('AgentController task was cancelled')
                break
            except Exception as e:
                logger.error(f'Error while running the agent: {e}')
                await self.report_error(
                    'There was an unexpected error while running the agent'
                )
                await self.set_agent_state_to(AgentState.ERROR)
                break

            await asyncio.sleep(1)

    async def _step(self):
        if self.state is None:
            raise ValueError('AgentController has no state')

        if self.get_agent_state() != AgentState.RUNNING:
            logger.debug('waiting for agent to run...')
            await asyncio.sleep(1)
            return

        if self._pending_action:
            logger.debug('waiting for pending action: ' + str(self._pending_action))
            await asyncio.sleep(1)
            return

        logger.info(f'STEP {self.state.iteration}', extra={'msg_type': 'STEP'})

        if self.delegate is not None:
            delegate_done = await self.delegate._step()
            if delegate_done:
                outputs = self.delegate.state.outputs if self.delegate.state else {}
                obs: Observation = AgentDelegateObservation(content='', outputs=outputs)
                await self.event_stream.add_event(obs, EventSource.AGENT)
                self.delegate = None
                self.delegateAction = None
            return

        if self.state.num_of_chars > self.max_chars:
            raise MaxCharsExceedError(self.state.num_of_chars, self.max_chars)

        self.update_state_for_step()
        action: Action = NullAction()
        try:
            action = self.agent.step(self.state)
            if action is None:
                raise AgentNoActionError('No action was returned')
        except (AgentMalformedActionError, AgentNoActionError, LLMOutputError) as e:
            await self.event_stream.add_event(
                ErrorObservation(str(e)), EventSource.AGENT
            )

        logger.info(f'Action: {action}', extra={'msg_type': 'ACTION'})
        self.update_state_after_step()

        if action.runnable:
            self._pending_action = action
        else:
            await self.add_history(action, NullObservation(''))
        if not isinstance(action, NullAction):
            await self.event_stream.add_event(action, EventSource.AGENT)

        if self._is_stuck():
            await self.report_error('Agent got stuck in a loop')
            await self.set_agent_state_to(AgentState.ERROR)

    def get_state(self):
        return self.state

    def _is_stuck(self):
        # check if delegate stuck
        if self.delegate and self.delegate._is_stuck():
            return True
        if (
            self.state is None
            or self.state.history is None
            or len(self.state.history) < 3
        ):
            return False

        # if the last three (Action, Observation) tuples are too repetitive
        # the agent got stuck in a loop
        if all(
            [
                self.state.history[-i][0] == self.state.history[-3][0]
                for i in range(1, 3)
            ]
        ):
            # it repeats same action, give it a chance, but not if:
            if all(
                isinstance(self.state.history[-i][1], NullObservation)
                for i in range(1, 4)
            ):
                # same (Action, NullObservation): like 'think' the same thought over and over
                logger.info('Action, NullObservation loop detected')
                return True
            elif all(
                isinstance(self.state.history[-i][1], ErrorObservation)
                for i in range(1, 4)
            ):
                # (NullAction, ErrorObservation): errors coming from an exception
                # (Action, ErrorObservation): the same action getting an error, even if not necessarily the same error
                logger.info('Action, ErrorObservation loop detected')
                return True

        return False
