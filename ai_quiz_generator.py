"""
AI Quiz Generator Module
========================
Uses Gemma via Ollama for question generation and Python for distractor generation.
Supports PDF text extraction for generating quizzes from PDF files.
"""

import logging
import os
import random
import re
from dataclasses import dataclass
from typing import List, Optional, Dict

from mainapp.ai_engine import AIEngine, generate_with_gemma

# Try to import PyMuPDF for PDF text extraction
try:
    import fitz  # PyMuPDF
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False
    logging.warning("PyMuPDF (fitz) not installed. PDF text extraction will not be available.")

# Configure logging
logger = logging.getLogger(__name__)

# Constants
MAX_INPUT_LENGTH = 5000  # Maximum characters for input text
MIN_INPUT_LENGTH = 100   # Minimum characters for meaningful generation
MAX_QUESTIONS = 10        # Maximum questions to generate
DEFAULT_QUESTIONS = 6    # Default number of questions
DEFAULT_MODEL_NAME = "google/flan-t5-base"


@dataclass
class GeneratedQuestion:
    """Data class for a generated question"""
    question_text: str
    options: List[str]
    correct_answer: str
    difficulty: str  # easy, medium, hard


class QuizGenerationError(Exception):
    """Custom exception for quiz generation errors"""
    pass


class ModelLoadError(QuizGenerationError):
    """Exception raised when model fails to load"""
    pass


class InputValidationError(QuizGenerationError):
    """Exception raised for invalid input"""
    pass


