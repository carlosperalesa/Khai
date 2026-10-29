from contextvars import ContextVar

# Each provider attempt, including failover, charges the same durable root objective.
current_budget = ContextVar('khai_budget', default=None)

# A generation may finish after an owner correction; it cannot update the new goal.
current_goal_revision = ContextVar('khai_goal_revision', default=None)
current_question_revision = ContextVar('khai_question_revision', default=None)


class StaleGoalRevision(ValueError):
    pass
