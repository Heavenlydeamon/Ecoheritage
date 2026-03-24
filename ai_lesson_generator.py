"""
AI Lesson Generator Module
========================
Uses HuggingFace FLAN-T5 model for comprehensive study material generation.
Employs a multi-prompt strategy to generate robust content.
"""

import logging
import torch
import re
from typing import Dict

from mainapp.ai_engine import AIEngine
from mainapp.ai_quiz_generator import ModelLoadError

logger = logging.getLogger(__name__)

class LessonGenerationError(Exception):
    """Custom exception for lesson generation errors"""
    pass

class AILessonGenerator:
    """
    AI Lesson Generator using the globally loaded HuggingFace FLAN-T5 model.
    """
    
    def __init__(self):
        # We reuse the model and tokenizer from AIEngine to save RAM/VRAM
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
    
    def get_model(self):
        """Get the globally loaded model and tokenizer."""
        return AIEngine.get_model()

    def _generate_segment(self, model, tokenizer, prompt: str, max_length: int = 250, min_length: int = 10) -> str:
        """
        Helper to generate a specific segment of text given a prompt.
        Now uses AIEngine.generate_text under the hood.
        """
        return AIEngine.generate_text(prompt, max_length=max_length, min_length=min_length)
    
    def clean_generated_text(self, text: str) -> str:
        """
        Cleans up common generation artifacts from FLAN-T5.
        """
        if not text:
            return ""
            
        # Remove repeated sentences
        sentences = re.split(r'(?<=[.!?])\s+', text)
        unique_sentences = []
        for s in sentences:
            if s.strip() and s not in unique_sentences:
                unique_sentences.append(s.strip())
        
        cleaned = " ".join(unique_sentences)
        
        # Capitalize first letters
        if cleaned and cleaned[0].islower():
            cleaned = cleaned[0].upper() + cleaned[1:]
            
        return cleaned

    def generate_lesson(self, title: str, context_text: str = None, length: str = "medium") -> str:
        """
        Generates a detailed lesson using section-wise generation.
        
        Args:
            title: The subject to generate a lesson for.
            context_text: Optional chapter text to base the generation on.
            length: short, medium, or detailed.
        """
        try:
            model, tokenizer = self.get_model()
        except ModelLoadError as e:
            raise LessonGenerationError(f"Model not available: {str(e)}")
            
        # Define length-based constraints
        constraints = {
            "short": {"max": 150, "min": 30},
            "medium": {"max": 250, "min": 80},
            "detailed": {"max": 450, "min": 150}
        }
        
        # Get constraints for this request, defaulting to medium
        c = constraints.get(length.lower(), constraints["medium"])
        
        logger.info(f"Generating {length} lesson for: {title} (Context provided: {bool(context_text)})")
        
        context_slice = context_text[:1200] if context_text else ""
        sections = {}
        
        # Section Prompt Definitions
        prompts = [
            ("Overview", 
             f"Provide a high-level overview of {title}. Explain what it is and why it matters." if not context_text else 
             f"Based on: '{context_slice}', provide an overview of {title}."),
            
            ("Background", 
             f"Discuss the historical or cultural background of {title}. How did it originate?" if not context_text else 
             f"From the text: '{context_slice}', extract the historical or environmental background of {title}."),
            
            ("Key Concepts", 
             f"Explain the most important concepts and principles of {title} in detail." if not context_text else 
             f"Identify and explain the key concepts of {title} mentioned in: '{context_slice}'."),
            
            ("Examples", 
             f"Provide specific, illustrative examples related to {title} for students." if not context_text else 
             f"Find specific examples or case studies in the text: '{context_slice}' regarding {title}."),
            
            ("Key Terms", 
             f"List essential vocabulary and definitions for {title}." if not context_text else 
             f"List 5-8 key terms and definitions from the text: '{context_slice}' related to {title}."),
            
            ("Summary", 
             f"Provide a concluding summary of {title} for final review." if not context_text else 
             f"Summarize the main takeaways about {title} from the text: '{context_slice}'.")
        ]
        
        full_text_list = [f"# {title}\n"]
        
        for section_title, base_prompt in prompts:
            # Force target length based on selection
            full_prompt = f"{base_prompt} Write a {length} educational response."
            
            segment_text = self._generate_segment(
                model, tokenizer, full_prompt, 
                max_length=c["max"], 
                min_length=c["min"]
            )
            sections[section_title] = self.clean_generated_text(segment_text)
            
            full_text_list.append(f"## {section_title}\n{sections[section_title]}\n")
            
        lesson_content = "\n".join(full_text_list)
        
        if len(lesson_content.strip()) < 150:
             raise LessonGenerationError("AI failed to generate sufficient content. Try providing more text.")
             
        return lesson_content

    def generate_summary_only(self, title: str, context_text: str = "") -> str:
        """Generates only a summary from provided text."""
        try:
            model, tokenizer = self.get_model()
        except ModelLoadError as e:
            raise LessonGenerationError(f"Model not available: {str(e)}")
            
        if not context_text:
            return f"# Summary: {title}\n\nThe user did not provide any context text to summarize. Please provide chapter text for a grounded summary."
            
        context_slice = context_text[:1500]
        prompt = f"Provide a comprehensive summary of the following text regarding {title}: '{context_slice}'. Focus on main points."
        summary = self._generate_segment(model, tokenizer, prompt, max_length=300)
        return f"# Summary: {title}\n\n{self.clean_generated_text(summary)}"

    def generate_key_terms_only(self, title: str, context_text: str = "") -> str:
        """Generates only key terms from provided text."""
        try:
            model, tokenizer = self.get_model()
        except ModelLoadError as e:
            raise LessonGenerationError(f"Model not available: {str(e)}")
            
        if not context_text:
            return f"# Key Terms: {title}\n\nThe user did not provide any context text to extract terms from. Please provide chapter text for grounded extraction."
            
        context_slice = context_text[:1500]
        prompt = f"Extract the most important key terms and their definitions from this text: '{context_slice}' related to {title}. Format as a list."
        key_terms = self._generate_segment(model, tokenizer, prompt, max_length=300)
        return f"# Key Terms: {title}\n\n{self.clean_generated_text(key_terms)}"

    def suggest_topics(self, subject_name: str, num_topics: int = 5) -> list:
        """
        Suggests a list of topics for a given subject name.
        Uses Gemma (Ollama) if available, falls back to T5.
        """
        prompt = f"List exactly {num_topics} essential educational topics for a school subject titled '{subject_name}'. Provide only the topic names, one per line, no numbering."
        
        from mainapp.ai_engine import generate_with_gemma
        
        # Try Gemma first as it's better at lists
        raw_output = generate_with_gemma(prompt)
        
        if "Error" in raw_output or not raw_output.strip():
            # Fallback to T5
            try:
                model, tokenizer = self.get_model()
                prompt_t5 = f"What are 5 major topics to teach in a school class about '{subject_name}'? Please list them clearly."
                raw_output = self._generate_segment(model, tokenizer, prompt_t5, max_length=200)
            except:
                return []
        
        # Parse the output into a list of topics
        topics = []
        # Support both newline and comma separation
        parts = re.split(r'[\n,]', raw_output)
        for line in parts:
            line = line.strip()
            # Remove numbering (e.g., "1. Topic Name" or "1) Topic Name")
            line = re.sub(r'^\d+[\.\)]\s*', '', line)
            # Remove common bullet points
            line = re.sub(r'^[\-\*]\s*', '', line)
            # Remove common prefixes like "Topic:"
            line = re.sub(r'^Topic:\s*', '', line, flags=re.IGNORECASE)
            
            if line and len(line) > 3 and len(line) < 100:
                # Basic cleaning
                lower_line = line.lower()
                fillers = ['sure', 'the following', 'here are', 'of course', 'certainly', 'topics:', 'educational topics']
                if not any(filler in lower_line for filler in fillers) and lower_line not in ['and', 'the', 'with']:
                    topics.append(line)
                
        # If AI failed to provide enough, return what we have
        return list(dict.fromkeys(topics))[:num_topics] # Remove duplicates
