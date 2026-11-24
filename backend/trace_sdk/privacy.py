import re
from typing import Any

PRIVATE_KEY = re.compile(
    r"password|passwd|secret|authorization|cookie|apikey|accesskey|privatekey|"
    r"token$|tracewrite|sessionid|credential|chainofthought|reasoningcontent|hiddenthought|"
    r"银行卡|e?mail(address)?$|phone(number)?$|mobile(number)?$|identitycard|身份证|手机号"
)
PRIVATE_VALUE = re.compile(
    r"(?i)\b(?:bearer|basic|apikey)\s+\S+|\bsk-[a-z0-9_-]{12,}|"
    r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\.[\w-]{32,}|"
    r"\b[0-9a-f]{24}\.[\w-]{43}(?![\w-])|\beyJ[\w-]+\.eyJ[\w-]+\.[\w-]+|"
    r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[a-z]{2,}|(?<!\d)1[3-9]\d{9}(?!\d)"
)


def check_fields(value: Any, depth: int = 0) -> None:
    if depth > 10:
        raise ValueError("FIELD_DEPTH")
    if isinstance(value, dict):
        for key, child in value.items():
            if PRIVATE_KEY.search(re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", key.lower())):
                raise ValueError("SENSITIVE_FIELD")
            check_fields(key, depth + 1)
            check_fields(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            check_fields(child, depth + 1)
    elif isinstance(value, str):
        if "\x00" in value or PRIVATE_VALUE.search(value):
            raise ValueError("UNSAFE_VALUE")


def redact(value: Any) -> Any:
    """Bound traversal before copying user data or applying text redaction."""
    remaining = 32768

    def walk(node: Any, depth: int) -> Any:
        nonlocal remaining
        remaining -= 1 + (len(node) if isinstance(node, str) else 0)
        if remaining < 0 or depth > 10:
            raise ValueError("event exceeds size or depth limit")
        if isinstance(node, dict):
            result = {}
            for k, v in node.items():
                if not isinstance(k, str) or len(k) > remaining:
                    raise ValueError("invalid key")
                remaining -= len(k) + 1
                if PRIVATE_KEY.search(
                    re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", k.lower())
                ) or PRIVATE_VALUE.search(k):
                    continue
                result[k] = walk(v, depth + 1)
            return result
        if isinstance(node, list):
            return [walk(v, depth + 1) for v in node]
        if isinstance(node, str):
            return PRIVATE_VALUE.sub("[REDACTED]", node)
        return node

    return walk(value, 0)
