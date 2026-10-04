"""Registry of numerical models. Unknown model IDs are refused, never guessed."""
from . import compartment

MODELS = {compartment.MODEL_ID: compartment}


def get_model(model_id):
    if model_id not in MODELS:
        raise ValueError(f'Unknown model_id {model_id!r}; registered: {sorted(MODELS)}')
    return MODELS[model_id]
