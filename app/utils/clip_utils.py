import io
from typing import List, Tuple, Optional

import torch
from PIL import Image
from transformers import CLIPProcessor, CLIPModel

# Lazy singletons to avoid repeated heavy inits
_model: Optional[CLIPModel] = None
_processor: Optional[CLIPProcessor] = None
_device: Optional[torch.device] = None


def _ensure_model() -> Tuple[CLIPModel, CLIPProcessor, torch.device]:
    global _model, _processor, _device
    if _model is None or _processor is None:
        _device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        _model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32").to(_device)
        _model.eval()
        _processor = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")
    return _model, _processor, _device


def clip_image_text_scores(image: Image.Image, texts: List[str]) -> List[float]:
    """
    Compute CLIP similarity scores between the given image and list of texts.
    Returns a list of normalized scores [0..1].
    """
    if image.mode != "RGB":
        image = image.convert("RGB")

    model, processor, device = _ensure_model()
    with torch.no_grad():
        inputs = processor(text=texts, images=[image], return_tensors="pt", padding=True)
        inputs = {k: v.to(device) for k, v in inputs.items()}
        outputs = model(**inputs)
        logits_per_image = outputs.logits_per_image  # shape (1, N)
        # Convert to probabilities via softmax, then to list
        probs = logits_per_image.softmax(dim=1)[0]
        scores = probs.detach().cpu().tolist()
        return scores


def best_clip_label(image: Image.Image, labels: List[str]) -> Tuple[str, float]:
    """
    Return the best matching label and its score.
    """
    scores = clip_image_text_scores(image, labels)
    if not scores:
        return "", 0.0
    best_idx = max(range(len(scores)), key=lambda i: scores[i])
    return labels[best_idx], float(scores[best_idx])