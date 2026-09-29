Add one or more existing labels to an issue. This changes the repository.

Use this only when the user asks to label, tag or categorise a specific issue.
Labels must already exist in the repository (names are case-insensitive); the
tool cannot create, rename or remove labels, and it does not touch any other
issue. Adding a label the issue already has is a no-op.

Parameters:
- number: the issue number (12 for #12).
- labels: 1–5 label names, e.g. ["bug"].

Returns the labels that were added, the ones that were already present, and the
issue's full label list afterwards. Unknown label names return an invalid_input
error listing the available labels.
