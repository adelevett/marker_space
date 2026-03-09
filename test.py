import sys

print("Testing imports without monkey patches!")
print(sys.path)

print("Spoofing torch version...")
import torch
torch.__version__ = "2.4.0"

print("Importing transformers...")
import transformers
print(f"Transformers version: {transformers.__version__}")

print("Importing surya...")
import surya

print("Importing surya tokenizer...")
from surya.ocr_error.tokenizer import DistilBertTokenizer
print("Surya tokenizer imported smoothly!")

print("Importing surya encoder...")
from surya.ocr_error.model.encoder import DistilBertModel
print("Surya encoder imported smoothly!")

print("Importing marker...")
import marker.converters.pdf
print("Marker imported successfully!")

print("Importing PP-DocLayoutV3...")
from transformers import AutoImageProcessor, AutoModelForObjectDetection
processor = AutoImageProcessor.from_pretrained("PaddlePaddle/PP-DocLayoutV3_safetensors", trust_remote_code=True)
print("Processor loaded!")

print("All imports succeeded!")
