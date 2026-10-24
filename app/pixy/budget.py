from contextvars import ContextVar

# Each provider attempt, including failover, charges the same durable root objective.
current_budget = ContextVar('khai_budget', default=None)
