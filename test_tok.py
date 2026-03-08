import subprocess
import sys

# Upgrade transformers in this test script too, to match the app environment
subprocess.run([sys.executable, "-m", "pip", "install", "transformers>=5.1.0", "--quiet"], check=True)

try:
    import transformers.tokenization_utils as _tok_utils
    print("IMPORTED OK:", _tok_utils)
except Exception as e:
    print("FAILED TO IMPORT:", type(e), e)

import unicodedata
def _is_control(char):
    if char in ("\t", "\n", "\r"): return False
    return unicodedata.category(char) in ("Cc", "Cf")
def _is_punctuation(char):
    cp = ord(char)
    if (33 <= cp <= 47) or (58 <= cp <= 64) or (91 <= cp <= 96) or (123 <= cp <= 126): return True
    return unicodedata.category(char).startswith("P")
def _is_whitespace(char):
    if char in (" ", "\t", "\n", "\r"): return True
    return unicodedata.category(char) == "Zs"

try:
    import transformers.tokenization_utils as _tok_utils
    _tok_utils._is_control = _is_control
    _tok_utils._is_punctuation = _is_punctuation
    _tok_utils._is_whitespace = _is_whitespace
except ImportError:
    pass

try:
    from transformers.tokenization_utils import PreTrainedTokenizer, _is_control, _is_punctuation, _is_whitespace
    print("SUCCESS: _is_control is", _is_control)
except ImportError as e:
    print("IMPORT ERROR AFTER PATCH:", e)

