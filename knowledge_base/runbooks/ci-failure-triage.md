# Runbook: CI failure triage

1. Identify the failed workflow, job, and step.
2. Read the final error and the surrounding log context.
3. Retrieve the head commit and compare its diff with the previous successful commit.
4. Search the knowledge base for similar incidents and remediation history.
5. Form a root-cause hypothesis and assign a confidence score.
6. Generate the smallest safe patch.
7. Apply and validate the patch only inside an isolated temporary workspace.
8. If tests pass, perform a release-risk assessment.
9. Require human approval before a release or rollback action.
10. Record the outcome so the incident can become future retrieval context.