class AIQuizGenerator:
    """
    AI Quiz Generator using Gemma via Ollama and Python logic.
    """
    
    def __init__(self, model_name: str = None):
        self.model_name = model_name or DEFAULT_MODEL_NAME
    
    @classmethod
    def load_model(cls, model_name: str = None):
        """Redirects to AIEngine.load_model."""
        try:
            return AIEngine.load_model(model_name)
        except Exception as e:
            logger.error(f"Failed to load model via AIEngine: {str(e)}")
            raise ModelLoadError(f"Failed to load model: {str(e)}")
    
    @classmethod
    def get_model(cls):
        """Redirects to AIEngine.get_model."""
        try:
            return AIEngine.get_model()
        except Exception as e:
            raise ModelLoadError(f"Model not available: {str(e)}")
    
    @classmethod
    def is_model_loaded(cls) -> bool:
        """Check if model is loaded via AIEngine"""
        return AIEngine._model_loaded
    
    def validate_input(self, text: str) -> str:
        """Validate and clean input text."""
        if not text:
            raise InputValidationError("Input text cannot be empty")
        
        text = text.strip()
        word_count = len(text.split())
        if word_count < 20:
            raise InputValidationError(
                f"Input text too short for quality generation. Minimum 20 words required. "
                f"Current: {word_count} words."
            )
        
        # Content Cleaning
        text = re.sub(r'#+\s*', '', text) # Remove markdown headers
        text = re.sub(r'\*+\s*', '', text) # Remove bold/list markers
        text = re.sub(r'\s+', ' ', text) # Normalize whitespace
        
        return text
    
    def generate_questions(self, text: str, num_questions: int = DEFAULT_QUESTIONS) -> List[GeneratedQuestion]:
        """
        Generate multiple-choice questions using a hybrid approach:
        1. AI (Gemma) generates Question and Correct Answer.
        2. Python generates 3 distractors for each.
        """
        text = self.validate_input(text)
        
        try:
            # Step 1: Generate Q&A with Gemma
            prompt = f"""
            Generate exactly {num_questions} quiz questions with correct answers based on the text below.
            Format each as:
            Q: [Question]
            A: [Correct Answer]

            Text:
            {text}
            """
            raw_output = generate_with_gemma(prompt)
            
            # Step 2: Parse Q&A
            qa_pairs = self._parse_gemma_questions(raw_output)
            
            final_quiz = []
            for q_text, answer in qa_pairs:
                # Step 3: Generate choices automatically with Python
                options = self._generate_options_python(answer)
                
                difficulty = self._determine_difficulty(q_text)
                final_quiz.append(GeneratedQuestion(
                    question_text=q_text,
                    options=options,
                    correct_answer=answer,
                    difficulty=difficulty
                ))
                
                if len(final_quiz) >= num_questions:
                    break
            
            # Fallback if AI fails
            if not final_quiz:
                return self._generate_rule_based_questions(text, num_questions)
                
            return final_quiz
            
        except Exception as e:
            logger.error(f"Gemma hybrid pipeline failed: {str(e)}")
            return self._generate_rule_based_questions(text, num_questions)

    def _parse_gemma_questions(self, raw_output: str) -> List[tuple]:
        """Helper to parse Q and A pairs from Gemma output."""
        pairs = []
        # Look for Q: ... A: ... or Q: ... Answer: ... patterns
        q_blocks = re.split(r'Q:', raw_output)
        for block in q_blocks:
            if not block.strip(): continue
            
            ans_match = re.search(r'(?:A|Answer):\s*(.+?)(?=\n|Q:|$)', block, re.DOTALL | re.IGNORECASE)
            if ans_match:
                q_text = re.split(r'(?:A|Answer):', block, flags=re.IGNORECASE)[0].strip()
                answer = ans_match.group(1).strip()
                if q_text and answer:
                    pairs.append((q_text, answer))
        return pairs

    def _generate_options_python(self, correct_answer: str) -> List[str]:
        """Step 3: Option Generation (Python logic) - Most Important!"""
        distractors_db = {
            "Kathakali": ["Mohiniyattam", "Bharatanatyam", "Kuchipudi"],
            "India": ["China", "Japan", "Nepal"],
            "Kerala": ["Tamil Nadu", "Karnataka", "Andhra Pradesh"],
            "Onam": ["Vishu", "Thiruvathira", "Deepavali"],
            "Malayalam": ["Tamil", "Telugu", "Kannada"],
            "Cochin": ["Trivandrum", "Calicut", "Quilon"],
            "Western Ghats": ["Eastern Ghats", "Himalayas", "Aravallis"],
            "Arabic Sea": ["Bay of Bengal", "Indian Ocean", "Red Sea"],
        }

        # Try to find specific distractors
        options = []
        for key, value in distractors_db.items():
            if key.lower() in correct_answer.lower() or correct_answer.lower() in key.lower():
                options = value[:]
                break
        
        # If not enough distractors, add generic ones
        if len(options) < 3:
            generic_placeholders = ["Option B", "Option C", "Option D"]
            options += generic_placeholders[len(options):]

        options = options[:3] # Ensure only 3 distractors
        options.append(correct_answer)
        
        random.shuffle(options)
        return options

    def _determine_difficulty(self, question: str) -> str:
        """Estimate difficulty based on question keywords."""
        q = question.lower()
        if any(word in q for word in ['analyze', 'evaluate', 'implication', 'synthesis', 'complex']):
            return 'hard'
        if any(word in q for word in ['explain', 'describe', 'compare', 'contrast', 'how']):
            return 'medium'
        return 'easy'

    def _generate_rule_based_questions(self, text: str, num_questions: int) -> List[GeneratedQuestion]:
        """Simplified fallback when AI pipeline fails."""
        questions = []
        sentences = [s.strip() for s in re.split(r'[.!?]+', text) if len(s.strip()) > 30]
        random.shuffle(sentences)
        
        for s in sentences[:num_questions]:
            q_text = f"Based on the text, what is a key detail mentioned in: '{s[:60]}...'?"
            words = s.split()
            correct = words[0] if words else "This detail"
            options = self._generate_options_python(correct)
            questions.append(GeneratedQuestion(q_text, options, correct, "easy"))
            
        return questions


# Singleton access
_global_generator = None

def get_quiz_generator() -> AIQuizGenerator:
    global _global_generator
    if _global_generator is None:
        _global_generator = AIQuizGenerator()
    return _global_generator

def generate_quiz_from_text(text: str, num_questions: int = DEFAULT_QUESTIONS) -> List[Dict]:
    """Public convenience function."""
    generator = get_quiz_generator()
    questions = generator.generate_questions(text, num_questions)
    
    return [
        {
            'question_text': q.question_text,
            'options': q.options,
            'correct_answer': q.correct_answer,
            'difficulty': q.difficulty
        }
        for q in questions
    ]

# PDF Utilities
def extract_text_from_pdf(pdf_file) -> str:
    if not PDF_SUPPORT:
        raise ImportError("pymupdf not installed")
    try:
        if hasattr(pdf_file, 'read'):
            import io
            doc = fitz.open(stream=pdf_file.read(), filetype="pdf")
        else:
            doc = fitz.open(pdf_file)
        text = "".join(page.get_text() for page in doc)
        doc.close()
        return text.strip()
    except Exception as e:
        logger.error(f"PDF extraction failed: {e}")
        raise ValueError(f"PDF error: {e}")

def is_pdf_file(file_name: str) -> bool:
    return file_name.lower().endswith('.pdf')
