import logging
import os
import requests
import torch
from transformers import AutoTokenizer, AutoModelForCausalLM, AutoModelForSeq2SeqLM

logger = logging.getLogger(__name__)

DEFAULT_MODEL_NAME = "google/flan-t5-base"

def generate_with_gemma(prompt):
    """
    Generates text using Gemma:2b via local Ollama instance.
    """
    try:
        response = requests.post(
            "http://localhost:11434/api/generate",
            json={
                "model": "gemma:2b",
                "prompt": prompt,
                "stream": False
            },
            timeout=30
        )
        return response.json()["response"]
    except Exception as e:
        logger.error(f"Ollama/Gemma generation failed: {e}")
        return "Error generating response"

class AIEngine:
    """
    Central AI Engine for the EcoHeritage platform.
    Manages the globally loaded FLAN-T5 model for Lesson Generation and Summarization.
    """
    _model = None
    _tokenizer = None
    _model_loaded = False
    _model_name = DEFAULT_MODEL_NAME
    _prompt_cache = {}  # Simple in-memory cache for prompts

    @classmethod
    def load_model(cls, model_name: str = None):
        """
        Load the model and tokenizer once globally.
        Now uses a specific cache directory on Drive D to avoid disk space issues.
        """
        model_name = model_name or cls._model_name
        
        if cls._model_loaded:
            return cls._model, cls._tokenizer
        
        try:
            logger.info(f"Loading shared model: {model_name}")
            
            # Use Drive D for model cache
            cache_dir = "d:/main project/ecoheritage/.cache/huggingface"
            os.makedirs(cache_dir, exist_ok=True)
            
            token = os.environ.get('HF_TOKEN') or os.environ.get('HUGGING_FACE_HUB_TOKEN')
            
            cls._tokenizer = AutoTokenizer.from_pretrained(
                model_name, 
                token=token, 
                trust_remote_code=False, 
                cache_dir=cache_dir
            )
            
            # Optimization for CPU performance and memory
            device = "cuda" if torch.cuda.is_available() else "cpu"
            
            if device == "cpu":
                # Use float32 for CPU for better kernel optimization
                dtype = torch.float32
                logger.info("Using float32 for CPU inference")
                
                import multiprocessing
                num_cores = multiprocessing.cpu_count()
                torch.set_num_threads(min(num_cores, 8))
            else:
                dtype = "auto"

            # Use AutoModelForSeq2SeqLM for FLAN-T5
            if "t5" in model_name.lower():
                model_class = AutoModelForSeq2SeqLM
            else:
                model_class = AutoModelForCausalLM

            cls._model = model_class.from_pretrained(
                model_name, 
                token=token, 
                trust_remote_code=False,
                torch_dtype=dtype,
                cache_dir=cache_dir,
                low_cpu_mem_usage=True
            )
            
            cls._model = cls._model.to(device)
            cls._model.eval()
            
            cls._model_loaded = True
            cls._model_name = model_name
            logger.info(f"Shared model loaded successfully on {device}")
            
            return cls._model, cls._tokenizer
            
        except Exception as e:
            logger.error(f"Failed to load shared model: {str(e)}")
            raise RuntimeError(f"Failed to load AI model: {str(e)}")

    @classmethod
    def get_model(cls):
        """Get the globally loaded model and tokenizer."""
        if not cls._model_loaded:
            return cls.load_model()
        return cls._model, cls._tokenizer

    @classmethod
    def generate_text(cls, prompt: str, max_length: int = 500, min_length: int = 10, temperature: float = 0.5) -> str:
        """
        Common helper to generate text using common models.
        Automatically handles Seq2Seq vs CausalLM.
        """
        # 1. Check prompt cache first
        cache_key = f"{prompt}_{max_length}_{temperature}"
        if cache_key in cls._prompt_cache:
            logger.info("Returning cached response for prompt")
            return cls._prompt_cache[cache_key]

        model, tokenizer = cls.get_model()
        device = "cuda" if torch.cuda.is_available() else "cpu"
        
        logger.info(f"Generating text with prompt: {prompt[:50]}...")
        
        inputs = tokenizer(prompt, return_tensors="pt", truncation=True, max_length=1024)
        inputs = inputs.to(device)
        
        # Determine model type for generation
        is_seq2seq = any(tn in cls._model_name.lower() for tn in ["t5", "bart", "pegasus"])
        
        with torch.no_grad():
            if is_seq2seq:
                # Seq2Seq generation (T5)
                outputs = model.generate(
                    **inputs,
                    max_new_tokens=max_length,
                    do_sample=True,
                    temperature=temperature,
                    top_p=0.9
                )
                generated_text = tokenizer.decode(outputs[0], skip_special_tokens=True)
            else:
                # CausalLM generation (Phi-3)
                outputs = model.generate(
                    **inputs,
                    max_new_tokens=max_length,
                    do_sample=True,
                    temperature=temperature,
                    top_p=0.9,
                    repetition_penalty=1.1,
                    eos_token_id=tokenizer.eos_token_id,
                    pad_token_id=tokenizer.pad_token_id,
                    use_cache=True
                )
                generated_text = tokenizer.decode(outputs[0][inputs['input_ids'].shape[1]:], skip_special_tokens=True)
        
        result = generated_text.strip()
        logger.info(f"Generated text ({len(result)} chars)")
        
        # 2. Store in cache
        cls._prompt_cache[cache_key] = result
        return result

    @classmethod
    def generate_chatbot_response(cls, lesson_title, lesson_content, student_question):
        """
        Generates a context-aware response for the lesson chatbot using Gemma via Ollama.
        """
        prompt = f"""
        You are a helpful educational assistant for "EcoHeritage Kerala".
        Answer the student's question clearly and simply based on the lesson context provided.

        Lesson: {lesson_title}
        Context: {lesson_content[:1000]}

        Student Question: {student_question}
        Assistant:
        """
        
        return generate_with_gemma(prompt)

    @classmethod
    def simplify_content(cls, content):
        """
        Simplifies educational content for beginners.
        """
        prompt = f"""Simplify the following educational content for beginner students. 
Use short sentences, clear language, and explain any technical terms simply.

Content: {content[:1500]}

Simplified Version:"""
        
        return cls.generate_text(prompt, max_length=500, temperature=0.3)

    @classmethod
    def generate_story_interaction(cls, story_text, student_question):
        """
        Generates a response as a Storytelling Guide.
        """
        prompt = f"""You are a storytelling guide helping students learn Kerala's cultural history.
Story context: {story_text[:1500]}

Student question: {student_question}

Answer the student as a guide who is part of the narrative, encouraging them to explore further."""
        
        return cls.generate_text(prompt, max_length=300, temperature=0.7)
