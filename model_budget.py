"""Shared accounting for the exact serialized input sent by NodeClient."""

import json

MODEL_INPUT_CHAR_LIMIT = 190_000


def request_characters(instructions, task, context, schema):
    envelope = {"task": task, "context": context, "expected_output": schema}
    return len(instructions) + len(json.dumps(envelope, ensure_ascii=False)) + len(json.dumps(schema))
