# Tool use
- uv for python
- pydantic 

# Atomic rule
- After completing a task that changes files, the agent MUST create a git commit with a descriptive message summarizing the changes.
- Do not create empty or analysis-only commits just to satisfy the atomic rule.
- When continuing work in the same session or on a continuous task, prefer amending the latest relevant commit instead of creating a new commit.
- Do not write unit tests. 
