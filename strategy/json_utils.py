import json
import re
from typing import Any, Dict


def extract_json_object(text: str) -> Dict[str, Any]:
    """
    LLM이 순수 JSON만 반환하지 못했을 때를 대비해 첫 JSON object를 추출.
    """
    if not text:
        return {}

    text = text.strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    # code fence 제거
    text2 = re.sub(r"^```(?:json)?", "", text, flags=re.I).strip()
    text2 = re.sub(r"```$", "", text2).strip()
    try:
        return json.loads(text2)
    except Exception:
        pass

    # 가장 바깥 JSON object 추출
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidate = text[start:end+1]
        try:
            return json.loads(candidate)
        except Exception:
            pass

    return {"raw_text": text}
