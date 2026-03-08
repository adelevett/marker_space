import sys
import types

if "transformers.onnx" not in sys.modules:
    onnx_mod = types.ModuleType("transformers.onnx")
    onnx_mod.OnnxConfig = object
    sys.modules["transformers.onnx"] = onnx_mod

import marker.converters.pdf
print("Marker imported successfully!")
