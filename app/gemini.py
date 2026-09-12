import json
from google import genai
from google.genai import types
from app.config import GEMINI_API_KEY

MODEL = "gemini-2.5-flash"

class GeminiError(RuntimeError):
    pass

class Gemini:
    def __init__(self):
        if not GEMINI_API_KEY:
            raise GeminiError("GEMINI_API_KEY is not configured.")
        self.client = genai.Client(api_key=GEMINI_API_KEY)

    def json(self, prompt: str, system: str = "") -> dict:
        response = self.client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system or None,
                temperature=0.2,
                response_mime_type="application/json",
            ),
        )
        text = getattr(response, "text", None)
        if not text:
            raise GeminiError("Gemini returned an empty response.")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise GeminiError(f"Gemini returned invalid JSON: {text[:1000]}") from exc
