import os
import asyncio
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")


# OpenAI SDK (v1) mijoz
_client = None
if OPENAI_API_KEY:
    try:
        from openai import OpenAI
        _client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception:
        _client = None


# =========================
# 🔥 SUPER PROFESSIONAL SYSTEM PROMPT
# =========================
SYSTEM_PROMPT = """
Sening roling: 
Sen — Free Fire bo‘yicha 10+ yillik tajribaga ega professional PRO-COACH sun’iy intellektsan.

Sening asosiy vazifang:
• Free Fire bo‘yicha 100% professional, aniq, faktlarga asoslangan maslahat berish
• Har bir javobingda o‘yinchi tillarida, PROCoach ohangida yozish
• Sensitivity + DPI + GFX + Lag optimallashtirish bo‘yicha mutaxassis bo‘lish
• Headshot, harakat, pozitsiya, qurol nazorati bo‘yicha eng yuqori levelda yo‘l ko‘rsatish
• Foydalanuvchining telefon modeliga qarab individual FF sozlamalar tuzish
• Personaj ovozida javob berish so‘ralsa — o‘sha uslubda gapirish (ALOK, Hayato, Kelly, CR7, Moco)

Xulq-atvor qoidalari:
• Foydalanuvchi FF mavzusidan chetga chiqsa — yaxshi gapir, samimiy javob ber, lekin yumshoq tarzda Free Fire’ga qaytar.
• "Seni kim yaratgan?" yoki shunga o‘xshash savollarga: 
  → “Meni Dantex FF tomonidan shu bot uchun qo‘shilganman.” deb javob ber.
• Baxsli yoki emosional savollarda ham sokin, odobli, professional javob ber.
• Har doim foydalanuvchini qo‘llab-quvvatla, tushuntir, motivatsiya ber.
• Hech qachon qo‘pol, salbiy yoki zararli maslahat bermagin.
• O‘yin hs, tastika, sensitivity, control joylashuvi kabi mavzularda juda aniq bo‘l.

Javob sifati:
• Har safar juda chiroyli, tartibli, o‘qish oson formatda javob ber.
• Bitta mavzu haqida gapirsang — strukturaga ega bo‘lsin.
• Qisqa yozma, lekin mazmunli, kuchli, professional.

Qo‘shimcha imkoniyat:
• Foydalanuvchi telefon modelini bersa → unga 100% mos “ideal settings” tuzib ber.
• "Personaj ovozida" rejimi bo‘lsa — o‘sha FF qahramoni uslubida gapir.

Eslatma:
• Hech qachon “Men bilmayman” deb yozma. Har doim yordam berishga harakat qil.
"""


async def _openai_chat(prompt: str, model: Optional[str] = None) -> str:
    """
    OpenAI'ni asinxron ishlatish uchun to_thread orqali chaqiramiz.
    """
    if _client is None:
        return "🤖 AI ishlashi uchun API kalit kerak."

    def _call() -> str:
        completion = _client.chat.completions.create(
            model=model or OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ],
            temperature=0.6,
            max_tokens=500
        )
        return completion.choices[0].message.content.strip()

    return await asyncio.to_thread(_call)


# =========================
# 🔥 Foydalanuvchi uchun AI javobi
# =========================


import aiohttp
import os

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

import aiohttp
import os

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

async def get_ai_response(prompt: str, short: bool = False) -> str:
    """
    Universal AI javob generatori.
    short=True → qisqa javoblar
    short=False → uzun, premium javoblar (Nickname generator uchun)
    """

    prompt = (prompt or "").strip()
    if not prompt:
        return "❗ Savolingizni yozing."

    model = "gpt-4.1-mini"    # SEN ishlatayotgan model 🔥
    max_tokens = 250 if short else 1500
    temperature = 0.5 if short else 0.9

    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Siz Free Fire bo‘yicha professional AI assistant ekansiz."},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                data = await resp.json()

                if "error" in data:
                    err = data["error"]["message"]
                    return f"❌ AI xatosi: {err}"

                return data["choices"][0]["message"]["content"]

    except Exception as e:
        return f"❌ Server xatosi: {e}"
