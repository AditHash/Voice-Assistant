import os
import tempfile
import base64
import io
import logging
from flask import Flask, request, jsonify, make_response, send_file
from flask_cors import CORS
from pydantic import BaseModel, ValidationError
from sarvamai import SarvamAI
from groq import Groq
from dotenv import load_dotenv

# Load environment variables once
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# GROQ client setup
groq_api_key = os.getenv("GROQ_API_KEY")
if not groq_api_key:
    logger.error("GROQ_API_KEY environment variable not set")
    raise RuntimeError("GROQ_API_KEY environment variable not set")
groq_client = Groq(api_key=groq_api_key)

# SarvamAI client setup
sarvam_api_key = os.getenv("SARVAM_API_KEY")
if not sarvam_api_key:
    logger.error("SARVAM_API_KEY environment variable not set")
    raise RuntimeError("SARVAM_API_KEY environment variable not set")
sarvam_client = SarvamAI(api_subscription_key=sarvam_api_key)

# -------- Pydantic schemas --------

class SimpleRequest(BaseModel):
    request: str

class ChatResponse(BaseModel):
    response: str


@app.route("/voice-chat", methods=["POST"])
def voice_chat():
    temp_audio_path = None
    try:
        logger.info("Received /voice-chat request with content-type: %s", request.content_type)
        # Validate incoming audio content-type
        content_type = request.content_type
        if not content_type or not content_type.startswith("audio/"):
            logger.warning("Invalid Content-Type: %s", content_type)
            return make_response(jsonify({
                "detail": f"Invalid Content-Type: {content_type}. Must be an audio MIME type like audio/wav or audio/mpeg."
            }), 400)

        # Get language code from header, default to 'en-IN'
        language_code = request.headers.get('X-Language-Code', 'en-IN')
        logger.info(f"Using language code: {language_code}")

        # Save incoming audio to temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
            temp_audio_path = tmp.name
            tmp.write(request.data)
        logger.info("Saved incoming audio to temp file: %s", temp_audio_path)

        # Debug: Log file size and first few bytes
        file_size = os.path.getsize(temp_audio_path)
        logger.info(f"Temp audio file size: {file_size} bytes")
        with open(temp_audio_path, "rb") as debug_audio_file:
            first_bytes = debug_audio_file.read(32)
            logger.info(f"First 32 bytes of audio: {first_bytes}")

        # Step 1: Speech-to-text (STT)
        with open(temp_audio_path, "rb") as audio_file:
            stt_response = sarvam_client.speech_to_text.transcribe(
                file=audio_file,
                model="saarika:v2",
                language_code=language_code,  # Use selected language
            )
        logger.info("STT response: %s", getattr(stt_response, "transcript", None))

        user_text = getattr(stt_response, "transcript", None)
        if not user_text:
            logger.error("No transcription text found from STT")
            return make_response(jsonify({"detail": "No transcription text found from STT"}), 500)

        # Step 2: Send transcribed text to LLM chat
        from groq.types.chat import ChatCompletionUserMessageParam

        # Add a system prompt (customize as needed)
        system_prompt_text = (
            "You are Workmates Bot, a helpful, friendly, and multilingual AI voice assistant for India. "
            "You can understand and respond in English, Hindi, Bengali, Gujarati, Kannada, Malayalam, Marathi, Odia, Punjabi, Tamil, and Telugu. "
            "Always reply in the language the user spoke. Keep your answers concise, clear, and conversational."
            "Dont make any assumptions and dont make up information. "
        )
        system_prompt = ChatCompletionUserMessageParam(role="system", content=system_prompt_text)

        # Try to get conversation history from request (optional, for multi-turn)
        history = request.headers.get('X-Chat-History')
        messages = [system_prompt]
        if history:
            import json
            try:
                history_list = json.loads(history)
                for msg in history_list:
                    if 'role' in msg and 'content' in msg:
                        messages.append(ChatCompletionUserMessageParam(role=msg['role'], content=msg['content']))
            except Exception as e:
                logger.warning(f"Invalid history format: {e}")
        # Add current user message
        messages.append(ChatCompletionUserMessageParam(role="user", content=user_text))

        chat_response = groq_client.chat.completions.create(
            messages=messages,
            model="llama-3.3-70b-versatile", # use llama-3.3-70b-versatile for better performance
            # model="llama-3.1-8b-instant", # Use llama-3.1-8b-instant for faster response
            temperature=0.3,
            max_completion_tokens=512,
            top_p=1.0,
            stop=None,
            stream=False,
        )
        llm_text = chat_response.choices[0].message.content
        logger.info("LLM response: %s", llm_text)

        if not llm_text:
            logger.error("No text returned from LLM response")
            return make_response(jsonify({"detail": "No text returned from LLM response"}), 500)

        # Step 3: Convert LLM response text back to speech (TTS)
        tts_response = sarvam_client.text_to_speech.convert(
            text=llm_text,
            target_language_code=language_code,  # Use selected language
        )

        if not tts_response.audios or len(tts_response.audios) == 0:
            logger.error("No audio returned from TTS")
            return make_response(jsonify({"detail": "No audio returned from TTS"}), 500)

        audio_base64 = tts_response.audios[0]
        logger.info("Returning synthesized audio response")

        # Return JSON with transcription, llm_text, and audio as base64
        return jsonify({
            "transcription": user_text,
            "response": llm_text,
            "audio_base64": audio_base64
        })

    except Exception as e:
        logger.exception("Error in /voice-chat endpoint: %s", str(e))
        return make_response(jsonify({"detail": str(e)}), 500)

    finally:
        # Cleanup temp audio file
        if temp_audio_path and os.path.exists(temp_audio_path):
            os.remove(temp_audio_path)
            logger.info("Deleted temp audio file: %s", temp_audio_path)


# -------- Main --------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
