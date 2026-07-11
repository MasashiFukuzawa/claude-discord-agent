---toml
id = "example-task"
description = "Example repository-scoped implementation task"
required_vars = ["task_description"]
optional_vars = ["verification", "constraints"]
---
# Task

${task_description}

# Constraints

${constraints}

# Verification

${verification}

Work only within the registered repository. Do not access credentials, perform production changes,
or execute destructive operations unless those actions were explicitly authorized in the task.

End the response with the structured `<<<DISCORD_AGENT_RESULT>>>` JSON block required by the Worker contract.
