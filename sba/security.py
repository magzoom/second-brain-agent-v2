"""
Security scanner for external content.

Checks text from Google Drive and Apple Notes for prompt injection patterns
before indexing. If malicious content is detected, the tool returns a warning
and the caller receives a Telegram notification.
"""

import re
from typing import Optional

# Patterns that indicate prompt injection attempts (English + Russian)
_THREAT_PATTERNS = [
    # English — direct injection
    (r'ignore\s+(previous|all|above|prior)\s+instructions', "prompt_injection"),
    (r'forget\s+(previous|all|above|prior)\s+instructions', "prompt_injection"),
    (r'disregard\s+(your|all|any)\s+(instructions|rules|guidelines)', "disregard_rules"),
    (r'act\s+as\s+(if\s+)?you\s+(have\s+no|don\'t\s+have)\s+(restrictions|limits|rules)', "bypass_restrictions"),
    (r'do\s+not\s+tell\s+the\s+user', "deception_hide"),
    (r'system\s+prompt\s+override', "sys_prompt_override"),
    (r'new\s+instructions?\s*:', "new_instructions"),
    (r'<system>', "system_tag"),
    (r'\[system\]', "system_tag"),
    # Russian — direct injection
    (r'игнори\w*\s+(предыдущ\w+|все|все\s+предыдущ\w+)\s+(инструкц\w+|правил\w+)', "prompt_injection_ru"),
    (r'забудь\s+(все|предыдущ\w+)\s+(инструкц\w+|правил\w+)', "prompt_injection_ru"),
    (r'выполни\s+следующ\w+\s+(инструкц|команд|код)', "execute_instructions_ru"),
    (r'системн\w+\s+промпт', "sys_prompt_ru"),
    (r'ты\s+(теперь|должен)\s+(игнориров|забыть|действоват)', "bypass_ru"),
    (r'новые\s+инструкц\w*\s*:', "new_instructions_ru"),
    # Exfiltration via shell commands with secrets
    (r'curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|API)', "exfil_curl"),
    (r'wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|API)', "exfil_wget"),
    (r'cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|config\.yaml)', "read_secrets"),
    # Jailbreak bypass techniques
    (r'DAN\s+mode', "jailbreak_dan"),
    (r'developer\s+mode\s+enabled', "jailbreak_devmode"),
    (r'jailbreak', "jailbreak"),
]

# RIGHT-TO-LEFT OVERRIDE — genuinely used for filename/text obfuscation attacks.
# Other zero-width chars (​ etc.) are common in copy-pasted web/Office text.
_INVISIBLE_CHARS = {'‮'}


def scan_content(text: str) -> Optional[str]:
    """
    Scan text for prompt injection patterns.

    Returns an error string describing the threat if found, or None if clean.
    """
    if not text:
        return None

    # Check for invisible unicode
    for char in _INVISIBLE_CHARS:
        if char in text:
            return f"invisible unicode U+{ord(char):04X} (possible injection obfuscation)"

    # Check threat patterns
    for pattern, threat_id in _THREAT_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return f"threat pattern '{threat_id}'"

    return None
