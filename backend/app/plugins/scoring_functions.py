"""Trusted pure functions. The executable entry point accepts JSON data only."""

import json
import sys
from typing import Any

TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


def check_schema(schema: Any) -> None:
    if not isinstance(schema, dict) or schema.keys() - {
        "type",
        "properties",
        "required",
        "items",
        "additionalProperties",
        "enum",
    }:
        raise ValueError("schema 支持 type/properties/required/items/additionalProperties/enum")
    if "type" in schema and schema["type"] not in TYPES:
        raise ValueError("未知 JSON 类型")
    props = schema.get("properties", {})
    keys = schema.get("required", [])
    if (
        not isinstance(props, dict)
        or not isinstance(keys, list)
        or not all(isinstance(k, str) for k in keys)
    ):
        raise ValueError("properties 应为对象，required 应为字符串数组")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise ValueError("enum 需要非空数组")
    if "additionalProperties" in schema and type(schema["additionalProperties"]) is not bool:
        raise ValueError("additionalProperties 需要布尔值")
    for child in props.values():
        check_schema(child)
    if "items" in schema:
        check_schema(schema["items"])


def conforms(value: Any, schema: dict[str, Any]) -> bool:
    if "type" in schema and type(value) not in TYPES[schema["type"]]:
        return False
    if "enum" in schema and not any(type(value) is type(x) and x == value for x in schema["enum"]):
        return False
    if isinstance(value, dict):
        props = schema.get("properties", {})
        return (
            all(k in value for k in schema.get("required", []))
            and (schema.get("additionalProperties", True) or not value.keys() - props.keys())
            and all(conforms(value[k], s) for k, s in props.items() if k in value)
        )
    return not (isinstance(value, list) and "items" in schema) or all(
        conforms(x, schema["items"]) for x in value
    )


def run(
    key: str, expected: dict[str, Any], prediction: dict[str, Any], config: dict[str, Any]
) -> bool:
    output = prediction.get("structured_output")
    if key == "exact_match":
        return (
            isinstance(expected.get("answer"), str)
            and prediction.get("answer") == expected["answer"]
        )
    if key == "numeric_tolerance":
        actual = (output or {}).get("value")
        reference = expected.get("structured_output", {}).get("value")
        return (
            type(actual) in (int, float)
            and type(reference) in (int, float)
            and abs(actual - reference) <= config["tolerance"]
        )
    if key == "required_fields":
        return isinstance(output, dict) and all(k in output for k in config["fields"])
    if key == "json_schema_match":
        return conforms(output, config["output_schema"])
    raise ValueError("unknown trusted function")


def limit_resources() -> None:
    if sys.platform != "win32":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (128 << 20, 128 << 20))
        resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
        resource.setrlimit(resource.RLIMIT_NOFILE, (16, 16))
    else:
        # JOBOBJECT_EXTENDED_LIMIT_INFORMATION. Keep the job handle until process exit.
        import ctypes
        from ctypes import wintypes as w

        class Basic(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_longlong),
                ("job_time", ctypes.c_longlong),
                ("flags", w.DWORD),
                ("min_ws", ctypes.c_size_t),
                ("max_ws", ctypes.c_size_t),
                ("active", w.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", w.DWORD),
                ("scheduling", w.DWORD),
            ]

        class Limits(ctypes.Structure):
            _fields_ = [
                ("basic", Basic),
                ("io", ctypes.c_ulonglong * 6),
                ("process_memory", ctypes.c_size_t),
                ("other", ctypes.c_size_t * 3),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.restype = w.HANDLE
        job = w.HANDLE(kernel.CreateJobObjectW(None, None))
        limits = Limits()
        limits.basic.flags = 0x102
        limits.basic.process_time = 20_000_000
        limits.process_memory = 128 << 20
        if not (
            job
            and kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
            and kernel.AssignProcessToJobObject(job, w.HANDLE(-1))
        ):
            raise RuntimeError("resource limits unavailable")


if __name__ == "__main__":
    limit_resources()
    request = json.loads(sys.stdin.buffer.read(131073))
    print(json.dumps(run(**request)))
