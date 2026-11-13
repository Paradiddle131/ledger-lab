class StepError(ValueError):
    """A source's step refusing what it was served, on purpose: its message names a file, row or
    column and never a value, so the fetch kit shows it to the operator."""
