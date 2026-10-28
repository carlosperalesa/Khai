"""Base classes for GOD mode and dynamic skills."""


class ApprovalRequiredException(Exception):
    def __init__(self, action_type, message):
        self.action_type, self.message = action_type, message
        super().__init__(message)


class BaseSkill:
    """Base contract for dynamic skills with checkpointed steps and approvals."""

    def __init__(self, task_id, state_manager, telegram):
        self.task_id, self.state, self.telegram = task_id, state_manager, telegram

    def run_step(self, name, fn, *args, **kwargs):
        if self.state.is_step_completed(self.task_id, name):
            return self.state.get_step_data(self.task_id, name)
        result = fn(*args, **kwargs)
        self.state.save_step(self.task_id, name, result)
        return result

    def require_approval(self, action_type, details):
        raise ApprovalRequiredException(action_type, details)
