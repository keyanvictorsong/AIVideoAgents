"""Reusable local editing pipeline. Heavy speech dependencies load on demand."""

__version__ = "0.1.0"


def edit_video(*args, **kwargs):
    """See :func:`talking_editor.pipeline.edit_video`."""
    from .pipeline import edit_video as run
    return run(*args, **kwargs)


def edit_folder(*args, **kwargs):
    """See :func:`talking_editor.pipeline.edit_folder`."""
    from .pipeline import edit_folder as run
    return run(*args, **kwargs)

