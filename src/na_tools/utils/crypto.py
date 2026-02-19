"""安全随机字符串生成。"""

import secrets
import string


def random_string(length: int = 32) -> str:
    """生成指定长度的安全随机字符串（字母+数字）。"""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))
