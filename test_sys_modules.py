import sys
import types

try:
    import transformers.tokenization_utils as _tok_utils
except ImportError:
    pass

import unicodedata
def _is_control(char): return False
def _is_punctuation(char): return True
def _is_whitespace(char): return True

try:
    # Notice we are setting attributes on sys.modules["transformers.tokenization_utils"]
    # rather than just the local _tok_utils binding
    import transformers.tokenization_utils as _tok_utils
    sys.modules["transformers.tokenization_utils"]._is_control = _is_control
    sys.modules["transformers.tokenization_utils"]._is_punctuation = _is_punctuation
    sys.modules["transformers.tokenization_utils"]._is_whitespace = _is_whitespace
except ImportError:
    pass

try:
    from transformers.tokenization_utils import PreTrainedTokenizer, _is_control, _is_punctuation, _is_whitespace
    print("SUCCESS: _is_control is", _is_control)
except ImportError as e:
    print("IMPORT ERROR AFTER PATCH:", e)
